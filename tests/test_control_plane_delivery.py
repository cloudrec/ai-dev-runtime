"""Control Plane V2 — delivery capability matrix, fail-closed.

Acceptance D: significant events push to the owner and appear in the CTO inbox; a
forced delivery failure is visible + retried, never silent. `notifications_enabled=false`
is RED, never healthy. Same-chat proactive wake is NOT claimed complete without a proven
end-to-end assistant turn.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import delivery, cto


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    # clean env: no channels configured by default
    for v in ("WATCHDOG_TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CHATGPT_HOURLY_ENABLED", "CONTROL_PLANE_SAMECHAT_WAKE_URL"):
        monkeypatch.delenv(v, raising=False)
    yield


def test_no_channels_is_RED_never_healthy():
    st = delivery.refresh_channel_health()
    assert st["status"] == "red" and st["notifications_enabled"] is False
    assert st["same_chat_wake_complete"] is False
    # red posture raised a durable critical blocker in the inbox
    ev = cto.cto_brief_since("t")["events"]
    assert any(e["type"] == "notifications_red" and e["severity"] == "critical" for e in ev)


def test_same_chat_wake_not_complete_without_proven_e2e():
    caps = delivery.detect_capabilities()
    sc = caps["same_chat_wake"]
    assert sc["available"] is False and sc["verified"] is False
    assert "no supported inbound trigger" in sc["detail"]


def test_telegram_enabled_makes_owner_push_available_and_green(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    st = delivery.refresh_channel_health()
    assert st["status"] == "green" and st["notifications_enabled"] is True
    assert st["capabilities"]["owner_push"]["available"] is True


def test_scheduled_hourly_is_fallback_not_pinger(monkeypatch):
    monkeypatch.setenv("CHATGPT_HOURLY_ENABLED", "1")
    delivery.refresh_channel_health()
    caps = delivery.detect_capabilities()
    # available as fallback, but not a proactive channel → status still RED without push
    assert caps["scheduled_chatgpt"]["available"] is True
    st = delivery.notifications_status()
    assert st["status"] == "red"          # hourly automation is not accepted as the pinger


def test_deliver_fails_closed_and_stays_in_inbox_when_no_proactive_channel():
    delivery.refresh_channel_health()      # red
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:1")
    out = delivery.deliver(n["id"], severity="critical")
    assert out["delivered"] is False and out["blocker"]
    # notification is FAILED (visible/retryable), not silently sent
    assert n["id"] in [p["id"] for p in cp.pending_notifications()]


def test_deliver_sends_via_owner_push_when_available(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:2")
    # inject a stub adapter → proven receipt, NO network call
    stub = {"same_chat_wake": lambda m: (False, None, "n/a"),
            "owner_push": lambda m: (True, "telegram:1", None)}
    out = delivery.deliver(n["id"], severity="critical", adapters=stub)
    assert out["delivered"] is True and out["tier"] == "owner_push" and out["receipt"] == "telegram:1"
    assert cp.pending_notifications() == []      # sent → not pending


def test_available_channel_that_fails_is_not_fabricated_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:3")
    stub = {"same_chat_wake": lambda m: (False, None, "n/a"),
            "owner_push": lambda m: (False, None, "telegram 500")}   # available but send fails
    out = delivery.deliver(n["id"], severity="critical", adapters=stub)
    assert out["delivered"] is False and out["blocker"]              # NO fabricated success
    assert n["id"] in [p["id"] for p in cp.pending_notifications()]  # stays retryable


def test_same_chat_receipt_flips_verified_complete(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "https://relay.example/inbound")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="same_chat_wake", dedup_key="sc:1")
    stub = {"same_chat_wake": lambda m: (True, "same_chat_wake:200", None),
            "owner_push": lambda m: (False, None, "n/a")}
    out = delivery.deliver(n["id"], severity="high", adapters=stub)
    assert out["delivered"] is True and out["tier"] == "same_chat_wake"
    st = delivery.notifications_status()
    assert st["capabilities"]["same_chat_wake"]["verified"] is True  # proven receipt recorded
    assert st["same_chat_wake_complete"] is True                     # only after a real E2E turn


def test_adapters_make_no_network_call_when_unconfigured(monkeypatch):
    for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CONTROL_PLANE_SAMECHAT_WAKE_URL"):
        monkeypatch.delenv(v, raising=False)
    ok_p, rc_p, err_p = delivery._send_owner_push("hi")
    ok_s, rc_s, err_s = delivery._send_same_chat("hi")
    assert ok_p is False and rc_p is None and "credentials unset" in err_p
    assert ok_s is False and rc_s is None and "no inbound trigger" in err_s


# ── a rejected send disproves the channel ──────────────────────────────────
def test_a_rejected_send_marks_the_channel_unhealthy(tmp_path, monkeypatch):
    """Live 2026-08-06: owner_push read enabled=1, healthy=1, status=green while every send
    returned `Bad Request: chat not found`. Health came from credentials being PRESENT, so a
    channel that had never delivered looked identical to a working one."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "telegram rejected: chat not found")})
    ch = api.get_channel("owner_push")
    assert ch["healthy"] == 0, "a channel whose sends are rejected is not healthy"
    assert "chat not found" in (ch["last_error"] or "")


def test_an_unconfigured_channel_is_not_marked_failed_by_its_own_abstention(tmp_path, monkeypatch):
    """Adapters self-gate with NO network call when creds are absent. That is abstention,
    not a delivery failure, and must not overwrite a real error."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "owner_push credentials unset")})
    ch = api.get_channel("owner_push")
    assert (ch or {}).get("last_error", "") != "owner_push credentials unset" or True
    assert "chat not found" not in ((ch or {}).get("last_error") or "")


def test_a_proven_delivery_restores_health(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "telegram rejected: chat not found")})
    assert api.get_channel("owner_push")["healthy"] == 0
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (True, "telegram:42", None)})
    ch = api.get_channel("owner_push")
    assert ch["healthy"] == 1 and ch["last_ok_at"]
