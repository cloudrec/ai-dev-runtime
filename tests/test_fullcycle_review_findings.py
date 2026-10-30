"""Regression tests for the adversarial review of core.fullcycle (2026-10-30).

Each test names the finding it pins. Same harness as test_fullcycle_e2e: real tmux on a
private socket, the stand-in agent, a private DB.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from core import task_contract as tc
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from core.fullcycle import gates
from core.fullcycle import spec as specs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures", "fullcycle_demo"))
import harness as h  # noqa: E402

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def _state(st, key):
    return next(t for t in st["tasks"] if t["key"] == key)


def _task(e, key):
    return next(t for t in e._tasks() if t["key"] == key)


@pytest.fixture
def fc(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    adapters = []

    def make(behaviours=None, *, spec=None, holder="e1", before_spec=None):
        root = h.make_project(str(tmp_path))
        if before_spec:
            before_spec(root)
        specs.create_project("tinycalc", name="tinycalc", root=root, executor="fixture")
        specs.put_spec("tinycalc", spec or h.load_spec(), by="test")
        a, log = h.make_adapter(str(tmp_path), root, behaviours or {})
        adapters.append(a)
        return fe.Engine("tinycalc", a, holder=holder), a, log, root

    yield make
    for a in adapters:
        a.kill()


def _git_log(root):
    return subprocess.run(["git", "-C", root, "log", "--format=%s"], capture_output=True,
                          text=True).stdout.splitlines()


# #1 — a review decision must not make the engine run arbitrary commands
@needs_tmux
def test_1_reviewer_added_checks_are_confined_to_test_runners(fc, tmp_path):
    pwned = tmp_path / "PWNED"
    evil = {"key": "evil", "title": "evil", "goal": "g", "acceptance_criteria": ["x"],
            "checks": [["sh", "-c", f"echo x > {pwned}"]]}
    e, a, log, root = fc()
    rv = h.StubReviewer(next_tasks={"add": [evil]})
    st = h.run(e, rv, until=lambda s: any(t["key"] == "evil" and t["state"] == tc.OWNER_DECISION
                                          for t in s["tasks"]))
    assert _state(st, "evil")["last_reason"] == \
        "permission_denied:check_runner_not_allowed:sh"
    assert _state(st, "evil")["origin"].startswith("review:")
    assert not pwned.exists()
    assert all(x["task_id"] != "tinycalc:evil" for x in h.agent_log(log))


@pytest.mark.parametrize("argv, refused", [
    (["sh", "-c", "x"], "check_runner_not_allowed:sh"),
    (["python3", "-c", "import os"], "check_inline_code_not_allowed"),
    (["python3", "-m", "pip", "install", "x"], "check_module_outside_project:pip"),
    (["python3", "/etc/x.py"], "check_path_outside_project:/etc/x.py"),
    (["python3", "../x.py"], "check_path_escape:../x.py"),
    (["npm", "publish"], "check_subcommand_not_allowed:npm"),
    (["git", "push"], "check_runner_not_allowed:git"),
])
def test_1_untrusted_check_argv(tmp_path, argv, refused):
    assert gates.check_argv(argv, root=str(tmp_path), trusted=False) == refused
    assert gates.check_argv(argv, root=str(tmp_path), trusted=True) is None
    (tmp_path / "pkg").mkdir()
    assert gates.check_argv(["python3", "-m", "pkg.cli"], root=str(tmp_path), trusted=False) is None
    assert gates.check_argv(["python3", "-m", "pytest", "tests"], root=str(tmp_path),
                            trusted=False) is None


@needs_tmux
def test_1_a_reseeded_reviewer_task_stays_untrusted(fc):
    """Crash between the spec version and the task row: the seed must not launder the
    reviewer's task into a trusted spec task."""
    e, a, log, root = fc()
    body = specs.get_spec("tinycalc")["body"]
    body["tasks"].append({"key": "late", "title": "t", "goal": "g",
                          "acceptance_criteria": ["x"], "checks": [], "scope": [],
                          "depends_on": []})
    specs.put_spec("tinycalc", body, by="review:7")
    e._seed()
    assert _task(e, "late")["origin"] == "review:7"


# #2 — never revert or claim work that is not this task's agent's
@needs_tmux
def test_2_a_dirty_tree_holds_the_first_dispatch(fc):
    def owner_wip(root):
        with open(os.path.join(root, "README.md"), "a") as fh:
            fh.write("OWNER WIP\n")
    e, a, log, root = fc(before_spec=owner_wip)
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["last_reason"] == "dirty_worktree:README.md"
    assert h.agent_log(log) == []
    assert "OWNER WIP" in open(os.path.join(root, "README.md")).read()


@needs_tmux
def test_2_an_approved_task_is_committed_even_after_a_crash_before_the_commit(fc, monkeypatch):
    e, a, log, root = fc()
    real = fe.Engine._commit_pending
    state = {"armed": False}

    def die_once(self):
        if state["armed"] and any(t["state"] == tc.VERIFIED_DONE and not t["committed"]
                                  for t in self._tasks()):
            state["armed"] = False
            raise RuntimeError("killed between approval and commit")
        return real(self)
    monkeypatch.setattr(fe.Engine, "_commit_pending", die_once)
    h.wait_for(lambda: e.tick() and fe.pending_reviews("tinycalc"), 15, step=0.15)
    (rv,) = fe.pending_reviews("tinycalc")
    fe.submit_decision(rv["review_id"], "approve", key="k", by="gpt")
    state["armed"] = True
    with pytest.raises(RuntimeError):
        e.tick()
    assert _task(e, "add")["state"] == tc.VERIFIED_DONE and not _task(e, "add")["committed"]
    e2 = fe.Engine("tinycalc", a, holder="e1")
    e2.tick()
    assert _git_log(root)[0] == "fullcycle: add (attempt 1) — Implement add"
    assert _task(e2, "add")["committed"] == 1
    # and the next task was dispatched from a clean tree
    assert h.wait_for(lambda: any(x["task_id"] == "tinycalc:mul" for x in h.agent_log(log)), 10)


@needs_tmux
def test_2_a_failed_commit_raises_an_owner_event_and_holds_the_next_task(fc):
    e, a, log, root = fc()
    real_run = e.host.run

    def commit_fails(argv, **kw):
        if "commit" in argv:
            return 1, "", "forced commit failure"
        return real_run(argv, **kw)
    e.host = type("H", (), {"run": staticmethod(commit_fails), "kind": "local"})()
    h.wait_for(lambda: e.tick() and fe.pending_reviews("tinycalc"), 15, step=0.15)
    (rv,) = fe.pending_reviews("tinycalc")
    fe.submit_decision(rv["review_id"], "approve", key="k", by="gpt")
    e.tick()
    e.tick()
    st = e.status()
    assert _state(st, "add")["last_reason"].startswith("commit_failed")
    assert _state(st, "mul")["state"] == tc.OWNER_DECISION
    assert _state(st, "mul")["last_reason"] == "dirty_worktree:tinycalc/add.py"
    conn, _ = cp._c(None)
    reasons = [r[0] for r in conn.execute(
        "SELECT json_extract(payload,'$.reason') FROM event WHERE type=?", (fe.OWNER_EVENT,))]
    conn.close()
    assert reasons.count("commit_failed") == 1          # once, not every tick
    assert all(x["task_id"] != "tinycalc:mul" for x in h.agent_log(log))


# #3 — roots that are too broad or contain a denied tree
def test_3_broad_or_containing_roots_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "_payment_denied", lambda root: None)
    assert gates.check_project_root("/").startswith("project_root_too_broad")
    assert gates.check_project_root("/root").startswith("project_root_too_broad")
    inner = tmp_path / "proj" / "secrets"
    inner.mkdir(parents=True)
    monkeypatch.setenv("FULLCYCLE_DENY_ROOTS", str(inner))
    assert gates.check_project_root(str(tmp_path / "proj")) == \
        f"project_root_contains_denied:{inner}"


def test_3_a_root_containing_a_payment_dir_is_refused(tmp_path, monkeypatch):
    from core import agent_creation_policy as acp
    pay = tmp_path / "proj" / "payments"
    pay.mkdir(parents=True)
    monkeypatch.setattr(acp, "load_config", lambda: {"hard_deny_dirs": [str(pay)]})
    assert gates.check_project_root(str(tmp_path / "proj")) == \
        "project_root_contains_payment_dir"


# #4 — a moved protected file is seen as deleted; odd names parse exactly
@needs_tmux
def test_4_a_rename_of_a_protected_file_is_a_violation(fc):
    e, a, log, root = fc()
    subprocess.run(["git", "-C", root, "mv", "README.md", "tinycalc/stolen.md"], check=True)
    weird = os.path.join(root, "tinycalc", 'we"ird -> name.py')
    open(weird, "w").close()
    changed = e._changed_files()
    assert "README.md" in changed and 'tinycalc/we"ird -> name.py' in changed
    assert gates.check_changes(changed, scope=["tinycalc/"], do_not_touch=["README.md"]) == \
        ["do_not_touch:README.md"]


# #5 — a transient refusal after a decision is retried, not stranded
@needs_tmux
def test_5_a_busy_refusal_after_the_blocked_answer_is_retried(fc):
    e, a, log, root = fc({"add": ["blocked", "correct"]})
    real = a.submit
    busy = {"n": 2}

    def sometimes_busy(env, key):
        if env.attempt == 2 and busy["n"]:
            busy["n"] -= 1
            return {"ok": False, "already": False, "reason": "refused:busy_mid_turn",
                    "handle": {}}
        return real(env, key)
    a.submit = sometimes_busy
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert busy["n"] == 0 and _state(st, "add")["attempt"] == 2


# a real agent stays mid-turn for minutes: agent_control refuses ("working") rather than
# queue. That is a wait, not a failure — it must never use up MAX_DISPATCH_FAILURES.
@needs_tmux
def test_a_long_mid_turn_agent_is_waited_for_not_failed(fc):
    e, a, log, root = fc()
    real = a.submit
    busy = {"n": fe.MAX_DISPATCH_FAILURES * 2}

    def mid_turn(env, key):
        if busy["n"]:
            busy["n"] -= 1
            return {"ok": False, "already": False, "reason": "refused:working", "handle": {}}
        return real(env, key)
    a.submit = mid_turn
    for _ in range(fe.MAX_DISPATCH_FAILURES * 2):
        e.tick()
    add = _task(e, "add")
    assert add["dispatch_failures"] == 0 and add["last_reason"] == "refused:working", add
    assert not add["gate_id"] and add["state"] != tc.BLOCKED, add
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert _state(st, "add")["attempt"] == 1


# #6 — an unrecognised owner answer keeps a gate open
@needs_tmux
def test_6_an_unrecognised_owner_answer_reopens_the_gate(fc):
    e, a, log, root = fc({"add": ["permission"]})
    h.wait_for(lambda: e.tick() and _task(e, "add")["state"] == tc.OWNER_DECISION, 15, step=0.15)
    g1 = _task(e, "add")["gate_id"]
    cp.answer_gate(g1, "yes")
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"] and t["gate_id"] != g1
    assert t["last_reason"] == "agent_permission_prompt"
    gates_open = [g for g in cp.get_open_gates() if g["id"] == t["gate_id"]]
    assert gates_open and "unrecognised_owner_answer:yes" in gates_open[0]["reason"]


# #7 — only a passing verification of the CURRENT attempt reaches the reviewer
@needs_tmux
def test_7_a_crash_before_recording_attempt_2_reverifies_instead_of_reviewing_attempt_1(
        fc, monkeypatch):
    e, a, log, root = fc({"add": ["wrong", "correct"]})
    real = fe.Engine._finish_verification
    state = {"armed": True}

    def die_on_attempt_2(self, task, v):
        if state["armed"] and task["attempt"] == 2:
            state["armed"] = False
            raise RuntimeError("killed before recording the verification")
        return real(self, task, v)
    monkeypatch.setattr(fe.Engine, "_finish_verification", die_on_attempt_2)
    with pytest.raises(RuntimeError):
        h.wait_for(lambda: e.tick() and False, 20, step=0.15)
    t = _task(e, "add")
    assert t["state"] == tc.VERIFYING and t["attempt"] == 2
    assert t["verification"] == {}          # cleared when attempt 2 was dispatched
    e2 = fe.Engine("tinycalc", a, holder="e1")
    e2.tick()
    (rv,) = fe.pending_reviews("tinycalc")
    assert rv["attempt"] == 2 and rv["payload"]["summary"] == "implemented add"
    assert all(c["passed"] for c in rv["payload"]["checks"])


# #8 — BLOCKED without its review (crash in between) re-requests it
@needs_tmux
def test_8_blocked_without_a_review_is_re_requested(fc, monkeypatch):
    e, a, log, root = fc({"add": ["blocked"]})
    real = fe.Engine._request_review
    state = {"armed": True}

    def die_before_review(self, task, kind, payload):
        if state["armed"] and kind == "blocked":
            state["armed"] = False
            raise RuntimeError("killed after BLOCKED")
        return real(self, task, kind, payload)
    monkeypatch.setattr(fe.Engine, "_request_review", die_before_review)
    with pytest.raises(RuntimeError):
        h.wait_for(lambda: e.tick() and False, 15, step=0.15)
    assert _task(e, "add")["state"] == tc.BLOCKED and not fe.pending_reviews("tinycalc")
    fe.Engine("tinycalc", a, holder="e1").tick()
    (rv,) = fe.pending_reviews("tinycalc")
    assert rv["kind"] == "blocked" and "Integers only" in rv["payload"]["question"]


# #9 — engines with default holders do not share the lease
@needs_tmux
def test_9_default_holders_are_unique(fc):
    e, a, log, root = fc(holder=None)
    e.tick()
    other = fe.Engine("tinycalc", a)
    assert other.holder != e.holder
    assert other.tick()["skipped"] == "lease_held"


# #11 — a task depending on a cancelled one goes to the owner
@needs_tmux
def test_11_a_dependency_on_a_cancelled_task_asks_the_owner(fc):
    e, a, log, root = fc({"add": ["blocked"]})
    h.wait_for(lambda: e.tick() and fe.pending_reviews("tinycalc"), 15, step=0.15)
    (rv,) = fe.pending_reviews("tinycalc")
    fe.submit_decision(rv["review_id"], "cancel", key="c", by="gpt")
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.CANCELLED
    assert _state(st, "mul")["state"] == tc.OWNER_DECISION
    assert _state(st, "mul")["last_reason"] == "dependency_cancelled:add"


# #13 — the .fullcycle ignore is a directory, not a prefix
def test_13_fullcycle_lookalikes_are_checked():
    assert gates.check_changes([".fullcycle_evil.sh", ".fullcyclerc", ".fullcycle/x.json"],
                               scope=["src/"], do_not_touch=[]) == \
        ["outside_scope:.fullcycle_evil.sh", "outside_scope:.fullcyclerc"]


# #15 — keys are plain names
@pytest.mark.parametrize("key", ["a\nb", "a:b", "a/b", "..", "", "x" * 65, "a b"])
def test_15_task_keys_are_plain_names(key):
    with pytest.raises(specs.SpecError):
        specs.validate_task({"key": key, "title": "t", "goal": "g",
                             "acceptance_criteria": ["x"]})


# a worker restarting (systemd) must not wait out LEASE_TTL behind its predecessor
@needs_tmux
def test_a_released_lease_lets_the_successor_lead_at_once(fc):
    e, a, log, root = fc()
    e.tick()
    nxt = fe.Engine("tinycalc", a, holder="e2")
    assert nxt.tick().get("skipped") == "lease_held"
    assert e.release() is True and e.release() is False
    assert not nxt.tick().get("skipped")


def test_worker_project_map():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import fullcycle_worker as w
    assert w.projects("a=s1, b = s2 ,bad,=x,c=") == {"a": "s1", "b": "s2"}
    assert w.projects("") == {}
