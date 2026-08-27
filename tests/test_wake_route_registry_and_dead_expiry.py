"""Wake routing: session-named events, and dead marks that expire (stuchalka).

Two defects made project agents unable to wake their own ChatGPT chat, so every
notification landed in the owner-os control chat and the owner had to relay it
by hand:

1. `agent_watcher` labels a transition with the tmux SESSION
   (`payorch-live-buttons`, `chemmy-fast`), while routes are bound to the
   PROJECT (`payment-orchestrator`, `mess`). Those keys never matched, so
   resolution fell through to the labelled owner-os fallback. The mapping
   already existed in the control plane's own `agent` table and was simply
   never consulted.

2. A dead mark was permanent for a project route. A dead route is never
   selected for delivery, and only a delivery clears the mark, so
   `composer_did_not_clear_after_send` — a transient page hiccup — silenced
   gaika-video for twelve days. wake_bridge already exempted owner-os from this
   self-locking gate; project routes were left inside it.
"""
from __future__ import annotations

import time

import pytest

from core import chat_registry, wake_routes

CHAT_A = "https://chatgpt.com/c/aaaaaaaa-1111-2222-3333-444444444444"
CHAT_B = "https://chatgpt.com/c/bbbbbbbb-1111-2222-3333-444444444444"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import store
    c = store.connect()
    store.init_db(c)
    c.execute("CREATE TABLE IF NOT EXISTS agent (id INTEGER PRIMARY KEY, target TEXT, "
              "session TEXT, project_id TEXT)")
    wake_routes.bind_route("owner-os", CHAT_A, by="test", conn=c)
    wake_routes.bind_route("payment-orchestrator", CHAT_B, by="test", conn=c)
    c.commit()
    return c


def _agent_row(conn, session, project, target=""):
    conn.execute("INSERT INTO agent (target, session, project_id) VALUES (?,?,?)",
                 (target or f"{session}:0.0", session, project))
    conn.commit()


# ── 1. session -> project via the registry ─────────────────────────────────

def test_a_session_named_event_reaches_its_projects_chat(conn):
    _agent_row(conn, "payorch-live-buttons", "payment-orchestrator")
    out = wake_routes.resolve(project_id="payorch-live-buttons", conn=conn)
    assert out["bound"] is True
    assert out["route_key"] == "payment-orchestrator"
    assert out["conversation"] == CHAT_B
    assert "via_agent_registry(payorch-live-buttons)" in out["route_reason"]


def test_an_event_that_already_names_its_project_is_unchanged(conn):
    out = wake_routes.resolve(project_id="payment-orchestrator", conn=conn)
    assert out["route_key"] == "payment-orchestrator"
    assert out["route_reason"] == "explicit_route"


def test_a_session_whose_project_has_no_route_still_falls_back_labelled(conn):
    _agent_row(conn, "gaika-ext-audit", "gaika-extension")
    out = wake_routes.resolve(project_id="gaika-ext-audit", conn=conn)
    assert out["route_key"] == "owner-os"
    assert out["conversation"] == CHAT_A
    assert "unmapped_route:gaika-ext-audit" in out["route_reason"]


def test_an_unknown_session_is_never_guessed_at(conn):
    out = wake_routes.resolve(project_id="never-seen-anywhere", conn=conn)
    assert out["route_key"] == "owner-os"
    assert out["route_reason"] == "unmapped_route:never-seen-anywhere"


def test_an_ambiguous_session_refuses_to_pick(conn):
    """One session name mapping to two projects is a registry defect, not a
    routing decision. It must fall back, labelled, rather than choose."""
    _agent_row(conn, "shared-name", "payment-orchestrator", target="shared-name:0.0")
    _agent_row(conn, "shared-name", "some-other-project", target="shared-name:0.1")
    out = wake_routes.resolve(project_id="shared-name", conn=conn)
    assert out["route_key"] == "owner-os"
    assert "ambiguous_registry_project" in out["route_reason"]


def test_resolution_falls_back_when_the_agent_table_is_missing(tmp_path, monkeypatch):
    """Routing must survive a control plane without the registry table."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "bare.db"))
    from core.control_plane import store
    c = store.connect()
    store.init_db(c)
    c.execute("DROP TABLE IF EXISTS agent")
    wake_routes.bind_route("owner-os", CHAT_A, by="test", conn=c)
    out = wake_routes.resolve(project_id="payorch-live-buttons", conn=c)
    assert out["bound"] is True
    assert out["route_key"] == "owner-os"


def test_agent_id_is_used_when_the_session_lookup_finds_nothing(conn):
    _agent_row(conn, "some-session", "payment-orchestrator", target="pane-ref:0.0")
    out = wake_routes.resolve(project_id="not-a-session", agent_id="pane-ref:0.0", conn=conn)
    assert out["route_key"] == "payment-orchestrator"


# ── 2. dead marks expire ───────────────────────────────────────────────────

def test_a_fresh_dead_mark_still_gates_the_route(conn):
    chat_registry.mark_dead(CHAT_B, reason="composer_did_not_clear_after_send",
                            conn=conn, now=time.time())
    out = wake_routes.resolve(project_id="payment-orchestrator", conn=conn)
    assert out["route_key"] == "owner-os"
    assert "dead_route:payment-orchestrator" in out["route_reason"]


def test_a_stale_dead_mark_lets_the_route_retry(conn, monkeypatch):
    """The whole point: a transient refusal must not silence a project forever."""
    marked = time.time()
    chat_registry.mark_dead(CHAT_B, reason="composer_did_not_clear_after_send",
                            conn=conn, now=marked)
    monkeypatch.setattr(wake_routes, "now_ts",
                        lambda: marked + wake_routes.DEAD_ROUTE_RETRY_SECS + 1)
    out = wake_routes.resolve(project_id="payment-orchestrator", conn=conn)
    assert out["route_key"] == "payment-orchestrator"
    assert out["conversation"] == CHAT_B


def test_a_successful_delivery_clears_the_mark_outright(conn):
    chat_registry.mark_dead(CHAT_B, reason="composer_did_not_clear_after_send",
                            conn=conn, now=time.time())
    chat_registry.upsert_chat(CHAT_B, source="delivery", writable=True, conn=conn)
    row = conn.execute("SELECT active, dead_reason, dead_at_ts FROM chat_registry "
                       "WHERE conversation=?", (CHAT_B,)).fetchone()
    assert row[0] == 1 and not row[1] and row[2] is None
    assert wake_routes.resolve(project_id="payment-orchestrator",
                               conn=conn)["route_key"] == "payment-orchestrator"


def test_a_pre_migration_row_without_dead_at_is_dated_from_its_check_time(conn):
    """Rows written before `dead_at_ts` existed — gaika-video was one — must not
    read as 'dead since epoch 0' and be retried instantly, nor as permanent."""
    chat_registry.mark_dead(CHAT_B, reason="x", conn=conn, now=time.time())
    conn.execute("UPDATE chat_registry SET dead_at_ts=NULL WHERE conversation=?", (CHAT_B,))
    conn.commit()
    since = wake_routes._dead_since(conn, CHAT_B)
    assert since is not None and since > 0


def test_the_owner_os_fallback_is_still_exempt_from_dead_gating(conn):
    """Unchanged safety property: a dead mark on the control chat must never
    silence the entire notifier."""
    chat_registry.mark_dead(CHAT_A, reason="composer_did_not_clear_after_send",
                            conn=conn, now=time.time())
    out = wake_routes.resolve(project_id="owner-os", conn=conn)
    assert out["bound"] is True
    assert out["conversation"] == CHAT_A
    assert "despite_dead_mark" in out["route_reason"]


# ── 3. cooldowns are per chat, not global ──────────────────────────────────
# A cooldown exists so one chat is not spammed. It was evaluated across ALL
# routes, so the busiest chat silenced every other one: owner-os traffic alone
# accounted for most of 17k `cooldown_active` skips, and a MESS or payments
# agent waiting on the owner simply never rang while that ran.

import os  # noqa: E402

from core import wake_bridge  # noqa: E402


@pytest.fixture()
def bridge(conn, monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    _agent_row(conn, "payorch-live-buttons", "payment-orchestrator")
    return conn


def _wake(conn, *, event_id, project, now, severity="critical",
          event_type="agent_waiting_input"):
    d = wake_bridge.should_wake(event_id=event_id, severity=severity,
                                event_type=event_type, project_id=project,
                                owner_action_required=True, conn=conn, now=now)
    wake_bridge.record(d, event_id=event_id, severity=severity, event_type=event_type,
                       project_id=project, conn=conn, now=now)
    return d


def test_one_chats_cooldown_no_longer_silences_another(bridge):
    now = 1_800_000_000.0
    first = _wake(bridge, event_id=9001, project="owner-os", now=now)
    assert first["wake"] is True

    # Immediately after, a DIFFERENT project's chat must still be reachable.
    second = _wake(bridge, event_id=9002, project="payorch-live-buttons", now=now + 5)
    assert second["wake"] is True, second["reason"]
    assert second["route_key"] == "payment-orchestrator"


def test_the_same_chat_is_still_protected_from_a_burst(bridge):
    now = 1_800_000_000.0
    assert _wake(bridge, event_id=9101, project="payorch-live-buttons", now=now)["wake"]
    second = _wake(bridge, event_id=9102, project="payorch-live-buttons", now=now + 5)
    assert second["wake"] is False
    assert "cooldown" in second["reason"]


def test_the_floor_still_clears_for_that_chat_after_its_window(bridge):
    now = 1_800_000_000.0
    _wake(bridge, event_id=9201, project="payorch-live-buttons", now=now)
    later = _wake(bridge, event_id=9202, project="payorch-live-buttons",
                  now=now + wake_bridge.ACTIONABLE_COOLDOWN_SECS
                  + wake_bridge.COOLDOWN_SECS + 1)
    assert later["wake"] is True


def test_legacy_rows_without_a_route_key_count_as_owner_os(bridge):
    """Pre-migration audit rows are NULL. They were owner-os traffic, so they must
    keep holding the owner-os floor down rather than being ignored entirely."""
    now = 1_800_000_000.0
    wake_bridge._conn(bridge)          # ensure the audit schema exists
    bridge.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                   "project_id,route_key) VALUES (?,?,?,'wake','legacy',1,'',NULL)",
                   (now, "2026-08-27T00:00:00+00:00", 8999))
    bridge.commit()
    blocked = _wake(bridge, event_id=9301, project="owner-os", now=now + 5)
    assert blocked["wake"] is False
    assert "cooldown" in blocked["reason"]
    # ...while a project chat is unaffected by that legacy owner-os row.
    assert _wake(bridge, event_id=9302, project="payorch-live-buttons",
                 now=now + 6)["wake"] is True


def test_the_route_key_is_persisted_on_every_decision(bridge):
    now = 1_800_000_000.0
    _wake(bridge, event_id=9401, project="payorch-live-buttons", now=now)
    row = bridge.execute("SELECT route_key FROM wake_audit WHERE event_id=9401").fetchone()
    assert row[0] == "payment-orchestrator"


# ── 4. coalescing folds per CHAT, not per raw project key ──────────────────
# Two sessions of one project resolve to one conversation. Grouping the generic
# backlog by the raw project key left them unfolded, so that chat received the
# same "go read Owner OS" instruction twice, drained one per cooldown window -
# the backlog this function exists to collapse.

def _generic_wake(conn, *, event_id, project, now):
    d = wake_bridge.should_wake(event_id=event_id, severity="critical",
                                event_type="runtime_job_state", project_id=project,
                                owner_action_required=True, conn=conn, now=now)
    wake_bridge.record(d, event_id=event_id, severity="critical",
                       event_type="runtime_job_state", project_id=project,
                       conn=conn, now=now)
    return d


def test_two_sessions_of_one_project_fold_into_one_doorbell(bridge):
    _agent_row(bridge, "payorch-monitor-clean", "payment-orchestrator",
               target="payorch-monitor-clean:0.0")
    now = 1_800_000_000.0
    wake_bridge._conn(bridge)
    # Two generic wakes, different sessions, SAME project chat.
    for i, sess in enumerate(("payorch-live-buttons", "payorch-monitor-clean")):
        bridge.execute(
            "INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
            "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',0,?,?,0)",
            (now + i, "2026-08-27T18:00:00+00:00", 7100 + i, sess,
             "payment-orchestrator"))
    bridge.commit()
    out = wake_bridge.coalesce_generic_backlog(conn=bridge, now=now + 10)
    assert len(out["superseded_event_ids"]) == 1, out
    assert 7100 in out["superseded_event_ids"]      # older folded into newer


def test_different_chats_are_never_folded_into_each_other(bridge):
    now = 1_800_000_000.0
    wake_bridge._conn(bridge)
    for i, route in enumerate(("payment-orchestrator", "owner-os")):
        bridge.execute(
            "INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
            "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',0,?,?,0)",
            (now + i, "2026-08-27T18:00:00+00:00", 7200 + i, f"p{i}", route))
    bridge.commit()
    out = wake_bridge.coalesce_generic_backlog(conn=bridge, now=now + 10)
    assert out["superseded_event_ids"] == [], "a chat's only doorbell was dropped"


def test_rows_without_a_stored_route_are_resolved_fresh(bridge):
    """Pre-migration rows carry no route_key; they must still group by the chat
    they would actually reach, not by their raw project string."""
    _agent_row(bridge, "payorch-monitor-clean", "payment-orchestrator",
               target="payorch-monitor-clean:0.0")
    now = 1_800_000_000.0
    wake_bridge._conn(bridge)
    for i, sess in enumerate(("payorch-live-buttons", "payorch-monitor-clean")):
        bridge.execute(
            "INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
            "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',0,?,NULL,0)",
            (now + i, "2026-08-27T18:00:00+00:00", 7300 + i, sess))
    bridge.commit()
    out = wake_bridge.coalesce_generic_backlog(conn=bridge, now=now + 10)
    assert len(out["superseded_event_ids"]) == 1, out


# ── 5. the send choke point is also per chat ───────────────────────────────
# claim_send is the single gate every submission passes. Its cooldown was
# global, so it re-imposed at the send layer exactly the cross-chat suppression
# removed from the decision layer: a gaika-drop wake sat 867s behind an owner-os
# send that was never going to its chat (observed live, event 9870).

def test_a_claim_in_one_chat_does_not_block_another(bridge):
    now = 1_800_000_000.0
    a = wake_bridge.claim_send("test", event_id=1, conn=bridge, now=now,
                               route_key="owner-os")
    assert a["allowed"] is True
    b = wake_bridge.claim_send("test", event_id=2, conn=bridge, now=now + 5,
                               route_key="gaika-drop")
    assert b["allowed"] is True, b["reason"]


def test_a_second_claim_in_the_SAME_chat_is_still_refused(bridge):
    now = 1_800_000_000.0
    assert wake_bridge.claim_send("test", event_id=3, conn=bridge, now=now,
                                  route_key="gaika-drop")["allowed"] is True
    second = wake_bridge.claim_send("test", event_id=4, conn=bridge, now=now + 5,
                                    route_key="gaika-drop")
    assert second["allowed"] is False
    assert "cooldown" in second["reason"]


def test_the_actionable_floor_is_also_per_chat(bridge):
    now = 1_800_000_000.0
    assert wake_bridge.claim_send("t", event_id=5, conn=bridge, now=now,
                                  actionable=True, route_key="owner-os")["allowed"]
    other = wake_bridge.claim_send("t", event_id=6, conn=bridge, now=now + 5,
                                   actionable=True, route_key="mess")
    assert other["allowed"] is True
    same = wake_bridge.claim_send("t", event_id=7, conn=bridge, now=now + 6,
                                  actionable=True, route_key="mess")
    assert same["allowed"] is False


def test_every_attempt_is_still_recorded_allowed_or_not(bridge):
    """The out-of-band duplicate this gate exists to catch must stay visible."""
    now = 1_800_000_000.0
    wake_bridge.claim_send("companion", event_id=8, conn=bridge, now=now,
                           route_key="treasure")
    wake_bridge.claim_send("rogue-script", event_id=8, conn=bridge, now=now + 1,
                           route_key="treasure")
    rows = bridge.execute("SELECT source, allowed, route_key FROM wake_send "
                          "WHERE event_id=8 ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["companion", "rogue-script"]
    assert [r[1] for r in rows] == [1, 0]          # the second was refused, and recorded
    assert all(r[2] == "treasure" for r in rows)


def test_a_claim_without_a_route_counts_as_owner_os(bridge):
    """Legacy callers pass no route; they must keep the owner-os floor."""
    now = 1_800_000_000.0
    assert wake_bridge.claim_send("legacy", event_id=9, conn=bridge, now=now)["allowed"]
    blocked = wake_bridge.claim_send("legacy", event_id=10, conn=bridge, now=now + 5,
                                     route_key="owner-os")
    assert blocked["allowed"] is False
