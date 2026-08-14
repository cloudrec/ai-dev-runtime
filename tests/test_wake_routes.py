"""Multi-chat wake routing: each event wakes ITS chat, and only its chat.

The single global wake_target was correct while every event was owner-os traffic and wrong
the moment project agents got their own work chats. These tests pin the registry contract:
explicit route -> that conversation; unmapped project -> the owner-os fallback, labelled;
nothing bound -> nothing sent. Resolution happens fresh at delivery time, so a rebind can
never be outrun by a stale pointer, and a coalesce can never fold two chats' wakes together.
"""
from __future__ import annotations

import sqlite3

import pytest

from core import wake_bridge as wb
from core import wake_routes as wr

MESS = "https://chatgpt.com/c/mess-work-chat"
PAY = "https://chatgpt.com/c/payments-work-chat"
OWNER = "https://chatgpt.com/c/owner-os-control"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


def _decide(event_id, project_id, *, now, event_type="agent_waiting_input"):
    """Decide-and-record one actionable wake for a project. Actionable class: its 60s floor
    lets two distinct projects wake in one test without fighting the 900s generic floor."""
    d = wb.should_wake(event_id=event_id, severity="high", event_type=event_type,
                       project_id=project_id, now=now)
    wb.record(d, event_id=event_id, severity="high", event_type=event_type,
              project_id=project_id, now=now)
    return d


# ── resolution: explicit, fallback, fail-closed ────────────────────────────
def test_two_projects_resolve_to_their_own_chats():
    wr.bind_route("mess", MESS)
    wr.bind_route("payment-orchestrator", PAY)
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    m = wr.resolve(project_id="mess")
    p = wr.resolve(project_id="payment-orchestrator")
    assert m["conversation"] == MESS and m["route_reason"] == "explicit_route"
    assert p["conversation"] == PAY and p["route_reason"] == "explicit_route"
    assert m["conversation"] != p["conversation"]


def test_an_unmapped_project_falls_back_to_owner_os_with_an_explicit_reason():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    r = wr.resolve(project_id="gaika-video")
    assert r["bound"] is True and r["conversation"] == OWNER
    assert r["route_key"] == wr.FALLBACK_ROUTE
    assert r["route_reason"] == "unmapped_route:gaika-video"


def test_nothing_bound_anywhere_is_a_refusal_not_a_guess():
    r = wr.resolve(project_id="mess")
    assert r["bound"] is False and r["reason"] == "no_route_bound"
    d = wb.should_wake(event_id=1, severity="critical", project_id="mess", now=1000.0)
    assert d["wake"] is False and d["reason"] == "no_route_bound"


def test_owner_os_events_use_the_owner_os_route_not_a_project_one():
    wr.bind_route("mess", MESS)
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    r = wr.resolve(project_id="")           # runtime/infra traffic has no project
    assert r["conversation"] == OWNER and r["route_reason"] == "owner_os_route"


# ── end to end through the pending queue ───────────────────────────────────
def test_two_projects_wakes_are_offered_with_two_distinct_conversations():
    wr.bind_route("mess", MESS)
    wr.bind_route("payment-orchestrator", PAY)
    _decide(11, "mess", now=10_000.0)
    _decide(12, "payment-orchestrator", now=10_100.0)
    first = wb.pending_wake()
    assert first["pending"] and first["event_id"] == 11
    assert first["conversation"] == MESS and first["route_key"] == "mess"
    wb.mark_submitted(11, source="test")
    wb.acknowledge(11)
    second = wb.pending_wake()
    assert second["pending"] and second["event_id"] == 12
    assert second["conversation"] == PAY and second["route_key"] == "payment-orchestrator"
    assert first["conversation"] != second["conversation"]


def test_a_stale_cached_target_cannot_hijack_a_later_event():
    """The conversation is resolved at DELIVERY time from the registry, never frozen at
    decision time: a rebind between the two reaches the new chat."""
    wr.bind_route("mess", MESS)
    _decide(21, "mess", now=20_000.0)
    wr.bind_route("mess", "https://chatgpt.com/c/mess-rotated")
    p = wb.pending_wake()
    assert p["conversation"] == "https://chatgpt.com/c/mess-rotated"


def test_rebinding_one_project_does_not_alter_another():
    wr.bind_route("mess", MESS)
    wr.bind_route("payment-orchestrator", PAY)
    wr.bind_route("mess", "https://chatgpt.com/c/mess-rotated")
    assert wr.get_route("payment-orchestrator")["conversation"] == PAY
    assert wr.get_route("mess")["conversation"] == "https://chatgpt.com/c/mess-rotated"


def test_an_unresolvable_route_offers_nothing_rather_than_borrowing_a_chat():
    wr.bind_route("mess", MESS)
    _decide(31, "mess", now=30_000.0)
    wr.unbind_route("mess")                 # and no owner-os fallback exists
    p = wb.pending_wake()
    assert p["pending"] is False and p["reason"] == "no_route_bound"


# ── registry mechanics ─────────────────────────────────────────────────────
def test_bind_is_idempotent_and_audited_once():
    assert wr.bind_route("mess", MESS)["action"] == "bind"
    assert wr.bind_route("mess", MESS)["action"] == "unchanged"
    assert wr.bind_route("mess", MESS + "/")["action"] == "unchanged"
    audit = [h for h in wr.route_history() if h["route_key"] == "mess"]
    assert len(audit) == 1


def test_a_malformed_target_is_rejected_before_any_mutation():
    for bad in ("https://chat.com/c/abc", "https://chatgpt.com/gpts", "not-a-url", ""):
        r = wr.bind_route("mess", bad)
        assert r["ok"] is False
    assert wr.get_route("mess") is None
    assert wr.route_history() == []


def test_a_malformed_route_key_is_rejected_before_any_mutation():
    for bad in ("", "  ", "MESS CHAT", "a" * 100, "тест"):
        r = wr.bind_route(bad, MESS)
        assert r["ok"] is False, bad
    assert wr.list_routes() == []


def test_routes_survive_a_restart_because_they_live_in_the_database():
    wr.bind_route("mess", MESS)
    import os
    fresh = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = fresh.execute("SELECT conversation FROM wake_route WHERE route_key='mess'"
                        ).fetchone()
    fresh.close()
    assert row[0] == MESS
    assert wr.get_route("mess")["conversation"] == MESS


# ── legacy single target: migration bridge, not universal routing ──────────
def test_the_legacy_single_target_is_only_a_labelled_fallback():
    conn = sqlite3.connect(__import__("os").environ["CONTROL_PLANE_DB"])
    for stmt in wb._CHAT_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("INSERT INTO wake_target (id,conversation,bound_at,bound_ts,bound_by,note) "
                 "VALUES (1,?,'','','','')", (OWNER,))
    conn.commit(); conn.close()
    r = wr.resolve(project_id="mess")
    assert r["conversation"] == OWNER
    assert r["route_reason"] == "unmapped_route:mess:legacy_single_target"


def test_migration_copies_the_legacy_target_once_and_only_once():
    conn = sqlite3.connect(__import__("os").environ["CONTROL_PLANE_DB"])
    for stmt in wb._CHAT_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute("INSERT INTO wake_target (id,conversation,bound_at,bound_ts,bound_by,note) "
                 "VALUES (1,?,'','','','')", (OWNER,))
    conn.commit(); conn.close()
    assert wr.migrate_legacy_target()["action"] == "bind"
    assert wr.get_route(wr.FALLBACK_ROUTE)["conversation"] == OWNER
    assert wr.migrate_legacy_target()["action"] == "already_migrated"


def test_bind_chat_keeps_registry_and_legacy_row_in_lockstep():
    wb.bind_chat(OWNER)
    assert wr.get_route(wr.FALLBACK_ROUTE)["conversation"] == OWNER
    assert wb.active_chat()["conversation"] == OWNER


# ── coalescing is per route ────────────────────────────────────────────────
def test_generic_wakes_are_never_folded_across_routes():
    wr.bind_route("mess", MESS)
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    # Two generic wakes per route. Force-record them as wake decisions directly; the
    # decision path would refuse the second of each pair on the generic cooldown, and
    # coalescing is exactly the mechanism that makes that refusal safe.
    for eid, project in ((41, "mess"), (42, "mess"), (43, ""), (44, "")):
        wb.record({"wake": True, "reason": "test", "actionable": False},
                  event_id=eid, severity="critical", event_type="task_failed",
                  project_id=project, now=40_000.0 + eid)
    res = wb.coalesce_generic_backlog()
    assert sorted(res["superseded_event_ids"]) == [41, 43]
    assert sorted(res["kept_event_ids"]) == [42, 44]
    # The survivors resolve to their own chats.
    first = wb.pending_wake()
    assert first["event_id"] == 42 and first["conversation"] == MESS
    wb.mark_submitted(42, source="test"); wb.acknowledge(42)
    second = wb.pending_wake()
    assert second["event_id"] == 44 and second["conversation"] == OWNER


# ── the ledger proves every send ───────────────────────────────────────────
def test_the_delivery_ledger_records_route_and_conversation():
    wb.record_delivery("companion", event_id=51, delivered=True,
                       reason="submitted_and_user_turn_appeared",
                       conversation=MESS, route_key="mess")
    row = wb.last_delivery(51)
    assert row["conversation"] == MESS and row["route_key"] == "mess"


def test_one_event_wakes_at_most_one_conversation():
    """No broadcast: resolution is a single target, and the submission latch means even a
    rebind after a send cannot produce a second copy elsewhere."""
    wr.bind_route("mess", MESS)
    _decide(61, "mess", now=60_000.0)
    p = wb.pending_wake()
    assert isinstance(p["conversation"], str)
    wb.mark_submitted(61, source="test")
    wr.bind_route("mess", "https://chatgpt.com/c/mess-rotated")
    again = wb.pending_wake()
    assert again["pending"] is False, "a submitted event is never offered to any chat again"


# ── cooldown refusals get a second hearing (live event 4187) ───────────────
def test_a_cooldown_skipped_actionable_wakes_once_the_floor_clears():
    """An actionable event that arrived 11s behind another actionable send was skipped
    and had no path back until the hourly reminder. pending_wake re-decides it."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(201, "", now=50_000.0)                          # consumes the actionable floor
    wb.mark_submitted(201, source="test"); wb.acknowledge(201)
    d = wb.should_wake(event_id=202, severity="high", event_type="agent_waiting_input",
                       project_id="", now=50_010.0)          # 10s later: floor active
    assert d["reason"] == "actionable_cooldown_active"
    wb.record(d, event_id=202, severity="high", event_type="agent_waiting_input",
              project_id="", now=50_010.0)
    assert wb.pending_wake(now=50_020.0)["pending"] is False  # floor still running
    p = wb.pending_wake(now=50_120.0)                         # floor cleared
    assert p["pending"] is True and p["event_id"] == 202
    assert p["conversation"] == OWNER


def test_redeciding_a_running_cooldown_mints_no_audit_spam():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(211, "", now=60_000.0)
    wb.mark_submitted(211, source="test"); wb.acknowledge(211)
    d = wb.should_wake(event_id=212, severity="high", event_type="agent_waiting_input",
                       project_id="", now=60_010.0)
    wb.record(d, event_id=212, severity="high", event_type="agent_waiting_input",
              project_id="", now=60_010.0)
    import sqlite3, os
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    before = conn.execute("SELECT COUNT(*) FROM wake_audit WHERE event_id=212").fetchone()[0]
    for t in (60_020.0, 60_030.0, 60_040.0):                 # floor still active
        wb.pending_wake(now=t)
    after = conn.execute("SELECT COUNT(*) FROM wake_audit WHERE event_id=212").fetchone()[0]
    assert after == before, "a floor still running must not mint audit rows per poll"


def test_a_submitted_event_is_never_redecided():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(221, "", now=70_000.0)
    wb.mark_submitted(221, source="test"); wb.acknowledge(221)
    d = wb.should_wake(event_id=222, severity="high", event_type="agent_waiting_input",
                       project_id="", now=70_010.0)
    wb.record(d, event_id=222, severity="high", event_type="agent_waiting_input",
              project_id="", now=70_010.0)
    wb.mark_submitted(222, source="out-of-band")              # someone already sent it
    p = wb.pending_wake(now=70_200.0)
    assert p["pending"] is False


# ── a failed delivery benches the event, it does not own the line (event 4214) ─
def test_a_failing_event_backs_off_and_the_next_actionable_gets_its_turn():
    """113 retries against one wedged page starved the actionable behind it. After a
    failure, the failing event steps aside for RETRY_BACKOFF_SECS."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(301, "", now=80_000.0)
    _decide(302, "", now=80_100.0)
    p = wb.pending_wake(now=80_200.0)
    assert p["event_id"] == 301                              # oldest first, as ever
    wb.record_delivery("companion", event_id=301, delivered=False,
                       reason="cdp_error:WebSocketTimeoutException",
                       conversation=OWNER, route_key="owner-os", now=80_210.0)
    p2 = wb.pending_wake(now=80_220.0)
    assert p2["pending"] and p2["event_id"] == 302, "the line must move on"


def test_the_benched_event_returns_after_the_backoff_window():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(311, "", now=90_000.0)
    wb.record_delivery("companion", event_id=311, delivered=False,
                       reason="cdp_error:WebSocketTimeoutException",
                       conversation=OWNER, route_key="owner-os", now=90_010.0)
    assert wb.pending_wake(now=90_100.0)["pending"] is False  # benched
    p = wb.pending_wake(now=90_010.0 + wb.RETRY_BACKOFF_SECS + 5)
    assert p["pending"] and p["event_id"] == 311              # retried, not lost


def test_transient_failure_then_success_is_one_delivery_and_no_duplicate():
    """The retry that finally lands must be the ONLY submission: a pre-send failure sets
    no latch, and success both latches and acknowledges."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(321, "", now=100_000.0)
    wb.record_delivery("companion", event_id=321, delivered=False,
                       reason="renderer_unresponsive",
                       conversation=OWNER, route_key="owner-os", now=100_010.0)
    assert wb.was_submitted(321) is False                     # nothing fired yet
    later = 100_010.0 + wb.RETRY_BACKOFF_SECS + 5
    assert wb.pending_wake(now=later)["event_id"] == 321
    wb.mark_submitted(321, source="companion")
    wb.record_delivery("companion", event_id=321, delivered=True,
                       reason="submitted_and_user_turn_appeared",
                       conversation=OWNER, route_key="owner-os", now=later + 10)
    wb.acknowledge(321)
    assert wb.pending_wake(now=later + 20)["pending"] is False


# ── one agent, one pending ring (incident 2026-08-14: 17 stale copies) ─────
def test_older_actionables_for_the_same_agent_are_superseded_by_the_newest():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    for eid, t in ((401, 110_000.0), (402, 110_100.0), (403, 110_200.0)):
        d = wb.should_wake(event_id=eid, severity="high",
                           event_type="agent_waiting_input", project_id="",
                           correlation_id="agentwatch:gv:0.0", now=t)
        wb.record(d, event_id=eid, severity="high", event_type="agent_waiting_input",
                  project_id="", correlation_id="agentwatch:gv:0.0", now=t)
    p = wb.pending_wake(now=110_300.0)
    assert p["pending"] and p["event_id"] == 403, "the NEWEST copy is the queue"
    import sqlite3, os
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    retired = conn.execute(
        "SELECT COUNT(*) FROM wake_audit WHERE event_id IN (401,402) "
        "AND superseded_reason='superseded_by_newer_actionable_same_agent'").fetchone()[0]
    audited = conn.execute(
        "SELECT COUNT(*) FROM wake_coalesce_audit WHERE event_id IN (401,402)").fetchone()[0]
    assert retired == 2 and audited == 2, "retired with provenance, never deleted"


def test_a_stale_copy_cannot_head_of_line_block_a_fresh_event_of_another_agent():
    """The 4400 shape: stale duplicates of agent A queued ahead of agent B's fresh event.
    After supersede, B waits behind exactly ONE ring of A, not seventeen."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    for eid, t in ((411, 120_000.0), (412, 120_100.0), (413, 120_200.0)):
        d = wb.should_wake(event_id=eid, severity="high",
                           event_type="agent_waiting_input", project_id="",
                           correlation_id="waiting:agentA:0.0", now=t)
        wb.record(d, event_id=eid, severity="high", event_type="agent_waiting_input",
                  project_id="", correlation_id="waiting:agentA:0.0", now=t)
    d = wb.should_wake(event_id=414, severity="high", event_type="agent_waiting_input",
                       project_id="", correlation_id="agentwatch:agentB:0.0",
                       now=120_300.0)
    wb.record(d, event_id=414, severity="high", event_type="agent_waiting_input",
              project_id="", correlation_id="agentwatch:agentB:0.0", now=120_300.0)
    p1 = wb.pending_wake(now=120_400.0)
    assert p1["event_id"] == 413                       # A's newest, not A's oldest
    wb.mark_submitted(413, source="test"); wb.acknowledge(413)
    p2 = wb.pending_wake(now=120_500.0)
    assert p2["event_id"] == 414                       # B is next, immediately


def test_the_waiting_and_agentwatch_prefixes_group_as_one_agent():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    for eid, corr, t in ((421, "waiting:same:0.0", 130_000.0),
                         (422, "agentwatch:same:0.0", 130_100.0)):
        d = wb.should_wake(event_id=eid, severity="high",
                           event_type="agent_waiting_input", project_id="",
                           correlation_id=corr, now=t)
        wb.record(d, event_id=eid, severity="high", event_type="agent_waiting_input",
                  project_id="", correlation_id=corr, now=t)
    p = wb.pending_wake(now=130_200.0)
    assert p["event_id"] == 422
    wb.mark_submitted(422, source="test"); wb.acknowledge(422)
    assert wb.pending_wake(now=130_300.0)["pending"] is False
