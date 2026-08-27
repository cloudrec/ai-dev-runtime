"""Regression coverage for the event 10063 class (2026-08-27, gaika-server).

`cdp_composer._attempt` refused with `composer_ambiguous_or_absent:0` (the page was
mid-render, composer not mounted yet), and the wake sat pending for over five minutes
because every delivery failure — a wedged page, a dead chat, a momentary composer
miss — shared one 300s bench. `wake_bridge` now gives ONLY this one transient reason
class a short, bounded fast-retry lane; everything else keeps the original floor that
event 4214 put there. These tests pin: fast retry -> exactly one delivery, no
duplicate; the fast lane is itself bounded, not a hot-loop; and a genuinely cleared
condition still retracts the wake before the fast lane ever fires again.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from core import wake_bridge as wb
from core import wake_routes as wr

OWNER = "https://chatgpt.com/c/gaika-extension-chat"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


def _decide(event_id, project_id, *, now, event_type="agent_waiting_input"):
    d = wb.should_wake(event_id=event_id, severity="high", event_type=event_type,
                       project_id=project_id, now=now)
    wb.record(d, event_id=event_id, severity="high", event_type=event_type,
              project_id=project_id, now=now)
    return d


def test_ambiguous_composer_then_available_is_exactly_one_delivery():
    """Attempt 1: composer read 0 matches. Attempt 2, once the fast lane's window
    has elapsed: composer is present and the send lands. Exactly one successful
    wake, no duplicate — the event 10063 shape, fixed."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(10063, "gaika-server", now=1_000.0)

    p0 = wb.pending_wake(now=1_000.5)
    assert p0["pending"] and p0["event_id"] == 10063
    wb.record_delivery("companion", event_id=10063, delivered=False,
                       reason="composer_ambiguous_or_absent:0",
                       conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=1_001.0)
    assert wb.was_submitted(10063) is False        # nothing was typed, safe to retry

    # Still inside the fast-lane window: benched, not offered.
    still_benched = wb.pending_wake(now=1_001.0 + wb.TRANSIENT_RETRY_BACKOFF_SECS - 5)
    assert still_benched["pending"] is False
    assert still_benched["reason"] == "retry_backoff_pending"
    assert still_benched["event_id"] == 10063
    assert still_benched["transient_retry"] is True
    assert still_benched["attempt"] == 1
    assert still_benched["next_retry_in_secs"] <= 5

    # Well short of the standard 300s floor: proves this really is the FAST lane.
    retry_at = 1_001.0 + wb.TRANSIENT_RETRY_BACKOFF_SECS + 1
    assert retry_at - 1_000.0 < wb.RETRY_BACKOFF_SECS

    p1 = wb.pending_wake(now=retry_at)
    assert p1["pending"] and p1["event_id"] == 10063
    wb.mark_submitted(10063, source="companion")
    wb.record_delivery("companion", event_id=10063, delivered=True,
                       reason="submitted_and_assistant_started_generating",
                       conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=retry_at + 5)
    wb.acknowledge(10063)

    assert wb.pending_wake(now=retry_at + 10)["pending"] is False
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    delivered_rows = conn.execute(
        "SELECT delivered FROM wake_delivery WHERE event_id=10063 ORDER BY id").fetchall()
    conn.close()
    assert [d for (d,) in delivered_rows] == [0, 1], "attempt 1 failed, attempt 2 succeeded"
    assert sum(d for (d,) in delivered_rows) == 1, "exactly one successful delivery"


def test_repeated_ambiguity_falls_back_to_standard_backoff_not_a_hot_loop():
    """The fast lane is bounded: once an event has failed with the transient reason
    more than TRANSIENT_RETRY_MAX_ATTEMPTS times in a row, it stands down onto the
    original 300s floor instead of retrying every 30s forever. The event remains
    retryable on that slower cadence — a bounded safe end, not a dead letter."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(20001, "gaika-server", now=2_000.0)

    t = 2_000.0
    for i in range(wb.TRANSIENT_RETRY_MAX_ATTEMPTS):
        p = wb.pending_wake(now=t + 0.5)
        assert p["pending"] and p["event_id"] == 20001, f"attempt {i + 1} should be offered"
        wb.record_delivery("companion", event_id=20001, delivered=False,
                           reason=f"composer_ambiguous_or_absent:{i % 2}",
                           conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=t + 1.0)
        t += wb.TRANSIENT_RETRY_BACKOFF_SECS + 1

    # One more transient failure pushes the streak past the attempt cap.
    p_last_fast = wb.pending_wake(now=t + 0.5)
    assert p_last_fast["pending"] and p_last_fast["event_id"] == 20001
    wb.record_delivery("companion", event_id=20001, delivered=False,
                       reason="composer_ambiguous_or_absent:0",
                       conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=t + 1.0)

    # Past the fast-lane window but nowhere near the standard floor: must now be benched.
    just_past_fast_lane = t + 1.0 + wb.TRANSIENT_RETRY_BACKOFF_SECS + 1
    stood_down = wb.pending_wake(now=just_past_fast_lane)
    assert stood_down["pending"] is False, "the fast lane must stand down past the cap"
    assert stood_down["reason"] == "retry_backoff_pending"
    assert stood_down["transient_retry"] is False, "reporting reverts to the standard lane"

    # But it is not stuck forever: the standard floor still eventually retries it.
    standard_retry = t + 1.0 + wb.RETRY_BACKOFF_SECS + 1
    p_recovered = wb.pending_wake(now=standard_retry)
    assert p_recovered["pending"] and p_recovered["event_id"] == 20001


def test_condition_clearing_retracts_the_wake_before_the_fast_lane_fires_again():
    """If whatever the agent was waiting on is proven resolved (agent_watch's
    audited invalid-overlay) while a composer-ambiguous retry is benched, the wake
    must be retired — not delivered late into a chat nobody is blocked in anymore."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(30001, "gaika-server", now=3_000.0)
    p0 = wb.pending_wake(now=3_000.5)
    assert p0["pending"] and p0["event_id"] == 30001
    wb.record_delivery("companion", event_id=30001, delivered=False,
                       reason="composer_ambiguous_or_absent:0",
                       conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=3_001.0)

    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    conn.execute("CREATE TABLE IF NOT EXISTS agent_alert_invalid (event_id INTEGER "
                "PRIMARY KEY, at TEXT, ts REAL, by TEXT, reason TEXT)")
    conn.execute("INSERT INTO agent_alert_invalid (event_id, at, ts, by, reason) "
                "VALUES (30001, 'now', 3001.0, 'agent_watch', 'agent resumed on its own')")
    conn.commit()
    conn.close()

    # Still inside what would have been the fast-lane retry window.
    p1 = wb.pending_wake(now=3_001.0 + wb.TRANSIENT_RETRY_BACKOFF_SECS - 5)
    assert p1["pending"] is False
    assert p1["reason"] != "retry_backoff_pending", "retired, not merely benched"

    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = conn.execute("SELECT reason FROM wake_expire_audit WHERE event_id=30001").fetchone()
    submitted = conn.execute(
        "SELECT 1 FROM wake_submitted WHERE event_id=30001").fetchone()
    conn.close()
    assert row and row[0] == "marked_invalid"
    assert submitted is None, "a cleared condition must never be delivered late"


def test_a_non_transient_failure_still_uses_the_original_floor():
    """The fast lane is scoped to composer_ambiguous_or_absent alone; a wedged
    renderer (event 4214's shape) must keep the 300s floor, unchanged."""
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    _decide(40001, "gaika-server", now=4_000.0)
    wb.pending_wake(now=4_000.5)
    wb.record_delivery("companion", event_id=40001, delivered=False,
                       reason="renderer_unresponsive",
                       conversation=OWNER, route_key=wr.FALLBACK_ROUTE, now=4_001.0)
    just_past_fast_lane = 4_001.0 + wb.TRANSIENT_RETRY_BACKOFF_SECS + 1
    assert wb.pending_wake(now=just_past_fast_lane)["pending"] is False
    assert wb.pending_wake(now=4_001.0 + wb.RETRY_BACKOFF_SECS + 1)["pending"] is True
