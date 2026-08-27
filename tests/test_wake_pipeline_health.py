"""Is the wake pipeline MOVING? (observability for unattended operation)

`health()` answered "did we wake recently and did it land?", which is not the
same question. Event 9870 proved the gap live: it was decided for the gaika-drop
chat and sat undelivered for fifteen minutes while health looked green, because
a DIFFERENT chat had just been delivered to successfully. Nothing said "something
decided is not moving", so the owner found out by noticing silence.

pipeline_health() reports on movement: what is pending and for how long, per
route; whether the deliverer is even attempting claims; and whether deliveries
are failing in a row. It writes nothing and emits no event - the wake path
feeding itself is a failure this system has already had.
"""
from __future__ import annotations

import pytest

from core import wake_bridge


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    from core.control_plane import store
    c = store.connect()
    store.init_db(c)
    wake_bridge._conn(c)
    # wake_send / wake_delivery are created lazily by the writers; the health
    # reader must work before either has ever run, so build them here.
    c.execute(wake_bridge._SEND_SCHEMA)
    c.execute(wake_bridge._DELIVERY_SCHEMA)
    c.execute(wake_bridge._SUBMIT_SCHEMA)
    wake_bridge._migrate_send(c)
    wake_bridge._migrate_delivery(c)
    c.commit()
    return c


NOW = 1_800_000_000.0


def _pending_wake(conn, *, event_id, route, ts):
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                 "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',1,?,?,0)",
                 (ts, "2026-08-27T18:00:00+00:00", event_id, route, route))
    conn.commit()


def _claim(conn, *, ts, allowed=1):
    conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason,actionable,"
                 "route_key) VALUES (?,?,'companion',1,?,'t',1,'owner-os')",
                 (ts, "2026-08-27T18:00:00+00:00", allowed))
    conn.commit()


def _delivery(conn, *, delivered, ts_at="2026-08-27T18:00:00+00:00"):
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,"
                 "conversation,route_key) VALUES (?,?,'companion',1,?,'t','c','owner-os')",
                 (NOW, ts_at, delivered))
    conn.commit()


def test_an_empty_quiet_pipeline_is_ok(conn):
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "ok"
    assert h["reasons"] == []
    assert h["pending_count"] == 0


def test_a_wake_pending_past_the_threshold_is_reported_stuck(conn):
    """The 9870 case, isolated."""
    _pending_wake(conn, event_id=9870, route="gaika-drop",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 60)
    _claim(conn, ts=NOW - 10)                      # companion IS alive and trying
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert h["pending_oldest_route"] == "gaika-drop"
    assert any(r.startswith("pending_wake_stuck:gaika-drop") for r in h["reasons"])


def test_a_recent_delivery_to_another_chat_does_not_mask_it(conn):
    """Exactly what made this invisible: a healthy owner-os delivery seconds ago
    while a project chat's wake had been waiting a quarter of an hour."""
    _pending_wake(conn, event_id=9870, route="gaika-drop",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 60)
    _claim(conn, ts=NOW - 5)
    _delivery(conn, delivered=1)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert h["pending_by_route"]["gaika-drop"] > wake_bridge.STUCK_PENDING_SECS


def test_a_silent_companion_is_detected_even_with_work_queued(conn):
    """If the deliverer process dies, deliveries simply stop. The last successful
    delivery keeps looking recent, so only its CLAIM silence reveals it."""
    _pending_wake(conn, event_id=1, route="owner-os", ts=NOW - 30)
    _claim(conn, ts=NOW - wake_bridge.COMPANION_SILENT_SECS - 60)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("companion_silent") for r in h["reasons"])


def test_a_silent_companion_with_nothing_queued_is_not_an_alarm(conn):
    """Quiet is not broken. No pending work means no claim is expected."""
    _claim(conn, ts=NOW - wake_bridge.COMPANION_SILENT_SECS - 600)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "ok"


def test_a_companion_that_never_claimed_is_reported(conn):
    _pending_wake(conn, event_id=1, route="owner-os", ts=NOW - 30)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert "companion_never_claimed" in h["reasons"]


def test_consecutive_delivery_failures_are_surfaced(conn):
    for _ in range(wake_bridge.CONSECUTIVE_FAILURE_LIMIT):
        _delivery(conn, delivered=0)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["consecutive_delivery_failures"] >= wake_bridge.CONSECUTIVE_FAILURE_LIMIT
    assert any(r.startswith("consecutive_delivery_failures") for r in h["reasons"])


def test_a_success_after_failures_clears_the_streak(conn):
    _delivery(conn, delivered=0)
    _delivery(conn, delivered=0)
    _delivery(conn, delivered=1)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["consecutive_delivery_failures"] == 0


def test_backlog_is_reported_per_route_not_averaged(conn):
    _pending_wake(conn, event_id=1, route="mess", ts=NOW - 100)
    _pending_wake(conn, event_id=2, route="treasure", ts=NOW - 900)
    _claim(conn, ts=NOW - 5)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["pending_by_route"]["mess"] == 100
    assert h["pending_by_route"]["treasure"] == 900


def test_an_acknowledged_wake_is_not_pending(conn):
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                 "route_key,acknowledged) VALUES (?,?,?,'wake','t',1,'mess',1)",
                 (NOW - 5000, "2026-08-27T18:00:00+00:00", 42))
    conn.commit()
    assert wake_bridge.pipeline_health(conn=conn, now=NOW)["pending_count"] == 0


def test_the_kill_switch_is_reported_without_being_called_stuck(conn, monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "1")
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "disabled"
    assert "kill_switch_engaged" in h["reasons"]


def test_pipeline_health_writes_nothing(conn):
    """It must be safe to poll continuously."""
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("wake_audit", "wake_send", "wake_delivery", "wake_submitted")]
    for _ in range(3):
        wake_bridge.pipeline_health(conn=conn, now=NOW)
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("wake_audit", "wake_send", "wake_delivery", "wake_submitted")]
    assert before == after


def test_health_embeds_the_pipeline_verdict(conn):
    h = wake_bridge.health(conn=conn, now=NOW)
    assert "pipeline" in h and h["pipeline"]["status"] in ("ok", "stuck", "disabled")
