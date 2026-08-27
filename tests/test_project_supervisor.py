"""Server-resident project supervisor: continuous development between checkpoints.

The properties under test are the ones that decide whether this is a supervisor
or a machine that generates plausible work:

  * blocks come from the project's own files, never from the supervisor;
  * a roadmap that is missing, exhausted or entirely gated produces an OWNER
    GATE with a named reason, not a smaller invented task;
  * a claim of completion is checked against git - the commit must exist and
    must actually contain the files the agent said it changed;
  * a busy agent is left alone, so no duplicate work and no interrupted turn;
  * nothing runs for a project that was not explicitly enabled.

The handoff tests build a REAL git repository, because "validated against git"
is the whole point of that code and a mocked git would test nothing.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from core import project_supervisor as ps


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("PROJECT_SUPERVISOR_PROJECTS", "pilot")


def _repo(tmp_path, plan: str, name="PROJECT_PLAN.md"):
    repo = tmp_path / "proj"
    repo.mkdir(exist_ok=True)
    (repo / name).write_text(plan, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "plan"], cwd=repo, check=True)
    return str(repo)


def _commit(repo, files: dict, message="work"):
    for rel, content in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _write_handoff(repo, **fields):
    d = os.path.join(repo, ".owner-os")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "handoff.json"), "w", encoding="utf-8") as f:
        json.dump(fields, f)


PLAN = """# Plan

## Ready
- [x] first block, already done
- [ ] add a unit test for the parser
- [ ] wire the second adapter

## Blocked on a live browser session
- [ ] probe the retailer cart page
"""


# ── the roadmap comes from the project, never from the supervisor ───────────

def test_blocks_are_read_from_the_projects_own_file(tmp_path):
    plan = ps.parse_plan(_repo(tmp_path, PLAN))
    assert plan["ok"] and plan["source"] == "PROJECT_PLAN.md"
    assert plan["counts"] == {"total": 4, "done": 1, "open": 3, "actionable": 2}
    assert [b["title"] for b in plan["blocks"] if not b["done"]][0] == \
        "add a unit test for the parser"


def test_tasks_md_is_used_when_no_project_plan_exists(tmp_path):
    plan = ps.parse_plan(_repo(tmp_path, PLAN, name="TASKS.md"))
    assert plan["ok"] and plan["source"] == "TASKS.md"


def test_a_project_with_no_roadmap_is_an_owner_gate_not_an_invented_task(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    d = ps.decide("pilot", str(repo), agent_busy=False)
    assert d["action"] == ps.OWNER_GATE
    assert d["reason"] == "no_plan_file"
    assert "will not author one" in d["detail"]


def test_blocks_under_a_blocked_section_are_never_dispatched(tmp_path):
    plan = ps.parse_plan(_repo(tmp_path, PLAN))
    probe = [b for b in plan["blocks"] if "probe" in b["title"]][0]
    assert probe["gated"] is True
    assert probe["gate_reason"].startswith("section:")


@pytest.mark.parametrize("line", [
    "- [ ] rotate the production credentials",
    "- [ ] deploy to prod",
    "- [ ] reconcile the payment ledger",
    "- [ ] blocked on a real probe",
])
def test_dangerous_or_blocked_text_gates_a_block_on_its_own(tmp_path, line):
    plan = ps.parse_plan(_repo(tmp_path, f"# P\n\n## Ready\n{line}\n"))
    assert plan["blocks"][0]["gated"] is True


def test_a_constraint_line_is_not_treated_as_work(tmp_path):
    """'Preserve X while doing Y' is a rule the next block must respect, not a
    block to hand an agent. The live pilot's only open item is exactly this."""
    plan = ps.parse_plan(_repo(
        tmp_path, "# P\n\n## Ready\n- [ ] Preserve and reuse the existing parser layer\n"))
    b = plan["blocks"][0]
    assert b["constraint"] is True
    assert ps.next_block(plan)["found"] is False


def test_an_exhausted_roadmap_is_a_gate(tmp_path):
    plan = ps.parse_plan(_repo(tmp_path, "# P\n\n## Ready\n- [x] all done\n"))
    n = ps.next_block(plan)
    assert n == {"found": False, "reason": "roadmap_complete"}


def test_the_gate_names_what_is_blocking(tmp_path):
    plan = ps.parse_plan(_repo(
        tmp_path, "# P\n\n## Blocked on a live browser session\n- [ ] probe it\n"))
    n = ps.next_block(plan)
    assert n["reason"] == "all_open_blocks_gated"
    assert n["blocked"][0]["why"].startswith("section:")


# ── completion is evidence, not a claim ────────────────────────────────────

def test_a_handoff_whose_commit_does_not_exist_is_refused(tmp_path):
    repo = _repo(tmp_path, PLAN)
    v = ps.validate_handoff(repo, {
        "block_id": "abc", "commit": "deadbeef", "tests": {"passed": 3, "failed": 0},
        "files_changed": ["a.py"], "risks": [], "next_recommendation": ""})
    assert v["valid"] is False
    assert v["reason"].startswith("commit_not_in_repo")


def test_a_handoff_claiming_files_the_commit_does_not_contain_is_refused(tmp_path):
    repo = _repo(tmp_path, PLAN)
    sha = _commit(repo, {"real.py": "x = 1\n"})
    v = ps.validate_handoff(repo, {
        "block_id": "abc", "commit": sha, "tests": {"passed": 1, "failed": 0},
        "files_changed": ["real.py", "imaginary.py"], "risks": [],
        "next_recommendation": ""})
    assert v["valid"] is False
    assert "imaginary.py" in v["reason"]


def test_a_truthful_handoff_validates(tmp_path):
    repo = _repo(tmp_path, PLAN)
    sha = _commit(repo, {"real.py": "x = 1\n"})
    v = ps.validate_handoff(repo, {
        "block_id": "abc", "commit": sha, "tests": {"passed": 4, "failed": 0},
        "files_changed": ["real.py"], "risks": ["none"], "next_recommendation": "next"})
    assert v["valid"] is True
    assert v["commit"] == sha


def test_failing_tests_block_completion(tmp_path):
    repo = _repo(tmp_path, PLAN)
    sha = _commit(repo, {"real.py": "x = 1\n"})
    v = ps.validate_handoff(repo, {
        "block_id": "abc", "commit": sha, "tests": {"passed": 2, "failed": 1},
        "files_changed": ["real.py"], "risks": [], "next_recommendation": ""})
    assert v["valid"] is False
    assert v["reason"].startswith("tests_failed")


def test_a_handoff_for_a_different_block_does_not_close_this_one(tmp_path):
    repo = _repo(tmp_path, PLAN)
    sha = _commit(repo, {"real.py": "x = 1\n"})
    v = ps.validate_handoff(repo, {
        "block_id": "other", "commit": sha, "tests": {"passed": 1, "failed": 0},
        "files_changed": ["real.py"], "risks": [], "next_recommendation": ""},
        expect_block="mine")
    assert v["valid"] is False
    assert v["reason"].startswith("handoff_for_another_block")


@pytest.mark.parametrize("bad", [None, {"_malformed": True}, {"commit": "x"}])
def test_missing_or_malformed_handoffs_are_refused(tmp_path, bad):
    assert ps.validate_handoff(_repo(tmp_path, PLAN), bad)["valid"] is False


# ── the decision ───────────────────────────────────────────────────────────

def test_a_busy_agent_is_left_alone(tmp_path):
    d = ps.decide("pilot", _repo(tmp_path, PLAN), agent_busy=True)
    assert d["action"] == ps.AWAIT_AGENT
    assert d["reason"] == "agent_working"


def test_an_idle_agent_with_an_open_roadmap_gets_the_next_block(tmp_path):
    d = ps.decide("pilot", _repo(tmp_path, PLAN), agent_busy=False)
    assert d["action"] == ps.CONTINUE
    assert d["block"]["title"] == "add a unit test for the parser"


def test_an_unverified_block_keeps_the_agent_on_it(tmp_path):
    repo = _repo(tmp_path, PLAN)
    plan = ps.parse_plan(repo)
    first = [b for b in plan["blocks"] if not b["done"]][0]
    from core.control_plane import store
    conn = store.connect()
    ps._conn(conn)
    ps._save_state("pilot", conn, current_block=first["id"])
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.AWAIT_AGENT
    assert d["reason"] == "no_handoff"
    assert "checks out against git" in d["detail"]


def _release_fallback(conn, project="pilot"):
    """Simulate the review loop having been given its chance and failed, which is
    the only condition under which the supervisor may pick a block."""
    conn.execute("CREATE TABLE IF NOT EXISTS wake_loop_watch (event_id INTEGER, "
                 "target TEXT, project_id TEXT, delivered_ts REAL, delivered_at TEXT, "
                 "rewoken INTEGER, rewoken_ts REAL, rewoken_event_id INTEGER, "
                 "escalated INTEGER, escalated_ts REAL, escalated_event_id INTEGER, "
                 "resolved INTEGER, resolved_reason TEXT, resolved_ts REAL)")
    conn.execute("INSERT INTO wake_loop_watch (event_id,target,project_id,delivered_ts,"
                 "delivered_at,rewoken,escalated,resolved) VALUES (1,'t:0.0',?,?,?,1,1,NULL)",
                 (project, ps.now_ts() - 100, "2026-08-27T19:00:00+00:00"))
    conn.commit()


def test_a_verified_block_advances_to_the_next_one(tmp_path):
    repo = _repo(tmp_path, PLAN)
    plan = ps.parse_plan(repo)
    open_blocks = [b for b in plan["blocks"] if not b["done"]]
    first, second = open_blocks[0], open_blocks[1]
    sha = _commit(repo, {"real.py": "x = 1\n"})
    _write_handoff(repo, block_id=first["id"], commit=sha,
                   tests={"passed": 5, "failed": 0, "command": "npm test"},
                   files_changed=["real.py"], risks=[], next_recommendation="do the next")
    from core.control_plane import store
    conn = store.connect()
    ps._conn(conn)
    ps._save_state("pilot", conn, current_block=first["id"])
    _release_fallback(conn)
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.CONTINUE
    assert d["completed_block"] == first["id"]
    assert d["block"]["id"] == second["id"]


# ── acting, with the side effects injected ─────────────────────────────────

class _Ctrl:
    def __init__(self, busy=False):
        self.busy, self.sent, self.checkpoints = busy, [], []

    def agent_busy(self, target):
        return self.busy

    def enqueue_continuation(self, *, target, text, project, repo):
        self.sent.append({"target": target, "text": text, "project": project})
        return {"ok": True}

    def checkpoint(self, *, project, kind, reason, detail=""):
        self.checkpoints.append({"project": project, "kind": kind, "reason": reason})
        return {"ok": True}


def test_a_tick_dispatches_the_recorded_block_verbatim(tmp_path):
    repo = _repo(tmp_path, PLAN)
    ctrl = _Ctrl()
    out = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    assert out["acted"] is True and out["action"] == ps.CONTINUE
    assert len(ctrl.sent) == 1
    text = ctrl.sent[0]["text"]
    assert "add a unit test for the parser" in text          # verbatim
    assert ".owner-os/handoff.json" in text                  # contract stated
    assert "owner gate" in text.lower()                      # limits stated
    assert ctrl.checkpoints == []                            # no wake needed to continue


def test_a_gate_raises_a_checkpoint_instead_of_dispatching(tmp_path):
    repo = _repo(tmp_path, "# P\n\n## Blocked on a live browser session\n- [ ] probe\n")
    ctrl = _Ctrl()
    out = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    assert out["action"] == ps.OWNER_GATE
    assert ctrl.sent == []
    assert ctrl.checkpoints[0]["kind"] == "owner_gate"


def test_a_busy_agent_is_never_sent_a_second_block(tmp_path):
    ctrl = _Ctrl(busy=True)
    out = ps.tick("pilot", _repo(tmp_path, PLAN), target="pilot:0.0", ctrl=ctrl)
    assert out["acted"] is False
    assert ctrl.sent == []


def test_a_project_that_was_not_enabled_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_SUPERVISOR_PROJECTS", "")
    ctrl = _Ctrl()
    out = ps.tick("pilot", _repo(tmp_path, PLAN), target="pilot:0.0", ctrl=ctrl)
    assert out == {"acted": False, "reason": "project_not_enabled", "project": "pilot"}
    assert ctrl.sent == [] and ctrl.checkpoints == []


def test_the_full_loop_completes_a_block_and_assigns_the_next_without_a_wake(tmp_path):
    """The pilot criterion: block done -> verified -> next block dispatched, with
    no owner message and no ChatGPT wake anywhere in the path."""
    repo = _repo(tmp_path, PLAN)
    ctrl = _Ctrl()
    first = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    block1 = first["block"]

    sha = _commit(repo, {"parser_test.py": "def test(): pass\n"})
    _write_handoff(repo, block_id=block1["id"], commit=sha,
                   tests={"passed": 7, "failed": 0, "command": "pytest"},
                   files_changed=["parser_test.py"], risks=[],
                   next_recommendation="wire the adapter")

    from core.control_plane import store
    _release_fallback(store.connect())
    second = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    assert second["action"] == ps.CONTINUE
    assert second["block"]["title"] == "wire the second adapter"
    assert len(ctrl.sent) == 2
    assert ctrl.checkpoints == []                    # no wake in the ordinary loop

    log = ps.evidence_log("pilot")
    assert log[0]["validated"] is True
    assert log[0]["commit"] == sha


def test_evidence_is_recorded_even_when_the_next_step_is_a_gate(tmp_path):
    """Finishing the last block must still bank its evidence before pausing."""
    plan = "# P\n\n## Ready\n- [ ] only block\n"
    repo = _repo(tmp_path, plan)
    ctrl = _Ctrl()
    first = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    sha = _commit(repo, {"x.py": "1\n"})
    _write_handoff(repo, block_id=first["block"]["id"], commit=sha,
                   tests={"passed": 1, "failed": 0}, files_changed=["x.py"],
                   risks=[], next_recommendation="")
    from core.control_plane import store
    _release_fallback(store.connect())
    out = ps.tick("pilot", repo, target="pilot:0.0", ctrl=ctrl)
    assert out["action"] == ps.OWNER_GATE
    assert out["reason"] == "roadmap_complete"
    assert ctrl.checkpoints[-1]["kind"] == "owner_gate"


def test_status_reports_only_enabled_projects(tmp_path):
    s = ps.status()
    assert s["enabled_projects"] == ["pilot"]
    assert "authoritative between checkpoints" in s["note"]


# ── the loop, and the fact that it is dormant by default ───────────────────

def test_the_loop_does_nothing_when_no_project_is_enabled(monkeypatch):
    import asyncio
    monkeypatch.setenv("PROJECT_SUPERVISOR_PROJECTS", "")
    seen = []
    asyncio.run(ps.run_loop(log=lambda l, m: seen.append(m),
                            sleep=lambda _s: (_ for _ in ()).throw(AssertionError(
                                "the loop must not sleep-and-continue when dormant"))))
    assert seen == ["project supervisor: no projects enabled (dormant)"]


def test_the_loop_reports_a_state_change_once_not_every_tick(tmp_path, monkeypatch):
    import asyncio
    repo = _repo(tmp_path, PLAN)
    monkeypatch.setattr(ps, "project_targets", lambda: {"pilot": ("pilot:0.0", repo)})
    monkeypatch.setattr(ps, "tick", lambda *a, **k: {"action": ps.AWAIT_AGENT,
                                                     "reason": "agent_working"})
    seen, calls = [], {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError

    try:
        asyncio.run(ps.run_loop(log=lambda l, m: seen.append(m), sleep=fake_sleep))
    except asyncio.CancelledError:
        pass
    assert len([m for m in seen if "await_agent" in m]) == 1


def test_a_failing_tick_never_kills_the_loop(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(ps, "project_targets", lambda: {"pilot": ("t:0.0", "/nope")})

    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(ps, "tick", boom)
    seen, calls = [], {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    try:
        asyncio.run(ps.run_loop(log=lambda l, m: seen.append(m), sleep=fake_sleep))
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2
    assert any("tick error" in m for m in seen)


def test_only_enabled_projects_are_targeted(monkeypatch):
    """project_targets reads the agent registry, but must filter to the
    allowlist - a registry full of agents is not an authorisation."""
    from core.control_plane import store
    conn = store.connect()
    store.init_db(conn)
    conn.execute("INSERT OR REPLACE INTO agent (target, session, project_id, cwd) "
                 "VALUES ('pilot:0.0','pilot','pilot','/tmp/pilot')")
    conn.execute("INSERT OR REPLACE INTO agent (target, session, project_id, cwd) "
                 "VALUES ('other:0.0','other','other','/tmp/other')")
    conn.commit()
    targets = ps.project_targets()
    assert set(targets) == {"pilot"}


# ── ChatGPT review is PRIMARY; this module is the safety net ───────────────
# An agent finishing a block must reach the reviewer first. The supervisor may
# only pick the next block after the review loop has been given its chance and
# demonstrably failed - otherwise product review is silently replaced by
# roadmap order, which is a different (worse) system.

def _verified_state(tmp_path, conn):
    repo = _repo(tmp_path, PLAN)
    plan = ps.parse_plan(repo)
    first = [b for b in plan["blocks"] if not b["done"]][0]
    sha = _commit(repo, {"real.py": "x = 1\n"})
    _write_handoff(repo, block_id=first["id"], commit=sha,
                   tests={"passed": 3, "failed": 0}, files_changed=["real.py"],
                   risks=[], next_recommendation="")
    ps._conn(conn)
    ps._save_state("pilot", conn, current_block=first["id"])
    return repo


def _watch_row(conn, *, project, delivered_ts, rewoken=0, escalated=0, resolved=None):
    conn.execute("CREATE TABLE IF NOT EXISTS wake_loop_watch (event_id INTEGER, "
                 "target TEXT, project_id TEXT, delivered_ts REAL, delivered_at TEXT, "
                 "rewoken INTEGER, rewoken_ts REAL, rewoken_event_id INTEGER, "
                 "escalated INTEGER, escalated_ts REAL, escalated_event_id INTEGER, "
                 "resolved INTEGER, resolved_reason TEXT, resolved_ts REAL)")
    conn.execute("INSERT INTO wake_loop_watch (event_id,target,project_id,delivered_ts,"
                 "delivered_at,rewoken,escalated,resolved) VALUES (1,'t:0.0',?,?,?,?,?,?)",
                 (project, delivered_ts, "2026-08-27T19:00:00+00:00", rewoken,
                  escalated, resolved))
    conn.commit()


def test_a_finished_block_waits_for_the_reviewer_by_default(tmp_path):
    from core.control_plane import store
    conn = store.connect()
    repo = _verified_state(tmp_path, conn)
    _watch_row(conn, project="pilot", delivered_ts=ps.now_ts() - 60)
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.DEFER_TO_REVIEW
    assert "review_in_progress" in d["reason"]
    assert "ChatGPT review owns the next task" in d["detail"]


def test_no_wake_yet_is_not_a_review_failure(tmp_path):
    """The block just finished; the reviewer has not even been asked."""
    from core.control_plane import store
    conn = store.connect()
    ps._conn(conn)
    r = ps.review_failed("pilot", conn=conn)
    assert r["failed"] is False
    assert r["reason"] == "no_wake_watched_yet"


def test_an_escalated_wake_releases_the_fallback(tmp_path):
    from core.control_plane import store
    conn = store.connect()
    repo = _verified_state(tmp_path, conn)
    _watch_row(conn, project="pilot", delivered_ts=ps.now_ts() - 100, rewoken=1,
               escalated=1)
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.CONTINUE
    assert d["fallback_reason"].startswith("review_escalated_without_progress")


def test_silence_past_the_grace_window_releases_the_fallback(tmp_path):
    from core.control_plane import store
    conn = store.connect()
    repo = _verified_state(tmp_path, conn)
    _watch_row(conn, project="pilot",
               delivered_ts=ps.now_ts() - ps.REVIEW_GRACE_SECS - 60)
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.CONTINUE
    assert d["fallback_reason"].startswith("no_progress_")


def test_review_that_produced_progress_keeps_the_fallback_shut(tmp_path):
    from core.control_plane import store
    conn = store.connect()
    repo = _verified_state(tmp_path, conn)
    _watch_row(conn, project="pilot",
               delivered_ts=ps.now_ts() - ps.REVIEW_GRACE_SECS - 60, resolved=1)
    d = ps.decide("pilot", repo, agent_busy=False, conn=conn)
    assert d["action"] == ps.DEFER_TO_REVIEW
    assert d["reason"] == "review_produced_progress"


def test_a_failed_wake_delivery_releases_the_fallback(tmp_path):
    """If the turn never landed, there is no reviewer to wait for."""
    from core.control_plane import store
    conn = store.connect()
    ps._conn(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS wake_delivery (id INTEGER PRIMARY KEY "
                 "AUTOINCREMENT, ts REAL, at TEXT, source TEXT, event_id INTEGER, "
                 "delivered INTEGER, reason TEXT, conversation TEXT, route_key TEXT)")
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,"
                 "conversation,route_key) VALUES (?,?,'companion',1,0,'failed','c','pilot')",
                 (ps.now_ts() - 30, "2026-08-27T19:00:00+00:00"))
    conn.commit()
    assert ps.review_failed("pilot", conn=conn)["failed"] is True


def test_an_unreadable_review_state_defers_rather_than_acting(tmp_path, monkeypatch):
    """Unknown must never mean 'go ahead' for a module that types into panes."""
    from core.control_plane import store
    conn = store.connect()

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    monkeypatch.setattr(ps, "_conn", lambda c=None: (_Boom(), False))
    r = ps.review_failed("pilot", conn=conn)
    assert r["failed"] is False
    assert r["reason"].startswith("review_state_unreadable")
