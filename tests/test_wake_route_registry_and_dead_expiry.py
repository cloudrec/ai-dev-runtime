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
