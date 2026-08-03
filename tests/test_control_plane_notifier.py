"""Control Plane V2 — notifier outbox drain (P3): retry + dead-letter, never silent."""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import notifier, delivery, cto


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    for v in ("WATCHDOG_TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CHATGPT_HOURLY_ENABLED", "CONTROL_PLANE_SAMECHAT_WAKE_URL"):
        monkeypatch.delenv(v, raising=False)
    yield


def test_drain_sends_when_channel_available(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    delivery.refresh_channel_health()
    cp.enqueue_notification(channel="owner_push", dedup_key="k1")
    out = notifier.drain()
    assert out["sent"] == 1 and cp.pending_notifications() == []


def test_drain_fails_visibly_and_dead_letters_when_red(monkeypatch):
    delivery.refresh_channel_health()      # RED (no channels)
    cp.enqueue_notification(channel="owner_push", dedup_key="k2")
    # each drain attempt fails (visible), attempts increment; the dead-letter check runs at
    # the start of the next pass, so it needs one pass beyond max_attempts.
    for _ in range(6):
        notifier.drain(max_attempts=5)
    # after max attempts → dead-letter + critical event, not silent
    assert cp.pending_notifications() == []
    assert any(e["type"] == "notification_dead_letter" and e["severity"] == "critical"
               for e in cto.cto_brief_since("t")["events"])
