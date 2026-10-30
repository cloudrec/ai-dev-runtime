"""A finished Full Cycle project's idle agent is not a stalled one (2026-10-30).

TaskBoard: project_completed 76022 (acceptance VERIFIED_DONE, no pending review), then
agent_watch `work_stopped_incomplete` 76026 and the loop watchdog's
`wake_loop_no_progress` 76047 / `wake_loop_stalled` 76065 about the same idle agent.
Pinned here: the three paths (agent_watch emission, closed-loop watch resolution,
delayed-wake retirement) consult engine.settled_project_for_agent, and that answer is
narrow — active, failed, reviewed-but-unapplied and re-tasked projects, questions,
blockers and crashes all keep their detection.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from core import task_contract as tc
from core.control_plane import api as cp
from core.control_plane.store import now_ts
from core.fullcycle import engine as fe
from core.fullcycle import spec as specs

AGENT = "tb-agent"
TARGET = f"{AGENT}:0.0"
PROJECT = "tb-proj"


class _Adapter:
    name, target, owns_agent = "agent_control", AGENT, False


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    root = tmp_path / PROJECT
    root.mkdir()
    specs.create_project(PROJECT, name=PROJECT, root=str(root), executor="claude")
    specs.put_spec(PROJECT, {"title": "t", "goal": "g", "acceptance_criteria": ["ok"],
                             "tasks": [{"key": "acceptance", "title": "a", "goal": "g",
                                        "acceptance_criteria": ["ok"]}]}, by="test")
    return tmp_path


def _project(state=tc.VERIFIED_DONE, *, done=True, review="decided", applied=1,
             done_ago=3600.0):
    """A project whose one task was dispatched to AGENT and reached `state`."""
    e = fe.Engine(PROJECT, _Adapter(), holder="t")
    e.add_task({"key": "acceptance", "title": "a", "goal": "g",
                "acceptance_criteria": ["ok"]}, spec_version=1, origin="spec")
    (t,) = e._tasks()
    e._set(t, handle={"target": AGENT, "task_id": t["id"], "attempt": 1})
    path = {tc.VERIFIED_DONE: [tc.WORKING, tc.AGENT_DONE, tc.VERIFYING, tc.VERIFIED_DONE],
            tc.WORKING: [tc.WORKING], tc.CANCELLED: [tc.CANCELLED]}[state]
    for s in path:
        tc.transition(t["contract_id"], s, by="test", conn=e.conn,
                      evidence={"tests": [{"passed": True}], "review": {"by": "t"}})
    if review:
        e.conn.execute("INSERT INTO fc_review (project_id, task_id, attempt, kind, status,"
                       " applied, created_ts) VALUES (?,?,1,'review',?,?,?)",
                       (PROJECT, t["id"], review, applied, now_ts()))
    if done:
        e.conn.execute("INSERT INTO fc_project_done VALUES (?,?,?,?)",
                       (PROJECT, 1, "{}", now_ts() - done_ago))
    e.conn.commit()
    return e


def _delivery(db, ago: float):
    c = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    c.execute("CREATE TABLE IF NOT EXISTS deliveries (idempotency_key TEXT, target TEXT,"
              " action TEXT, result TEXT, created_at TEXT, created_ts REAL)")
    c.execute("INSERT INTO deliveries VALUES ('k', ?, 'agent_send', '{}', '', ?)",
              (TARGET, now_ts() - ago))
    c.commit()
    c.close()


# ── the answer itself ─────────────────────────────────────────────────────────────
def test_a_completed_project_settles_its_agent(db):
    _project()
    assert fe.settled_project_for_agent(TARGET) == PROJECT
    assert fe.settled_project_for_agent(AGENT) == PROJECT          # wake_loop events


@pytest.mark.parametrize("kw", [
    {"state": tc.WORKING, "done": False, "review": None},          # active
    {"state": tc.CANCELLED, "done": False, "review": None},        # failed / cancelled
    {"review": "pending"},                                         # review outstanding
    {"review": "decided", "applied": 0},                           # decided, not applied
])
def test_unfinished_projects_never_settle(db, kw):
    _project(**kw)
    assert fe.settled_project_for_agent(TARGET) is None


def test_an_agent_unknown_to_full_cycle_never_settles(db):
    _project()
    assert fe.settled_project_for_agent("someone-else:0.0") is None
    assert fe.settled_project_for_agent("") is None


def test_new_work_after_completion_unsettles(db):
    _project(done_ago=3600)
    _delivery(db, ago=60)                    # ChatGPT sent the agent something new
    assert fe.settled_project_for_agent(TARGET) is None


def test_a_new_turn_after_completion_unsettles_but_the_closing_turn_does_not(db):
    _project(done_ago=30)                    # the closing turn races the approval
    cp.append_event("claude_hook", "agent_turn_stopped", agent_id="session:abc",
                    project_id=PROJECT)
    assert fe.settled_project_for_agent(TARGET) == PROJECT
    with sqlite3.connect(os.environ["CONTROL_PLANE_DB"]) as c:
        c.execute("UPDATE fc_project_done SET ts=?", (now_ts() - 3600,))
    assert fe.settled_project_for_agent(TARGET) is None   # a turn long after: new work


# ── agent_watch ──────────────────────────────────────────────────────────────────
def _scan_to_rest(tail, *, state="idle"):
    from core import agent_watch as aw
    events = []

    def emit(source, etype, **kw):
        events.append(etype)
        return {"event_id": len(events)}
    t0 = now_ts() - 4000          # the pane came to rest before the project completed
    agent = {"target": TARGET, "alive": True, "is_agent": True, "cwd": "/x"}
    aw.scan(agents=[{**agent, "state": "working"}], read_fn=lambda t: tail,
            emit_fn=emit, now=t0)
    r = aw.scan(agents=[{**agent, "state": state}], read_fn=lambda t: tail,
                emit_fn=emit, now=t0 + aw.QUIESCENT_SECS + 5)
    return events, r


REST = "Report written.\nI notified the tech lead with stage_done.py (event 76017).\n"


def test_agent_watch_is_silent_about_a_settled_agent_at_rest(db):
    _project()
    events, r = _scan_to_rest(REST)
    assert events == []
    assert {"target": TARGET, "why": "fullcycle_project_settled"} in r["skipped"]


def test_agent_watch_still_reports_an_active_projects_stop(db):
    _project(state=tc.WORKING, done=False, review=None)
    events, _ = _scan_to_rest(REST)
    assert events == ["work_stopped_incomplete"]


def test_agent_watch_still_reports_a_question_from_a_settled_agent(db):
    _project()
    events, _ = _scan_to_rest(REST + "Paused, waiting for your input on the port.\n")
    assert events and events[-1] == "agent_waiting_input"


# ── closed-loop watchdog (delayed / duplicate events) ─────────────────────────────
def _watch(etype="task_completed", source="fullcycle"):
    from core import closed_loop_wake as clw
    eid = cp.append_event(source, etype, agent_id=AGENT, project_id=PROJECT,
                          severity="high")
    clw.register_delivery(event_id=eid, target=AGENT, project_id=PROJECT,
                          event_type=etype, now=now_ts() - 1000)
    return clw, eid


def test_a_watch_on_a_settled_agent_resolves_silently(db):
    _project()
    clw, eid = _watch()
    out = clw.deregister_resolved(now=now_ts())
    assert out == [{"event_id": eid, "target": AGENT, "reason": "fullcycle_project_settled"}]
    emitted = []
    clw.slo_scan(now=now_ts() + 10_000, emit_fn=lambda *a, **k: emitted.append(a) or {})
    assert emitted == []                     # no wake_loop_no_progress / stalled


def test_a_watch_on_an_active_project_still_escalates(db):
    _project(state=tc.WORKING, done=False, review=None)
    clw, eid = _watch("work_stopped_incomplete", "agent_watch")
    assert all(d["reason"] != "fullcycle_project_settled"
               for d in clw.deregister_resolved(now=now_ts()))


def test_a_crash_watch_is_never_resolved_by_settlement(db):
    _project()
    clw, eid = _watch("agent_process_failed", "agent_watch")
    assert all(d["reason"] != "fullcycle_project_settled"
               for d in clw.deregister_resolved(now=now_ts()))


# ── wake bridge: a stop wake decided before completion is never delivered late ────
def test_a_delayed_stop_wake_is_retired_but_project_completed_is_delivered(db, monkeypatch):
    from core import wake_bridge as wb
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    _project()
    stop = cp.append_event("agent_watch", "work_stopped_incomplete", agent_id=TARGET,
                           project_id=PROJECT, severity="high")
    done = cp.append_event("fullcycle", "task_completed", agent_id=AGENT,
                           project_id=PROJECT, severity="high")
    for eid, et in ((stop, "work_stopped_incomplete"), (done, "task_completed")):
        wb.record({"wake": True, "reason": "test", "actionable": False}, event_id=eid,
                  severity="high", event_type=et, project_id=PROJECT,
                  agent_id=TARGET if eid == stop else AGENT)
    out = {e["event_id"]: e["reason"] for e in wb.expire_stale()}
    assert out == {stop: "fullcycle_project_settled"}


# ── review findings (2026-10-30) ──────────────────────────────────────────────────
def test_the_owner_typing_into_the_pane_after_completion_unsettles(db):
    """No delivery record and no turn end yet: only the pane moved."""
    _project(done_ago=3600)
    with sqlite3.connect(os.environ["CONTROL_PLANE_DB"]) as c:
        c.execute("CREATE TABLE IF NOT EXISTS agent_watch_state (target TEXT PRIMARY KEY,"
                  " cls TEXT, digest TEXT, at TEXT, ts REAL, digest_since REAL)")
        c.execute("INSERT OR REPLACE INTO agent_watch_state (target, cls, digest_since)"
                  " VALUES (?, 'idle', ?)", (TARGET, now_ts() - 60))
    assert fe.settled_project_for_agent(TARGET) is None


def test_a_pane_unchanged_since_before_completion_stays_settled(db):
    _project(done_ago=3600)
    with sqlite3.connect(os.environ["CONTROL_PLANE_DB"]) as c:
        c.execute("CREATE TABLE IF NOT EXISTS agent_watch_state (target TEXT PRIMARY KEY,"
                  " cls TEXT, digest TEXT, at TEXT, ts REAL, digest_since REAL)")
        c.execute("INSERT OR REPLACE INTO agent_watch_state (target, cls, digest_since)"
                  " VALUES (?, 'idle', ?)", (TARGET, now_ts() - 4000))
    assert fe.settled_project_for_agent(TARGET) == PROJECT


def test_an_idle_notification_after_completion_is_not_new_work(db):
    _project(done_ago=3600)
    cp.append_event("claude_hook", "agent_turn_stopped", agent_id="session:abc",
                    project_id=PROJECT, payload={"hook_event": "Notification",
                                                 "notification_type": "idle_prompt"})
    assert fe.settled_project_for_agent(TARGET) == PROJECT


def test_a_delivery_inside_the_grace_window_still_unsettles(db):
    _project(done_ago=30)
    _delivery(db, ago=10)
    assert fe.settled_project_for_agent(TARGET) is None


def test_a_similarly_named_session_does_not_count_as_this_agents_delivery(db):
    _project(done_ago=3600)
    c = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    c.execute("CREATE TABLE deliveries (idempotency_key TEXT, target TEXT, action TEXT,"
              " result TEXT, created_at TEXT, created_ts REAL)")
    c.execute("INSERT INTO deliveries VALUES ('k','tb-agentX:0.0','agent_send','{}','',?)",
              (now_ts(),))
    c.commit()
    c.close()
    assert fe.settled_project_for_agent(TARGET) == PROJECT


def test_a_stalled_loop_about_an_active_project_is_still_delivered(db, monkeypatch):
    from core import wake_bridge as wb
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    _project(state=tc.WORKING, done=False, review=None)
    eid = cp.append_event("closed_loop_wake", "wake_loop_stalled", agent_id=AGENT,
                          project_id=PROJECT, severity="critical")
    wb.record({"wake": True, "reason": "test", "actionable": True}, event_id=eid,
              severity="critical", event_type="wake_loop_stalled", project_id=PROJECT,
              agent_id=AGENT)
    assert eid not in {e["event_id"] for e in wb.expire_stale()}
