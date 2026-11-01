"""Full cycle, end to end, on a real tmux pane with a stand-in agent.

    spec → task → tmux adapter → agent → result → checks → event → wake → ack →
    GPT review → next task → … → project done

REAL here: tmux (a private socket), the tmux adapter, the envelope/result files, git in
the fixture repo, the task contracts, the control-plane events, gates and leases, and
core.wake_bridge's own decision / pending / delivery / acknowledge functions.
MOCKED here, and only here: the LLM agent (tests/fixtures/fullcycle_demo/fake_agent.py),
the GPT reviewer (harness.StubReviewer, which goes through the same inbox functions
ChatGPT would call) and the browser leg (a delivery is recorded, nothing is typed into
ChatGPT). Nothing touches a live pane, project or database.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

from core import task_contract as tc
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from core.fullcycle import spec as specs

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures", "fullcycle_demo"))
import harness as h  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")

DONE = lambda s: s["done"]  # noqa: E731


def _state(st, key):
    return next(t for t in st["tasks"] if t["key"] == key)


@pytest.fixture
def fc(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    adapters = []

    def make(behaviours=None, *, executor="fixture", spec=None, max_attempts=3, holder="e1"):
        root = h.make_project(str(tmp_path))
        specs.create_project("tinycalc", name="tinycalc", root=root, executor=executor)
        specs.put_spec("tinycalc", spec or h.load_spec(), by="test")
        a, log = h.make_adapter(str(tmp_path), root, behaviours or {})
        adapters.append(a)
        e = fe.Engine("tinycalc", a, max_attempts=max_attempts, holder=holder)
        return e, a, log, root

    yield make
    for a in adapters:
        a.kill()


def _events(type_=None):
    q = "SELECT id, type, payload, owner_action_required FROM event WHERE source='fullcycle'"
    conn, _ = cp._c(None)
    rows = conn.execute(q + (" AND type=?" if type_ else "") + " ORDER BY id",
                        (type_,) if type_ else ()).fetchall()
    conn.close()
    return rows


def _git_log(root):
    import subprocess
    return subprocess.run(["git", "-C", root, "log", "--format=%s"], capture_output=True,
                          text=True).stdout.splitlines()


# ── the whole loop ──────────────────────────────────────────────────────────
def test_spec_to_done_with_gpt_adding_the_missing_task(fc):
    e, a, log, root = fc()
    rv = h.StubReviewer(next_tasks={"__project__": [h.CLI_TASK]})
    st = h.run(e, rv, until=DONE, timeout=40)

    assert [(t["key"], t["state"], t["attempt"]) for t in st["tasks"]] == [
        ("add", tc.VERIFIED_DONE, 1), ("mul", tc.VERIFIED_DONE, 1), ("cli", tc.VERIFIED_DONE, 1)]
    # the project checks failed after add+mul; the reviewer added `cli`; then they passed
    assert [(s["key"], s["choice"]) for s in rv.seen] == [
        ("add", "approve"), ("mul", "approve"), ("__project__", "add_tasks"), ("cli", "approve")]
    assert _state(st, "cli")["origin"].startswith("review:")
    assert specs.get_spec("tinycalc")["version"] == 2          # the plan change is versioned
    assert _git_log(root)[:3] == ["fullcycle: cli (attempt 1) — Command-line entry point",
                                  "fullcycle: mul (attempt 1) — Implement mul",
                                  "fullcycle: add (attempt 1) — Implement add"]
    kinds = [__import__("json").loads(p)["fullcycle"] for _, _, p, _ in _events()]
    assert kinds == ["review", "review", "project", "review", "project_completed"]
    # each task went through the contract's verified path, with the evidence recorded
    for t in e._tasks():
        hist = [x["to"] for x in tc.history(t["contract_id"])]
        assert hist == ["CREATED", "WORKING", "AGENT_DONE", "VERIFYING", "VERIFIED_DONE"]
        last = tc.history(t["contract_id"])[-1]["evidence"]
        assert last["tests"] and last["review"]["by"] == "stub-gpt"
    # one envelope per task: no duplicate prompt reached the agent
    assert [x["task_id"] for x in h.agent_log(log)] == [
        "tinycalc:add", "tinycalc:mul", "tinycalc:cli"]
    # ticking a finished project changes nothing and emits nothing
    n = len(_events())
    e.tick()
    e.tick()
    assert len(_events()) == n


def test_review_events_go_through_the_real_wake_bridge_and_a_duplicate_wake_is_harmless(
        fc, monkeypatch):
    from core import wake_bridge as wb
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat("https://chatgpt.com/c/fullcycle-test-chat")
    e, a, log, root = fc()
    h.wait_for(lambda: e.tick() and fe.pending_reviews("tinycalc"), 15, step=0.15)
    (rv,) = fe.pending_reviews("tinycalc")
    eid = rv["event_id"]

    # the engine itself offered the review to the bridge (as cto.emit does): nobody
    # calls should_wake/record by hand, yet the companion has a wake to deliver
    p = wb.pending_wake()
    assert p["pending"] and p["event_id"] == eid, p
    phrase = wb.compose_phrase(event_id=eid, event_type=fe.REVIEW_EVENT, project_id="tinycalc")
    assert f"event={eid}" in phrase and "type=task_completed" in phrase
    # MOCKED browser leg: the companion would type `phrase` into the bound chat
    wb.record_delivery("test-companion", event_id=eid, delivered=True,
                       reason="submitted_and_assistant_started_generating")

    # the reviewer is woken TWICE for the same event and answers both times
    k = f"gpt:{rv['review_id']}"
    first = fe.submit_decision(rv["review_id"], "approve", key=k, by="gpt")
    again = fe.submit_decision(rv["review_id"], "approve", key=k, by="gpt")
    other = fe.submit_decision(rv["review_id"], "reject", key=k + ":2", by="gpt",
                               feedback="changed my mind")
    assert first["ok"] and again["duplicate"] and other["reason"] == "review_already_decided"
    fe.ack_review(rv["review_id"], by="gpt")
    wb.acknowledge(eid)
    assert not wb.pending_wake().get("pending")

    e.tick()
    e.tick()
    add = next(t for t in e._tasks() if t["key"] == "add")
    assert add["state"] == tc.VERIFIED_DONE
    assert [x["to"] for x in tc.history(add["contract_id"])].count("VERIFIED_DONE") == 1
    assert len(_git_log(root)) == 2                              # one commit, not two


# ── negative paths ──────────────────────────────────────────────────────────
def test_agent_crash_restarts_the_pane_and_redispatches(fc):
    e, a, log, root = fc({"add": ["crash", "correct"]})
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert _state(st, "add")["attempt"] == 2
    got = h.agent_log(log)
    assert [(x["task_id"], x["attempt"]) for x in got][:2] == [
        ("tinycalc:add", 1), ("tinycalc:add", 2)]
    assert "agent process exited" in got[1]["feedback"]
    assert a.health()["ok"]


def test_wrong_work_is_caught_by_the_checks_and_the_output_goes_back(fc):
    e, a, log, root = fc({"add": ["wrong", "correct"]})
    rv = h.StubReviewer()
    st = h.run(e, rv, until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert _state(st, "add")["attempt"] == 2
    assert "check_failed" in h.agent_log(log)[1]["feedback"]
    assert "AssertionError" in h.agent_log(log)[1]["feedback"]
    # the reviewer never saw the broken attempt
    assert [s["key"] for s in rv.seen] == ["add"]


def test_reviewer_rejection_sends_the_feedback_and_a_new_attempt(fc):
    e, a, log, root = fc()
    rv = h.StubReviewer({"add": ["reject", "approve"]})
    st = h.run(e, rv, until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert _state(st, "add")["attempt"] == 2
    assert "stub feedback for add" in h.agent_log(log)[1]["feedback"]


def test_a_change_outside_scope_fails_verification(fc):
    e, a, log, root = fc({"add": ["outside", "correct"]})
    st = h.run(e, h.StubReviewer(),
               until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    fb = h.agent_log(log)[1]["feedback"]
    assert "do_not_touch:README.md" in fb and "outside your scope: README.md" in fb
    assert open(os.path.join(root, "README.md")).read().startswith("# tinycalc")
    assert _state(st, "add")["attempt"] == 2


def test_permission_prompt_waits_for_the_owner_and_never_auto_answers(fc):
    e, a, log, root = fc({"add": ["permission"]})
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.OWNER_DECISION,
               15, step=0.15)
    t = next(t for t in e._tasks() if t["key"] == "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]
    (owner_ev,) = _events(fe.OWNER_EVENT)
    payload = __import__("json").loads(owner_ev[2])
    assert owner_ev[3] == 1 and payload["permission"]["command"] == "rm -rf build"
    assert payload["permission"]["decision"] == "owner"
    for _ in range(5):                                       # it stays put by itself
        e.tick()
    assert _state(e.status(), "add")["state"] == tc.OWNER_DECISION

    # the OWNER answers the dialog in the pane and the gate in Owner OS
    a._tmux("send-keys", "-t", a.target, "1", "Enter")
    cp.answer_gate(t["gate_id"], "resume")
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert _state(st, "add")["attempt"] == 1                 # same attempt, no re-send
    assert len(h.agent_log(log)) >= 1 and h.agent_log(log)[0]["attempt"] == 1
    assert all(x["task_id"] != "tinycalc:add" or x["attempt"] == 1 for x in h.agent_log(log))


def test_permission_denied_at_dispatch_sends_nothing(fc):
    e, a, log, root = fc(executor="codex")
    e.tick()
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.OWNER_DECISION
    assert _state(st, "add")["last_reason"].startswith("permission_denied:executor_unavailable")
    assert h.agent_log(log) == []
    assert len(_events(fe.OWNER_EVENT)) == 1
    # an owner "resume" is not a waiver: the gate is checked again and still refuses
    t = next(t for t in e._tasks() if t["key"] == "add")
    cp.answer_gate(t["gate_id"], "resume")
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.OWNER_DECISION
    assert h.agent_log(log) == []


def test_a_blocked_agent_gets_the_tech_leads_answer(fc):
    e, a, log, root = fc({"add": ["blocked", "correct"]})
    rv = h.StubReviewer()
    st = h.run(e, rv, until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert [s["kind"] for s in rv.seen][:2] == ["blocked", "review"]
    assert "stub feedback for add" in h.agent_log(log)[1]["feedback"]
    assert _state(st, "add")["attempt"] == 2


def test_attempts_are_bounded_and_then_the_owner_is_asked(fc):
    e, a, log, root = fc({"add": ["wrong"]}, max_attempts=2)
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.BLOCKED,
               20, step=0.15)
    st = e.status()
    assert _state(st, "add")["state"] == tc.BLOCKED
    assert _state(st, "add")["last_reason"].startswith("max_attempts_2")
    assert len(h.agent_log(log)) == 2
    assert len(_events(fe.OWNER_EVENT)) == 1


# ── crash recovery of the engine itself ─────────────────────────────────────
def test_engine_restart_mid_task_resumes_without_a_second_prompt(fc):
    e, a, log, root = fc({"add": ["hang"]})
    e.tick()                                         # dispatched, agent "working"
    assert _state(e.status(), "add")["state"] == tc.WORKING
    del e                                            # the engine process dies
    e2 = fe.Engine("tinycalc", a, holder="e1")       # restarted (same holder)
    for _ in range(3):
        e2.tick()
    assert len(h.agent_log(log)) == 1                # nothing re-sent
    assert _state(e2.status(), "add")["state"] == tc.WORKING


def test_a_crash_right_after_the_send_does_not_send_twice(fc):
    """The process dies after the adapter sent the prompt and before the engine
    recorded it (state still CREATED, dispatch latch 'sending')."""
    e, a, log, root = fc()
    real = a.submit
    calls = {"n": 0}

    def submit_then_die(env, key):
        r = real(env, key)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("engine process killed")
        return r
    a.submit = submit_then_die
    with pytest.raises(RuntimeError):
        e.tick()
    t = next(t for t in e._tasks() if t["key"] == "add")
    assert t["state"] == tc.CREATED and t["redispatch"] == 1 and t["attempt"] == 1
    e2 = fe.Engine("tinycalc", a, holder="e1")
    st = h.run(e2, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE)
    assert [x["attempt"] for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"] == [1]
    assert _state(st, "add")["attempt"] == 1


def test_a_second_engine_cannot_run_the_project_while_the_lease_is_held(fc):
    e, a, log, root = fc(holder="e1")
    e.tick()
    other = fe.Engine("tinycalc", a, holder="e2")
    assert other.tick()["skipped"] == "lease_held"
    # after the holder's lease expires, the other engine takes over with a higher fence
    late = fe.Engine("tinycalc", a, holder="e2", clock=lambda: 10 ** 10)
    r = late.tick()
    assert "skipped" not in r and r["fence"] >= 2


# ── an owner gate on ONE task parks only that task ─────────────────────────────
# A reviewer-added task whose check is not a test runner is refused at dispatch
# (gates.check_argv) and waits for the owner. It never reached the agent, so nothing of
# it is in the tree: independent tasks go on; only it (and anything depending on it) waits.
RISKY_TASK = {"key": "risky", "title": "Fetch something", "goal": "Needs a network check.",
              "acceptance_criteria": ["the URL answers"],
              "checks": [["curl", "-sf", "http://example.invalid/"]]}


def _with_risky(spec=None):
    import copy
    s = copy.deepcopy(spec or h.load_spec())
    s["tasks"].insert(1, dict(RISKY_TASK))       # add, risky, mul (mul depends on add only)
    specs.put_spec("tinycalc", s, by="review:gpt")


def test_a_hazardous_task_gate_parks_only_that_task(fc):
    e, a, log, root = fc()
    _with_risky()
    st = h.run(e, h.StubReviewer(),
               until=lambda s: _state(s, "mul")["state"] == tc.VERIFIED_DONE, timeout=40)
    assert _state(st, "risky")["state"] == tc.OWNER_DECISION
    assert _state(st, "risky")["last_reason"] == "permission_denied:check_runner_not_allowed:curl"
    assert _state(st, "add")["state"] == tc.VERIFIED_DONE
    assert [x["task_id"] for x in h.agent_log(log)] == ["tinycalc:add", "tinycalc:mul"]
    owner = _events(fe.OWNER_EVENT)
    assert len(owner) == 1 and "risky" in owner[0][2]          # one gate, on the risky task
    assert not st["done"]                                     # the project is not done...
    for _ in range(3):
        e.tick()
    assert len(_events(fe.OWNER_EVENT)) == 1                   # ...and nothing re-fires

    # the owner cancels the hazardous task: the rest of the project completes
    t = next(t for t in e._tasks() if t["key"] == "risky")
    cp.answer_gate(t["gate_id"], "cancel")
    st = h.run(e, h.StubReviewer(next_tasks={"__project__": [h.CLI_TASK]}), until=DONE,
               timeout=40)
    assert _state(st, "risky")["state"] == tc.CANCELLED
    assert "tinycalc:risky" not in [x["task_id"] for x in h.agent_log(log)]


def test_a_dependent_of_a_parked_task_still_waits(fc):
    e, a, log, root = fc()
    import copy
    s = copy.deepcopy(h.load_spec())
    s["tasks"][1]["depends_on"] = ["add", "risky"]            # mul now needs risky too
    _with_risky(s)
    h.run(e, h.StubReviewer(),
          until=lambda s: _state(s, "risky")["state"] == tc.OWNER_DECISION, timeout=30)
    for _ in range(5):
        e.tick()
    st = e.status()
    assert _state(st, "mul")["state"] == tc.CREATED
    assert [x["task_id"] for x in h.agent_log(log)] == ["tinycalc:add"]


def test_a_resumed_parked_task_waits_for_the_task_in_flight(fc, monkeypatch):
    e, a, log, root = fc({"mul": ["hang"]})
    _with_risky()
    h.run(e, h.StubReviewer(),
          until=lambda s: _state(s, "mul")["state"] == tc.WORKING, timeout=30)
    assert _state(e.status(), "risky")["state"] == tc.OWNER_DECISION
    # the owner lifts the hazard (simulated: the gate no longer refuses) and resumes
    real = fe.gates.check_dispatch
    monkeypatch.setattr(fe.gates, "check_dispatch",
                        lambda **kw: None if kw.get("trusted_checks") is False else real(**kw))
    t = next(t for t in e._tasks() if t["key"] == "risky")
    cp.answer_gate(t["gate_id"], "resume")
    for _ in range(3):
        e.tick()
    risky = next(t for t in e._tasks() if t["key"] == "risky")
    assert risky["redispatch"] == 1                            # the intent is kept...
    assert "tinycalc:risky" not in [x["task_id"] for x in h.agent_log(log)]   # ...not sent
    assert _state(e.status(), "mul")["state"] == tc.WORKING


# ── failed QA → specific repair → regression ─────────────────────────────────
def test_a_task_that_breaks_an_earlier_verified_task_is_repaired_before_review(fc, monkeypatch):
    # mul's first attempt is right for mul but breaks add (already verified and committed)
    monkeypatch.setitem(h.BROKEN, "mul", {"tinycalc/mul.py": h.SOLUTIONS["mul"]["tinycalc/mul.py"],
                                          "tinycalc/add.py": h.BROKEN["add"]["tinycalc/add.py"]})
    monkeypatch.setitem(h.SOLUTIONS, "mul", {**h.SOLUTIONS["mul"], **h.SOLUTIONS["add"]})
    e, a, log, root = fc({"mul": ["wrong", "correct"]})
    rv = h.StubReviewer()
    st = h.run(e, rv, until=lambda s: _state(s, "mul")["state"] == tc.VERIFIED_DONE, timeout=40)
    assert _state(st, "mul")["attempt"] == 2
    # the regression never reached the reviewer: one review per task, the repaired one
    assert [(s["key"], s["choice"]) for s in rv.seen if s["key"] != "__project__"] == [
        ("add", "approve"), ("mul", "approve")]
    fb = [x for x in h.agent_log(log) if x["task_id"] == "tinycalc:mul"][1]["feedback"]
    assert "regression:add:" in fb                       # names the task it broke...
    assert "assert add(2, 3) == 5" in fb                 # ...and the failing check
    t = next(t for t in e._tasks() if t["key"] == "mul")
    failed = [h_["evidence"]["verification"] for h_ in tc.history(t["contract_id"])
              if h_["to"] == tc.VERIFICATION_FAILED][0]
    assert [r["task"] for r in failed["regression"]] == ["add"]
    assert not failed["regression"][0]["passed"]


def test_the_regression_gate_renews_the_lease_per_verified_task(fc, monkeypatch):
    e, a, log, root = fc()
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "mul")["state"] == tc.VERIFIED_DONE,
          timeout=40)
    calls = []
    real = e._lead
    monkeypatch.setattr(e, "_lead", lambda: calls.append(1) or real())
    probe = {"id": "tinycalc:probe"}
    out = e._regression_checks(probe)
    assert sorted({r["task"] for r in out}) == ["add", "mul"] and all(r["passed"] for r in out)
    assert len(calls) == 2
    monkeypatch.setenv("FULLCYCLE_REGRESSION_CHECKS", "0")
    assert e._regression_checks(probe) == []


# ── G1: git evidence is required; its absence fails closed ─────────────────────
def _git_failing(e, *subcmds, rc=128, err="fatal: unable to read index"):
    real = e._git
    e._git = lambda *args: (rc, "", err) if args[:1] and args[0] in subcmds else real(*args)
    return real


def test_g1_git_status_failure_fails_verification_closed(fc):
    e, a, log, root = fc()
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
          timeout=40)
    with open(f"{root}/README.md", "w") as fh:                  # out of scope, unclaimed
        fh.write("tampered\n")
    t = next(t for t in e._tasks() if t["key"] == "mul")
    t["attempt"] = 1
    _git_failing(e, "status")
    v = e._verify(t, {"changed_files": ["tinycalc/mul.py"], "summary": "ok"})
    assert not v["passed"]
    assert v["changed_source"] == "unavailable" and v["changed_files"] == []
    assert v["claimed_changed_files"] == ["tinycalc/mul.py"]   # kept for the record only
    assert "changed_files_unverifiable" in v["reasons"]


def test_g1_git_failure_before_first_dispatch_sends_nothing(fc):
    e, a, log, root = fc()
    _git_failing(e, "status")
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.OWNER_DECISION
    assert _state(st, "add")["last_reason"] == "permission_denied:git_status_unavailable"
    assert h.agent_log(log) == []


def test_g1_unreadable_head_refuses_dispatch(fc):
    e, a, log, root = fc()
    real = e._git
    e._git = lambda *args: (128, "", "fatal: bad HEAD") if args[:2] == ("rev-parse", "--verify") \
        else real(*args)
    e.tick()
    assert _state(e.status(), "add")["last_reason"].startswith("permission_denied:git_head_unavailable")
    assert h.agent_log(log) == []


def test_g1_a_non_git_root_is_refused_by_full_cycle(fc):
    e, a, log, root = fc()
    shutil.rmtree(os.path.join(root, ".git"))
    e.tick()
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.OWNER_DECISION
    assert _state(st, "add")["last_reason"] == "permission_denied:project_root_not_git"
    assert h.agent_log(log) == []
    assert len(_events(fe.OWNER_EVENT)) == 1                   # project-wide, gated once


def test_g1_a_transient_git_failure_recovers_on_the_next_attempt(fc):
    e, a, log, root = fc({"add": ["correct"]})
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.WORKING, 15)
    real = _git_failing(e, "status")
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["attempt"] == 2, 15)
    e._git = real                                              # the lock went away
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    t = next(t for t in e._tasks() if t["key"] == "add")
    fails = [x["evidence"]["verification"]["reasons"] for x in tc.history(t["contract_id"])
             if x["to"] == tc.VERIFICATION_FAILED]
    assert fails and "changed_files_unverifiable" in fails[0]
    assert _state(st, "add")["attempt"] == 2


def test_g1_a_failed_git_add_never_marks_the_task_committed(fc):
    e, a, log, root = fc()
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE
          and True, timeout=40)
    # put the approved task back to uncommitted and make `git add` fail
    t = next(t for t in e._tasks() if t["key"] == "add")
    e._set(t, committed=0)
    with open(f"{root}/tinycalc/extra.py", "w") as fh:
        fh.write("x = 1\n")
    _git_failing(e, "add")
    e._commit_pending()
    t = next(t for t in e._tasks() if t["key"] == "add")
    assert t["committed"] == 0 and t["last_reason"].startswith("commit_failed")
