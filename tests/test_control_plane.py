"""Owner OS Control Plane V2 — P0 foundations: durable SoT, event log, explicit
unknown/stale, lease arbitration + fence-token restart safety, owner-gate
correlation, notification outbox dedup. No actuation (P2)."""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


# ── schema ───────────────────────────────────────────────────────────────────
def test_schema_initializes_at_version():
    assert store.schema_version() == store.SCHEMA_VERSION >= 1


# ── event log ────────────────────────────────────────────────────────────────
def test_event_append_and_query_by_entity_and_correlation():
    cp.append_event("collector", "agent_state", entity_type="agent", entity_id="a:0.0",
                    payload={"actual_state": "idle"}, correlation_id="corr1")
    cp.append_event("collector", "agent_state", entity_type="agent", entity_id="b:0.0")
    ev = cp.get_events(entity_type="agent", entity_id="a:0.0")
    assert len(ev) == 1 and ev[0]["payload"]["actual_state"] == "idle"
    assert cp.get_events(correlation_id="corr1")[0]["entity_id"] == "a:0.0"


# ── explicit unknown / stale (health never from absence) ─────────────────────
def test_agent_defaults_to_unknown_and_is_stale_without_evidence():
    cp.upsert_agent("arb:0.0", session="arb", project_id="arbitrage2")
    a = cp.get_agent("arb:0.0")
    assert a["actual_state"] == "unknown"
    assert cp.is_stale("arb:0.0") is True            # no evidence yet → explicit stale


def test_state_advances_only_with_evidence_and_freshness_drives_stale():
    cp.set_agent_state("arb:0.0", "working", controller="collector", evidence_ref="pane#1")
    a = cp.get_agent("arb:0.0")
    assert a["actual_state"] == "working" and a["evidence_fresh_at"]
    assert cp.is_stale("arb:0.0", ttl_secs=120) is False
    # freshness anchor is evidence_fresh_at; a tiny ttl makes it explicitly stale
    assert cp.is_stale("arb:0.0", ttl_secs=0) is True
    assert cp.latest_evidence("agent", "arb:0.0")["ref"] == "pane#1"


def test_unknown_agent_is_stale_not_ok():
    assert cp.is_stale("never-seen:0.0") is True     # absence ⇒ stale, never healthy


# ── resource lease: single holder + monotonic fence + restart safety ─────────
def test_lease_single_holder_and_expiry():
    l1 = cp.acquire_lease("agent:arb:0.0", "orchestrator", ttl_secs=100, now=1000)
    assert l1 and l1["fence_token"] == 1
    # another controller refused while the lease is held
    assert cp.acquire_lease("agent:arb:0.0", "watchdog", ttl_secs=100, now=1050) is None
    # after expiry it can be taken, fence increments
    l2 = cp.acquire_lease("agent:arb:0.0", "watchdog", ttl_secs=100, now=1200)
    assert l2 and l2["fence_token"] == 2


def test_fence_token_rejects_stale_actuation_after_restart():
    l1 = cp.acquire_lease("agent:arb:0.0", "watchdog", ttl_secs=100, now=1000)
    # simulate a restart: the controller re-acquires → higher fence
    l2 = cp.acquire_lease("agent:arb:0.0", "watchdog", ttl_secs=100, now=1010)
    assert l2["fence_token"] == l1["fence_token"] + 1
    # an in-flight action carrying the OLD fence is no longer current → no duplicate cmd
    assert cp.lease_is_current("agent:arb:0.0", l1["lease_id"], l1["fence_token"]) is False
    assert cp.lease_is_current("agent:arb:0.0", l2["lease_id"], l2["fence_token"]) is True


def test_lease_release_frees_resource():
    l1 = cp.acquire_lease("agent:x:0.0", "c1", now=1000)
    assert cp.release_lease("agent:x:0.0", l1["lease_id"]) is True
    l2 = cp.acquire_lease("agent:x:0.0", "c2", now=1001)
    assert l2 is not None


# ── owner gate: correlated stop → answer → resume target ─────────────────────
def test_owner_gate_open_answer_preserves_correlation_and_agent():
    g = cp.open_gate(agent_id="arb:0.0", work_item_id="wi1", reason="unsafe pending",
                     kind="policy")
    assert g["state"] == "open"
    assert cp.get_open_gates()[0]["agent_id"] == "arb:0.0"
    ans = cp.answer_gate(g["id"], "approve")
    assert ans["agent_id"] == "arb:0.0" and ans["work_item_id"] == "wi1"
    assert ans["correlation_id"] == g["correlation_id"]     # reply correlated to the gate
    assert cp.get_open_gates() == []                        # no longer open
    assert cp.answer_gate(g["id"], "again") is None         # not answerable twice


# ── notification outbox: durable state + dedup ───────────────────────────────
def test_notification_outbox_states_and_dedup():
    n = cp.enqueue_notification(channel="telegram", dedup_key="blocker:arb:0.0", correlation_id="c")
    assert n["state"] == "pending" and n["deduped"] is False
    # same dedup key while pending → deduped, not re-enqueued
    assert cp.enqueue_notification(channel="telegram", dedup_key="blocker:arb:0.0")["deduped"] is True
    assert [p["id"] for p in cp.pending_notifications()] == [n["id"]]
    cp.mark_notification(n["id"], "sending")
    cp.mark_notification(n["id"], "sent", receipt="tg-123")
    assert cp.pending_notifications() == []                 # sent → not pending
    # a failure re-enters the retry set
    n2 = cp.enqueue_notification(channel="telegram", dedup_key="blocker:other")
    cp.mark_notification(n2["id"], "failed")
    assert n2["id"] in [p["id"] for p in cp.pending_notifications()]


# ── decision + budget ────────────────────────────────────────────────────────
def test_decision_and_budget_persist():
    cp.record_decision("agent", "arb:0.0", "autonomous_safe", "continue", "safe next step")
    cp.upsert_budget("global", model="haiku", tokens=1000, cost_usd=0.01)
    assert cp.get_budget("global")["tokens"] == 1000
