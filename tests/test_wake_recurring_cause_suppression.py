"""A cause already signalled must not keep waking the same conversation.

`notification_dead_letter` mints a fresh event every dedup window, so every occurrence had
a NEW event_id: `already_woke_for_this_event` never matched, and the non-actionable
cooldown only spaced the repeats out. Measured live 2026-09-07 — 1029 wakes of the
owner-os conversation for ONE cause that has not changed since 2026-08-03, eight of them in
a single afternoon. Only the owner can clear it, so every repeat was noise.

What must survive the suppression, and is asserted here:

* a CHANGED reason, or a different channel, is a different cause and still wakes;
* a cause that returns AFTER A RECOVERY wakes again — "it broke, healed, broke again" is
  news, "it is still broken" is not;
* event types without a cause signature are completely unaffected;
* an unreadable payload fails OPEN and wakes, because silencing an alert on an internal
  error is the one outcome worth avoiding.
"""
from __future__ import annotations

import json

import pytest

from core import wake_bridge as wb

DEAD_LETTER = "notification_dead_letter"
CHAT_NOT_FOUND = "telegram send failed: Bad Request: chat not found"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.delenv("WAKE_BRIDGE_KILL_SWITCH", raising=False)
    yield


def _conn():
    from core.control_plane.api import _c
    conn, _own = _c(None)
    conn.execute("""CREATE TABLE IF NOT EXISTS event (
        id INTEGER PRIMARY KEY, ts TEXT, type TEXT, severity TEXT, payload TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS wake_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, at TEXT, event_id INTEGER,
        correlation_id TEXT, severity TEXT, decision TEXT, reason TEXT,
        acknowledged INTEGER DEFAULT 0, acknowledged_at TEXT, event_type TEXT,
        actionable INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS notification (
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, channel TEXT,
        dedup_key TEXT, state TEXT, attempts INTEGER, last_attempt_at TEXT,
        receipt TEXT, correlation_id TEXT, created_at TEXT)""")
    conn.commit()
    return conn


def _dead_letter_event(conn, event_id, *, reason=CHAT_NOT_FOUND, channel="telegram"):
    payload = json.dumps({"notification_id": event_id, "channel": channel, "attempts": 5,
                          "reasons": {"owner_push": reason,
                                      "same_chat_wake": "no inbound trigger configured"}})
    conn.execute("INSERT OR REPLACE INTO event (id,ts,type,severity,payload) VALUES (?,?,?,?,?)",
                 (event_id, "2026-09-07T00:00:00+00:00", DEAD_LETTER, "critical", payload))
    conn.commit()


def _record_wake(conn, event_id, *, ts=1000.0, at="2026-09-07T00:00:00+00:00"):
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,event_type,actionable) "
                 "VALUES (?,?,?,'wake','urgent_event_not_yet_signalled',?,0)",
                 (ts, at, event_id, DEAD_LETTER))
    conn.commit()


# ── the signature itself ────────────────────────────────────────────────────
def test_the_signature_is_stable_for_the_same_cause():
    conn = _conn()
    _dead_letter_event(conn, 1)
    _dead_letter_event(conn, 2)
    assert wb.cause_signature(conn, 1, DEAD_LETTER) == wb.cause_signature(conn, 2, DEAD_LETTER)


def test_a_different_reason_is_a_different_signature():
    conn = _conn()
    _dead_letter_event(conn, 1)
    _dead_letter_event(conn, 2, reason="telegram send failed: Unauthorized")
    assert wb.cause_signature(conn, 1, DEAD_LETTER) != wb.cause_signature(conn, 2, DEAD_LETTER)


def test_a_different_channel_is_a_different_signature():
    conn = _conn()
    _dead_letter_event(conn, 1, channel="telegram")
    _dead_letter_event(conn, 2, channel="owner_push")
    assert wb.cause_signature(conn, 1, DEAD_LETTER) != wb.cause_signature(conn, 2, DEAD_LETTER)


def test_types_without_a_recurring_cause_get_no_signature():
    conn = _conn()
    _dead_letter_event(conn, 1)
    assert wb.cause_signature(conn, 1, "agent_waiting_input") == ("", "")


def test_an_unreadable_payload_fails_open():
    """No signature means no suppression: an internal error must not silence an alert."""
    conn = _conn()
    conn.execute("INSERT INTO event (id,ts,type,severity,payload) VALUES (9,'t',?,'critical','not json')",
                 (DEAD_LETTER,))
    conn.commit()
    assert wb.cause_signature(conn, 9, DEAD_LETTER) == ("", "")


# ── the suppression decision ────────────────────────────────────────────────
def test_the_same_cause_is_reported_as_already_signalled():
    conn = _conn()
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1)
    _dead_letter_event(conn, 2)
    sig, chan = wb.cause_signature(conn, 2, DEAD_LETTER)
    seen = wb.recurring_cause_already_signalled(
        conn, event_id=2, event_type=DEAD_LETTER, signature=sig, channel=chan)
    assert seen and seen["event_id"] == 1


def test_a_changed_reason_is_not_suppressed():
    conn = _conn()
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1)
    _dead_letter_event(conn, 2, reason="telegram send failed: Unauthorized")
    sig, chan = wb.cause_signature(conn, 2, DEAD_LETTER)
    assert wb.recurring_cause_already_signalled(
        conn, event_id=2, event_type=DEAD_LETTER, signature=sig, channel=chan) is None


def test_a_cause_that_returns_after_a_recovery_wakes_again():
    """broke -> healed -> broke again is news; still-broken is not."""
    conn = _conn()
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1, ts=1000.0, at="2026-09-07T00:00:00+00:00")
    # a proven send on that channel AFTER the first wake
    conn.execute("INSERT INTO notification (channel,state,created_at) VALUES "
                 "('telegram','sent','2026-09-07T01:00:00+00:00')")
    conn.commit()
    _dead_letter_event(conn, 2)
    sig, chan = wb.cause_signature(conn, 2, DEAD_LETTER)
    assert wb.recurring_cause_already_signalled(
        conn, event_id=2, event_type=DEAD_LETTER, signature=sig, channel=chan) is None


def test_a_failed_send_in_between_does_not_re_arm_it():
    """Only a PROVEN delivery counts as recovery; another dead letter is more of the same."""
    conn = _conn()
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1, at="2026-09-07T00:00:00+00:00")
    conn.execute("INSERT INTO notification (channel,state,created_at) VALUES "
                 "('telegram','dead_letter','2026-09-07T01:00:00+00:00')")
    conn.commit()
    _dead_letter_event(conn, 2)
    sig, chan = wb.cause_signature(conn, 2, DEAD_LETTER)
    seen = wb.recurring_cause_already_signalled(
        conn, event_id=2, event_type=DEAD_LETTER, signature=sig, channel=chan)
    assert seen and seen["event_id"] == 1


# ── end to end through should_wake ──────────────────────────────────────────
def _bind_route(conn):
    from core import wake_routes
    wake_routes.bind_route("owner-os", "https://chatgpt.com/c/6a967789-9b28-83ed-9261-de5162d4ac17",
                           by="test", conn=conn)


def test_should_wake_suppresses_the_repeat_and_names_the_first(monkeypatch):
    conn = _conn()
    _bind_route(conn)
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1)
    _dead_letter_event(conn, 2)
    d = wb.should_wake(event_id=2, severity="critical", owner_action_required=True,
                       event_type=DEAD_LETTER, project_id="owner-os", conn=conn)
    assert d["wake"] is False
    assert d["reason"] == "recurring_cause_already_signalled", d
    assert d["first_signalled_event_id"] == 1


def test_should_wake_still_wakes_for_a_new_reason(monkeypatch):
    conn = _conn()
    _bind_route(conn)
    _dead_letter_event(conn, 1)
    _record_wake(conn, 1)
    _dead_letter_event(conn, 2, reason="telegram send failed: Unauthorized")
    d = wb.should_wake(event_id=2, severity="critical", owner_action_required=True,
                       event_type=DEAD_LETTER, project_id="owner-os", conn=conn)
    assert d["wake"] is True, d
    assert d["reason"] == "urgent_event_not_yet_signalled"


def test_the_first_occurrence_of_a_cause_still_wakes():
    """Suppression must never swallow the FIRST report of a problem."""
    conn = _conn()
    _bind_route(conn)
    _dead_letter_event(conn, 1)
    d = wb.should_wake(event_id=1, severity="critical", owner_action_required=True,
                       event_type=DEAD_LETTER, project_id="owner-os", conn=conn)
    assert d["wake"] is True, d


def test_unrelated_event_types_are_untouched():
    conn = _conn()
    _bind_route(conn)
    conn.execute("INSERT INTO event (id,ts,type,severity,payload) VALUES "
                 "(5,'t','work_stopped_incomplete','high','{}')")
    conn.commit()
    d = wb.should_wake(event_id=5, severity="high", owner_action_required=True,
                       event_type="work_stopped_incomplete", project_id="owner-os", conn=conn)
    assert d["reason"] != "recurring_cause_already_signalled"


def test_recovery_re_arm_does_not_depend_on_matching_a_float_timestamp():
    """Regression: the re-arm once re-looked the timestamp up with `WHERE ts=?`.

    Float equality on a REAL column, resolved with `ORDER BY id DESC LIMIT 1` — so when
    another wake row shared that ts, the lookup returned THAT row's `at` instead of the
    one being evaluated, and a real recovery in between was missed. The alert then stayed
    suppressed for a reason unrelated to recovery.

    Shaped to discriminate: the matching prior is the OLD row (00:00), while a newer,
    NON-matching row (different reason) shares its ts and carries a later `at` (05:00).
    The old lookup resolves to 05:00 and misses the 01:00 recovery; the caller already
    holds 00:00, so it must see it.
    """
    conn = _conn()
    _dead_letter_event(conn, 1)                                   # same cause
    _dead_letter_event(conn, 3, reason="telegram send failed: Unauthorized")   # different
    _record_wake(conn, 1, ts=1000.0, at="2026-09-07T00:00:00+00:00")
    _record_wake(conn, 3, ts=1000.0, at="2026-09-07T05:00:00+00:00")
    conn.execute("INSERT INTO notification (channel,state,created_at) VALUES "
                 "('telegram','sent','2026-09-07T01:00:00+00:00')")
    conn.commit()

    _dead_letter_event(conn, 4)                                   # same cause as event 1
    sig, chan = wb.cause_signature(conn, 4, DEAD_LETTER)
    assert wb.recurring_cause_already_signalled(
        conn, event_id=4, event_type=DEAD_LETTER, signature=sig, channel=chan) is None, (
        "the 01:00 recovery after the matching 00:00 wake was missed — the re-arm is "
        "resolving the timestamp from the wrong row")


def test_a_missing_timestamp_never_silently_suppresses():
    """No `at` means no recovery evidence, but must not crash or mis-answer."""
    conn = _conn()
    assert wb._channel_delivered_since(conn, "telegram", "") is False
    assert wb._channel_delivered_since(conn, "", "2026-09-07T00:00:00+00:00") is False
