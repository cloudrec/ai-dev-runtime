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
    # real adapter returns a proven receipt (stubbed — no network); delivery marks sent only
    # on a real receipt now, never on mere availability.
    monkeypatch.setattr(delivery, "_send_owner_push", lambda m: (True, "telegram:1", None))
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


# ── one dead channel is ONE alarm, not one per message (event 15483) ──────────────────
# The dead-letter event was keyed `deadletter:{notification_id}`. An id is unique by
# construction, so the dedup window could never match and every dead-lettered message
# minted a fresh critical owner_action_required event: 937 events under 937 distinct keys
# in 24h on this host, all for a single unchanging cause (Telegram "chat not found").

def _dead_letter_events():
    return [e for e in cto.cto_brief_since("t")["events"]
            if e["type"] == "notification_dead_letter"]


def test_one_dead_channel_raises_one_alarm_not_one_per_message():
    delivery.refresh_channel_health()                     # RED
    for i in range(4):
        cp.enqueue_notification(channel="owner_push", dedup_key=f"many{i}")
    for _ in range(6):
        notifier.drain(max_attempts=5)
    assert cp.pending_notifications() == []
    assert len(_dead_letter_events()) == 1


def test_every_dead_lettered_message_is_still_recorded_individually():
    """The collapse is in the alarm, never in the ledger."""
    delivery.refresh_channel_health()
    for i in range(3):
        cp.enqueue_notification(channel="owner_push", dedup_key=f"ledger{i}")
    for _ in range(6):
        notifier.drain(max_attempts=5)
    conn = cp.api._c(None)[0]
    n = conn.execute("SELECT COUNT(*) FROM notification WHERE state='dead_letter'").fetchone()[0]
    assert n == 3                                          # per-message record intact


def test_a_different_channel_still_gets_its_own_alarm():
    """Keying by channel must not hide a genuinely NEW failure."""
    delivery.refresh_channel_health()
    cp.enqueue_notification(channel="owner_push", dedup_key="chA")
    cp.enqueue_notification(channel="some_other_channel", dedup_key="chB")
    for _ in range(6):
        notifier.drain(max_attempts=5)
    channels = {e.get("payload", {}).get("channel") for e in _dead_letter_events()}
    assert channels == {"owner_push", "some_other_channel"}


# ── a dead letter must say WHY (2026-09-01) ──────────────────────────────────
# 311 critical dead letters in 24 h whose entire payload was an id, a channel
# name, an attempt count and a dedup key. The cause sat one table away the whole
# time — `channel.last_error` read "telegram send failed: Bad Request: chat not
# found", a chat id the bot cannot post to. `deliver()` computes that reason on
# every attempt and returns it in `attempts[].detail`, but the dead-letter branch
# fires on a LATER drain, when that return value is long gone.

CHAT_NOT_FOUND = "telegram send failed: Bad Request: chat not found"


def _drain_to_dead_letter(max_attempts=5):
    cp.enqueue_notification(channel="owner_push", dedup_key="why")
    for _ in range(max_attempts + 1):
        notifier.drain(max_attempts=max_attempts)
    return [e for e in cto.cto_brief_since("t")["events"]
            if e["type"] == "notification_dead_letter"]


def test_a_dead_letter_carries_the_rejection_that_caused_it(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setattr(delivery, "_send_owner_push",
                        lambda m: (False, None, CHAT_NOT_FOUND))
    delivery.refresh_channel_health()

    evs = _drain_to_dead_letter()
    assert evs, "no dead letter raised"
    assert evs[0]["payload"]["reasons"]["owner_push"] == CHAT_NOT_FOUND


def test_the_summary_line_names_the_cause(monkeypatch):
    """`action_taken` is what the notifications surface shows, and "delivery
    channel unhealthy" only restates the severity."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    monkeypatch.setattr(delivery, "_send_owner_push",
                        lambda m: (False, None, CHAT_NOT_FOUND))
    delivery.refresh_channel_health()

    evs = _drain_to_dead_letter()
    assert "chat not found" in (evs[0].get("action_taken") or "")


def test_an_unexplained_failure_still_dead_letters(monkeypatch):
    """Best-effort by construction: an alarm that cannot be raised because its
    explanation failed to load is strictly worse than an unexplained alarm."""
    monkeypatch.setattr(notifier, "_failure_reasons", lambda conn=None: {})
    delivery.refresh_channel_health()
    evs = _drain_to_dead_letter()
    assert evs, "the alarm must survive having no reason to give"
    assert evs[0]["severity"] == "critical"
    assert "delivery channel unhealthy" in (evs[0].get("action_taken") or "")


def test_reading_the_reason_never_breaks_the_alarm(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("channel table unreadable")
    monkeypatch.setattr(notifier.api, "get_channel", boom)
    assert notifier._failure_reasons() == {}
