"""An unchanged owner gate wakes the chat once, not every hour.

`agent_watch` re-publishes an unanswered owner prompt every REMINDER_SECS as a NEW event
with the SAME digest. vtb:0.0 has shown one owner prompt (digest a3f420d6b8b74117) since
2026-10-19; each hourly reminder (74802, 74822, 74846, 74889 on 2026-10-29) was woken
into the owner-os chat, then re-woken and escalated (74914). Only the owner can answer it.

What must survive, and is asserted here: a changed gate, a re-asked gate, a gate whose
earlier wake never landed, another agent, a non-gate `agent_waiting_input`, the daily
re-alert, and fail-open on anything unreadable.
"""
from __future__ import annotations

import json

import pytest

from core import wake_bridge as wb

T = "vtb:0.0"
D = "a3f420d6b8b74117"
PROMPT = "agent_prompt_needs_response"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat("https://chatgpt.com/c/default-test-chat")
    yield


def _conn(with_state=True):
    from core.control_plane.api import _c
    conn, _own = _c(None)
    conn.execute("""CREATE TABLE IF NOT EXISTS event (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ts_epoch REAL, source TEXT,
        type TEXT, entity_type TEXT, entity_id TEXT, payload TEXT,
        evidence_ref TEXT, correlation_id TEXT,
        project_id TEXT, agent_id TEXT, severity TEXT DEFAULT 'info',
        owner_action_required INTEGER DEFAULT 0, action_taken TEXT,
        dedup_key TEXT, supersedes INTEGER, resolves INTEGER)""")
    if with_state:
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_watch_state (
            target TEXT PRIMARY KEY, cls TEXT, digest TEXT, at TEXT, ts REAL,
            notified_cls TEXT, notified_digest TEXT, notified_at TEXT, notified_ts REAL,
            emissions INTEGER DEFAULT 0, miss_count INTEGER DEFAULT 0, digest_since REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wake_delivery (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, at TEXT, source TEXT,
        event_id INTEGER, delivered INTEGER, reason TEXT,
        conversation TEXT DEFAULT '', route_key TEXT DEFAULT '')""")
    conn.commit()
    return conn


def _event(conn, eid, *, target=T, etype=PROMPT, cls="owner_prompt", digest=D):
    pl = {"target": target}
    if cls:
        pl["class"] = cls
    if digest:
        pl["digest"] = digest
    conn.execute("INSERT INTO event (id,ts,ts_epoch,source,type,payload,agent_id,severity,"
                 "owner_action_required) VALUES (?,?,?,?,?,?,?,?,1)",
                 (eid, "2026-10-29T00:00:00+00:00", 0.0, "agent_watch", etype,
                  json.dumps(pl), target, "high"))
    conn.commit()


def _delivered(conn, eid, ts, ok=1):
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason) "
                 "VALUES (?,?,?,?,?,?)", (ts, "x", "companion", eid, ok,
                                          "submitted_and_assistant_started_generating"
                                          if ok else "cdp_error:WebSocketTimeoutException"))
    conn.commit()


def _state(conn, *, target=T, cls="owner_prompt", digest=D, since=500.0):
    conn.execute("INSERT OR REPLACE INTO agent_watch_state (target,cls,digest,ts,digest_since) "
                 "VALUES (?,?,?,?,?)", (target, cls, digest, since, since))
    conn.commit()


def _decide(conn, eid, *, now=5000.0, etype=PROMPT, target=T):
    return wb.should_wake(event_id=eid, severity="high", owner_action_required=True,
                          event_type=etype, project_id="vtb_old", agent_id=target,
                          conn=conn, now=now)


def _vtb(conn):
    """The live shape: gate on screen since 500, the first reminder delivered at 1000."""
    _state(conn)
    _event(conn, 1)
    _delivered(conn, 1, 1000.0)
    _event(conn, 2)


def test_the_hourly_reminder_of_an_unchanged_gate_does_not_wake():
    """Fails without the fix: `actionable_waiting_transition`, every hour."""
    conn = _conn()
    _vtb(conn)
    d = _decide(conn, 2)
    assert d["wake"] is False, d
    assert d["reason"] == "owner_gate_unchanged_already_delivered"
    assert d["first_signalled_event_id"] == 1


def test_control_the_first_delivery_of_a_gate_still_wakes():
    conn = _conn()
    _state(conn)
    _event(conn, 1)
    assert _decide(conn, 1)["wake"] is True


def test_a_gate_whose_earlier_wake_never_landed_still_wakes():
    conn = _conn()
    _state(conn)
    _event(conn, 1)
    _delivered(conn, 1, 1000.0, ok=0)
    _event(conn, 2)
    assert _decide(conn, 2)["wake"] is True


def test_a_changed_gate_wakes():
    conn = _conn()
    _vtb(conn)
    _state(conn, digest="ffff000011112222", since=3000.0)
    _event(conn, 3, digest="ffff000011112222")
    assert _decide(conn, 3)["wake"] is True


def test_a_gate_re_asked_after_the_agent_moved_wakes_even_with_the_same_digest():
    """The pane changed and came back: digest_since is after the last delivery."""
    conn = _conn()
    _vtb(conn)
    _state(conn, since=2000.0)
    assert _decide(conn, 2)["wake"] is True


def test_a_pane_no_longer_showing_the_gate_is_not_suppressed():
    conn = _conn()
    _vtb(conn)
    _state(conn, cls="working")
    assert _decide(conn, 2)["wake"] is True


def test_the_gate_is_re_delivered_once_a_day():
    conn = _conn()
    _vtb(conn)
    assert _decide(conn, 2, now=1000.0 + wb.RE_ALERT_SECS)["wake"] is True


def test_another_agents_delivery_does_not_count():
    conn = _conn()
    _state(conn, target="other:0.0")
    _event(conn, 1, target="other:0.0")
    _delivered(conn, 1, 1000.0)
    _state(conn)
    _event(conn, 2)
    assert _decide(conn, 2)["wake"] is True


def test_an_unchanged_blocker_follows_the_same_rule():
    conn = _conn()
    _state(conn, cls="blocker")
    _event(conn, 1, etype="agent_waiting_input", cls="blocker")
    _delivered(conn, 1, 1000.0)
    _event(conn, 2, etype="agent_waiting_input", cls="blocker")
    d = _decide(conn, 2, etype="agent_waiting_input")
    assert d["wake"] is False and d["reason"] == "owner_gate_unchanged_already_delivered"


def test_a_waiting_input_without_a_gate_digest_is_untouched():
    """stall_doctor's escalations and other sources carry no class/digest."""
    conn = _conn()
    _state(conn, cls="blocker")
    _event(conn, 1, etype="agent_waiting_input", cls=None, digest=None)
    _delivered(conn, 1, 1000.0)
    _event(conn, 2, etype="agent_waiting_input", cls=None, digest=None)
    assert _decide(conn, 2, etype="agent_waiting_input")["wake"] is True


def test_an_unreadable_state_fails_open():
    conn = _conn(with_state=False)
    _event(conn, 1)
    _delivered(conn, 1, 1000.0)
    _event(conn, 2)
    assert _decide(conn, 2)["wake"] is True
