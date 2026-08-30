"""cp-canary shadow pinger — scope confinement, no-re-emit, false-idle, restart/cursor/outbox
consistency, and honest (receipt-only) delivery. Observe-only; no pane actuation.
"""
from __future__ import annotations

import pytest

from core.control_plane import pinger_shadow as ps
from core.control_plane import event_pipeline as ep
from core.control_plane import cto, diagnostics as diag
from core.control_plane import api as cp
from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_PINGER_SHADOW_AGENTS", "cp-canary:0.0")   # canary only
    for v in ("CONTROL_PLANE_SAMECHAT_WAKE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "WATCHDOG_TELEGRAM_ENABLED"):
        monkeypatch.delenv(v, raising=False)
    yield


def _inv(pairs):
    return {"agents": [{"target": t, "state": s, "alive": True, "is_agent": True} for t, s in pairs]}


def _commander(agent=None):
    return [r for r in ac.list_commander_events(limit=100) if agent is None or r["agent"] == agent]


# ── SCOPE CONFINEMENT ────────────────────────────────────────────────────────
def test_scope_confinement_only_allowlisted_agent_emits():
    inv = _inv([("cp-canary:0.0", "waiting_owner"),
                ("payment:0.0", "waiting_owner"),
                ("arbitrage2-opus:0.0", "dead")])
    r = ps.shadow_tick(inventory=inv)
    assert r["emitted_count"] == 1 and r["emitted"][0]["agent"] == "cp-canary:0.0"
    assert r["out_of_scope_significant"] == 2         # payment + arbitrage2 counted, never emitted
    # only cp-canary reaches the durable inbox + legacy surface
    assert _commander("payment:0.0") == [] and _commander("arbitrage2-opus:0.0") == []
    assert len(_commander("cp-canary:0.0")) == 1
    assert cp.get_events(entity_id="payment:0.0") == []


def test_empty_allowlist_emits_nothing(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_PINGER_SHADOW_AGENTS", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_CANARY_AGENTS", raising=False)
    r = ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "waiting_owner")]))
    assert r["emitted_count"] == 0 and r["scope"] == []


# ── NO RE-EMIT / DEDUPE ──────────────────────────────────────────────────────
def test_no_reemit_same_state_across_ticks():
    inv = _inv([("cp-canary:0.0", "waiting_owner")])
    a = ps.shadow_tick(inventory=inv)
    b = ps.shadow_tick(inventory=inv)
    assert a["emitted_count"] == 1 and b["emitted_count"] == 0   # 2nd tick: same state, no re-emit
    assert len(_commander("cp-canary:0.0")) == 1                 # legacy surface deduped too


def test_transition_to_new_kind_emits_again():
    ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "waiting_owner")]))
    r = ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "completed")]),
                       tail_fn=lambda t: "done. Type your message.")
    assert r["emitted_count"] == 1 and r["emitted"][0]["kind"] == "completed"


# ── FALSE-IDLE ───────────────────────────────────────────────────────────────
def test_false_idle_completed_suppressed_and_last_kind_not_advanced():
    inv = _inv([("cp-canary:0.0", "completed")])
    r = ps.shadow_tick(inventory=inv, tail_fn=lambda t: "Pouncing… (8s · thinking)")
    assert r["emitted_count"] == 0 and r["suppressed_false_idle"] == ["cp-canary:0.0"]
    assert _commander("cp-canary:0.0") == []                     # no completion mirrored
    # last_kind NOT advanced → once genuinely quiet, it still emits
    r2 = ps.shadow_tick(inventory=inv, tail_fn=lambda t: "done. Type your message.")
    assert r2["emitted_count"] == 1


# ── HONEST DELIVERY: receipt only if proven, else cto_inbox floor ────────────
def test_honest_floor_no_receipt_without_proactive_channel():
    r = ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "waiting_owner")]))
    e = r["emitted"][0]
    assert e["delivered"] is False and e["receipt"] is None and e["delivery_floor"] == "cto_inbox"


def test_receipt_propagates_when_pipeline_delivers():
    def fake_publish(**kw):
        return {"ok": True, "event_id": 7, "delivered": True, "receipt": "same_chat_wake:9",
                "delivery_floor": None}
    r = ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "waiting_owner")]), publish_fn=fake_publish)
    assert r["emitted"][0]["delivered"] is True and r["emitted"][0]["receipt"] == "same_chat_wake:9"


def test_retry_once_flows_through_pipeline():
    calls = {"n": 0}

    def deliver_fn(nid, *, severity, conn=None):
        calls["n"] += 1
        if calls["n"] == 1:
            cp.mark_notification(nid, "failed", conn=conn)
            return {"delivered": False, "attempts": [{"tier": "owner_push", "result": "unavailable"}],
                    "blocker": "x"}
        cp.mark_notification(nid, "sent", receipt="owner_push:1", conn=conn)
        return {"delivered": True, "attempts": [{"tier": "owner_push", "result": "sent"}]}

    def publish(**kw):
        return ep.publish_significant_event(deliver_fn=deliver_fn, **kw)
    r = ps.shadow_tick(inventory=_inv([("cp-canary:0.0", "waiting_owner")]), publish_fn=publish)
    assert calls["n"] == 2 and r["emitted"][0]["delivered"] is True
    assert r["emitted"][0]["receipt"] == "owner_push:1"


# ── RESTART / CURSOR / OUTBOX CONSISTENCY ────────────────────────────────────
def test_restart_cursor_outbox_and_no_reemit_after_restart():
    inv = _inv([("cp-canary:0.0", "waiting_owner")])
    r = ps.shadow_tick(inventory=inv)
    eid = r["emitted"][0]["event_id"]
    # "restart": brand-new connection on the same durable DB (shadow_tick opens its own conn)
    brief = cto.cto_brief_since("chatgpt", ack=True)     # durable inbox holds it; cursor advances
    assert eid in [e["event_id"] for e in brief["events"]]
    assert cto.get_cursor("chatgpt") == brief["next_cursor"] >= eid
    # invariants hold across the (simulated) restart
    assert diag.consistency_report()["consistent"] is True
    assert diag.restart_consistency_report()["restart_safe"] is True
    # re-run after restart, SAME state → last_kind persisted → no duplicate ping
    r2 = ps.shadow_tick(inventory=inv)
    assert r2["emitted_count"] == 0
    assert len(_commander("cp-canary:0.0")) == 1


# ── ENGINE WIRING: shadow tick is invoked, best-effort (never breaks the loop) ─
def test_engine_tick_wires_pinger_and_swallows_errors(monkeypatch):
    import core.control_plane.engine as eng
    from core.control_plane import discovery, delivery, notifier
    monkeypatch.setattr(discovery, "discover", lambda: {
        "discovered": 0, "managed": 0, "observe_only": 0, "blocked": 0,
        "duplicates": 0, "dead": 0, "recovered": 0, "events": []})
    monkeypatch.setattr(delivery, "refresh_channel_health", lambda: {"status": "red"})
    monkeypatch.setattr(notifier, "drain", lambda: {"sent": 0, "failed": 0, "dead_letter": 0})
    seen = {}
    monkeypatch.setattr(ps, "shadow_tick",
                        lambda: (seen.__setitem__("called", True), {"emitted_count": 0})[1])
    res = eng.tick_once()
    assert "pinger" in res and seen.get("called") is True

    def boom():
        raise RuntimeError("pinger down")
    monkeypatch.setattr(ps, "shadow_tick", boom)
    res2 = eng.tick_once()               # a pinger failure must NOT break the tick
    assert res2["pinger"]["error"] == "pinger down"
    assert "discovery" in res2 and "outbox" in res2
