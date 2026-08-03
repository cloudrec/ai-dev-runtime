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
    out = delivery.deliver(n["id"], severity="critical")
    assert out["delivered"] is True and out["tier"] == "owner_push" and out["attempts"]
    assert cp.pending_notifications() == []      # sent → not pending
