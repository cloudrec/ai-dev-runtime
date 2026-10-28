"""STOP -> wake -> ChatGPT -> next task, without the owner poking the chat.

Pins the two defects behind the 2026-10-28 gaika-country-ru stalls (stops at 02:25,
03:26 and 04:45 sat 60, 79 and 35+ minutes; owner_intervention at 02:25 and 03:21):

  1. a wake the page TOOK but whose user turn was never observed
     (`user_turn_not_observed_after_send`, event 73945) is latched — correctly never
     re-sent — and was never watched, so nothing ever followed it up;
  2. the SLO watchdog treated the unanswered `classify_scope` discovery gate that every
     unmapped agent carries as "the pane is waiting on the owner", resolving every
     watch for those agents within a second (`pane_awaiting_owner`).

The chain below runs the REAL cto.emit -> wake_bridge decision/route/pending ->
tools/wake_companion.tick -> closed_loop_wake.slo_scan; only the browser is faked.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

from core import closed_loop_wake as clw
from core import wake_bridge as wb
from core import wake_routes as wr
from core.control_plane import api, cto

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "wake_companion_stopwake", os.path.join(_HERE, "tools", "wake_companion.py"))
wc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wc)

PANE = "gaika-country-ru:0.0"
CWD = "/opt/gaika-worktrees/ru"
CHAT = "https://chatgpt.com/c/gaika-ru-chat"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("OWNEROS_CLAUDE_PROJECTS", "")
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


@pytest.fixture()
def conn():
    from core.control_plane.store import connect, init_db
    c = connect()
    init_db(c)
    yield c
    c.close()


@pytest.fixture()
def page(monkeypatch):
    """Fake cdp_composer. `mode` decides what the page does with the next phrase:
    'took_unverified' latches like the real composer does once the composer clears,
    then reports the user turn missing; 'ok' is a verified delivery; 'refused' never
    clears the composer (no latch)."""
    mod = types.ModuleType("cdp_composer")
    mod.mode = "ok"
    mod.calls = []

    def submit_phrase(conversation, phrase, *, source, event_id, actionable=False,
                      route_key=""):
        mod.calls.append({"event_id": event_id, "phrase": phrase,
                          "conversation": conversation})
        if mod.mode == "refused":
            r = {"ok": False, "reason": "composer_did_not_clear_after_send"}
        else:
            wb.mark_submitted(event_id, source=source)
            r = ({"ok": True, "reason": "submitted_and_assistant_started_generating"}
                 if mod.mode == "ok" else
                 {"ok": False, "reason": "user_turn_not_observed_after_send"})
        wb.record_delivery(source, event_id=event_id, delivered=r["ok"],
                           reason=r["reason"], conversation=conversation,
                           route_key=route_key)
        return r

    mod.submit_phrase = submit_phrase
    monkeypatch.setitem(sys.modules, "cdp_composer", mod)
    return mod


def _setup(conn, *, gate_kind="classify_scope"):
    api.register_agent(PANE, session="gaika-country-ru", cwd=CWD, conn=conn)
    wr.bind_route("ru", CHAT)
    if gate_kind:
        # exactly what discovery opens for every agent outside the static scope map
        api.open_gate(agent_id=PANE, kind=gate_kind,
                      reason=f"unknown-scope agent at {CWD}", conn=conn)


def _stop(conn):
    """agent_watch's quiescent structural stop for the agent (the STOP completion)."""
    ev = cto.emit("agent_watch", "work_stopped_incomplete", project_id="ru",
                  agent_id=PANE, severity="high", correlation_id=f"agentwatch:{PANE}",
                  payload={"class": "quiescent"}, conn=conn)
    return ev["event_id"]      # cto.emit makes and records the wake decision itself


def _elapse(conn, secs):
    """Move everything recorded so far `secs` into the past — the wall clock the bridge
    reads for its cooldowns stays real, so time passing is simulated on the rows."""
    for table, col in (("event", "ts_epoch"), ("wake_audit", "ts"), ("wake_send", "ts"),
                       ("wake_delivery", "ts"), ("wake_submitted", "ts"),
                       ("wake_loop_watch", "delivered_ts"),
                       ("wake_loop_watch", "rewoken_ts")):
        try:
            conn.execute(f"UPDATE {table} SET {col}={col}-? WHERE {col} IS NOT NULL",
                         (secs,))
        except Exception:  # noqa: BLE001 — table not created yet in this test
            pass
    conn.commit()


def _watch(conn, eid):
    clw._conn(conn)            # the table is created lazily; absent == not watched
    return conn.execute(
        "SELECT delivery_verified, rewoken, rewoken_event_id, escalated, resolved, "
        "resolved_reason FROM wake_loop_watch WHERE event_id=?", (eid,)).fetchone()


def test_two_turn_chain_unverified_stop_is_rewoken_once_then_delivered(conn, page):
    """Turn 1: the stop wake is taken by the page but unverified. Turn 2: the watchdog's
    single re-wake reaches the SAME project chat and is verified. The original event is
    never sent twice."""
    _setup(conn)
    stop_eid = _stop(conn)

    p = wb.pending_wake(conn=conn)
    assert p["pending"] and p["event_id"] == stop_eid and p["conversation"] == CHAT
    assert "trigger=completion" in p["phrase"]

    page.mode = "took_unverified"
    r1 = wc.tick(wb)
    assert r1["ok"] is False and r1["reason"] == "user_turn_not_observed_after_send"
    assert wb.was_submitted(stop_eid)
    w = _watch(conn, stop_eid)
    assert w is not None, "an unverified-but-taken stop wake must be watched"
    assert w[0] == 0, "and recorded as unverified"

    # the latch holds: the original is never offered again
    assert wb.pending_wake(conn=conn).get("event_id") != stop_eid

    # no progress for an SLO window -> exactly one re-wake, as a NEW event
    _elapse(conn, clw.WAKE_LOOP_SLO_SECS + 5)
    t = clw.now_ts()
    s1 = clw.slo_scan(conn=conn, now=t)
    assert [x["event_id"] for x in s1["rewoken"]] == [stop_eid], s1
    assert not s1["deregistered"], "classify_scope is not an owner decision on the pane"
    rewake_eid = s1["rewoken"][0]["rewoken_event_id"]
    assert rewake_eid and rewake_eid != stop_eid

    # repeated scans inside the next window emit nothing more (no storm)
    for k in range(5):
        s = clw.slo_scan(conn=conn, now=t + 10 * (k + 1))
        assert not s["rewoken"] and not s["escalated"]

    p2 = wb.pending_wake(conn=conn)
    assert p2["pending"] and p2["event_id"] == rewake_eid
    assert p2["conversation"] == CHAT, "the re-wake goes to gaika's own chat"
    assert "trigger=loop_watchdog" in p2["phrase"]

    page.mode = "ok"
    r2 = wc.tick(wb)
    assert r2["ok"] is True and r2["event_id"] == rewake_eid
    assert [c["event_id"] for c in page.calls] == [stop_eid, rewake_eid], \
        "each event submitted exactly once"
    # the watchdog never watches its own re-wake (no self-feeding chain)
    assert _watch(conn, rewake_eid) is None

    # ChatGPT acted: the agent moves -> the original watch resolves, no escalation
    cto.emit("agent_watch", "agent_state", agent_id=PANE, project_id="ru", conn=conn)
    s3 = clw.slo_scan(conn=conn, now=t + clw.WAKE_LOOP_SLO_SECS + 5)
    assert not s3["escalated"]
    assert _watch(conn, stop_eid)[4] == 1 and _watch(conn, stop_eid)[5] == \
        "progress_observed"
    assert wb.pending_wake(conn=conn)["pending"] is False


def test_still_no_progress_escalates_once_then_goes_quiet(conn, page):
    _setup(conn)
    stop_eid = _stop(conn)
    page.mode = "took_unverified"
    wc.tick(wb)
    t = clw.now_ts() + clw.WAKE_LOOP_SLO_SECS + 5
    assert clw.slo_scan(conn=conn, now=t)["rewoken"]
    e = clw.slo_scan(conn=conn, now=t + clw.WAKE_LOOP_SLO_SECS + 5)
    assert [x["event_id"] for x in e["escalated"]] == [stop_eid]
    for k in range(5):
        s = clw.slo_scan(conn=conn, now=t + (k + 3) * clw.WAKE_LOOP_SLO_SECS)
        assert not s["rewoken"] and not s["escalated"], "terminal: one escalation"
    n = conn.execute("SELECT COUNT(*) FROM event WHERE source='closed_loop_wake'"
                     ).fetchone()[0]
    assert n == 2, "one re-wake + one escalation, never more"


def test_a_real_owner_decision_gate_still_suppresses(conn, page):
    """The pane-awaiting-owner rule keeps working for gates that ARE decisions."""
    _setup(conn, gate_kind="owner_approval_required")
    stop_eid = _stop(conn)
    page.mode = "ok"
    wc.tick(wb)
    s = clw.slo_scan(conn=conn, now=clw.now_ts() + clw.WAKE_LOOP_SLO_SECS + 5)
    assert not s["rewoken"]
    assert _watch(conn, stop_eid)[5] == "pane_awaiting_owner"


@pytest.mark.parametrize("kind", ["classify_scope", "canary_agent_selection",
                                  "governor_queued_input_stalled", "os_task_failed"])
def test_diagnostic_gates_do_not_suppress(conn, page, kind):
    _setup(conn, gate_kind=kind)
    stop_eid = _stop(conn)
    page.mode = "ok"
    wc.tick(wb)
    s = clw.slo_scan(conn=conn, now=clw.now_ts() + clw.WAKE_LOOP_SLO_SECS + 5)
    assert [x["event_id"] for x in s["rewoken"]] == [stop_eid]


def test_a_refused_send_is_not_latched_not_watched_and_retried(conn, page):
    """Unchanged behaviour: a phrase still in the composer was provably not sent."""
    _setup(conn)
    stop_eid = _stop(conn)
    page.mode = "refused"
    r = wc.tick(wb)
    assert r["ok"] is False
    assert not wb.was_submitted(stop_eid)
    assert _watch(conn, stop_eid) is None


def test_non_actionable_unverified_wake_is_not_watched(conn, page):
    """Only the fast (actionable/lifecycle) class gets the watchdog, as for verified."""
    _setup(conn)
    ev = cto.emit("runtime", "task_failed", project_id="ru", agent_id=PANE,
                  severity="high", conn=conn)
    eid = ev["event_id"]
    page.mode = "took_unverified"
    p = wb.pending_wake(conn=conn)
    assert p["pending"] and p["event_id"] == eid and not p.get("actionable"), p
    wc.tick(wb)
    assert wb.was_submitted(eid)
    assert _watch(conn, eid) is None


def test_register_delivery_is_idempotent_and_keeps_first_verdict(conn):
    _setup(conn, gate_kind="")
    stop_eid = _stop(conn)
    clw.register_delivery(event_id=stop_eid, target=PANE, project_id="ru",
                          event_type="work_stopped_incomplete", verified=False, conn=conn)
    clw.register_delivery(event_id=stop_eid, target=PANE, project_id="ru",
                          event_type="work_stopped_incomplete", verified=True, conn=conn)
    rows = conn.execute("SELECT delivery_verified FROM wake_loop_watch WHERE event_id=?",
                        (stop_eid,)).fetchall()
    assert rows == [(0,)]


def test_latched_helper_fails_closed():
    class NoLatch:
        pass

    class Boom:
        def was_submitted(self, eid):
            raise RuntimeError("db gone")
    assert wc._latched(NoLatch(), 1) is False
    assert wc._latched(Boom(), 1) is False
