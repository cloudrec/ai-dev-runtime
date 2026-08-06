"""Wake bridge server half: decide, dedupe, cool down, stop on ack, and stay off by default."""
from __future__ import annotations

import pytest

from core import wake_bridge as wb


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


def _wake(event_id, severity="critical", now=1000.0, **kw):
    d = wb.should_wake(event_id=event_id, severity=severity, now=now, **kw)
    wb.record(d, event_id=event_id, severity=severity, now=now)
    return d


# ── off unless explicitly enabled ──────────────────────────────────────────
def test_the_bridge_is_off_unless_enabled(monkeypatch):
    """An automation that can poke a chat must be opt-in, never on by import."""
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "0")
    assert wb.should_wake(event_id=1, severity="critical")["reason"] == "bridge_disabled"


def test_the_kill_switch_overrides_an_explicit_enable(monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "1")
    d = wb.should_wake(event_id=1, severity="critical")
    assert d["wake"] is False and d["reason"] == "kill_switch_engaged"


def test_switches_are_read_at_decision_time(monkeypatch):
    """Flipping the kill switch must take effect without restarting the service."""
    assert wb.should_wake(event_id=1, severity="critical")["wake"] is True
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "1")
    assert wb.should_wake(event_id=1, severity="critical")["wake"] is False


# ── only urgent things, exactly once ───────────────────────────────────────
def test_routine_severity_never_wakes():
    assert _wake(1, severity="info")["wake"] is False
    assert _wake(2, severity="warning")["wake"] is False


def test_an_owner_decision_wakes_even_at_low_severity():
    d = wb.should_wake(event_id=3, severity="info", owner_action_required=True)
    assert d["wake"] is True


def test_the_same_event_never_wakes_twice():
    assert _wake(10, now=1000.0)["wake"] is True
    again = _wake(10, now=1000.0 + wb.COOLDOWN_SECS * 5)
    assert again["wake"] is False and again["reason"] == "already_woke_for_this_event"


def test_distinct_events_still_respect_the_cooldown():
    """A burst of different events must not become a burst of wakes."""
    assert _wake(20, now=1000.0)["wake"] is True
    d = _wake(21, now=1000.0 + 10)
    assert d["wake"] is False and d["reason"] == "cooldown_active"
    assert d["wait_secs"] > 0
    assert _wake(22, now=1000.0 + wb.COOLDOWN_SECS + 1)["wake"] is True


def test_acknowledgement_is_recorded_and_stops_repeat_wakes():
    _wake(30, now=1000.0)
    wb.acknowledge(30)
    d = _wake(30, now=1000.0 + wb.COOLDOWN_SECS * 3)
    assert d["wake"] is False and d["acknowledged"] is True


# ── auditability ───────────────────────────────────────────────────────────
def test_refusals_are_audited_not_just_successes():
    """A bridge that records only its successes cannot be debugged when it stays silent."""
    _wake(40, severity="info")
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = c.execute("SELECT decision,reason FROM wake_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "skip" and row[1] == "severity_below_wake_threshold"


def test_health_reports_freshness_and_ack_state():
    h = wb.health()
    assert h["enabled"] is True and h["wakes_total"] == 0
    _wake(50, now=1000.0)
    h = wb.health(now=1000.0 + 60)
    assert h["wakes_total"] == 1 and h["last_wake_event_id"] == 50
    assert h["last_wake_acknowledged"] is False and h["last_wake_age_secs"] == 60
    wb.acknowledge(50)
    assert wb.health()["last_wake_acknowledged"] is True


def test_the_phrase_carries_no_event_content():
    """The companion submits one fixed phrase; leaking event text would make this a channel."""
    d = wb.should_wake(event_id=60, severity="critical",
                       correlation_id="secret-correlation-value")
    assert d["phrase"] == wb.WAKE_PHRASE
    assert "secret-correlation-value" not in d["phrase"]
    assert "60" not in d["phrase"]


# ── integration: consulted only when enabled ───────────────────────────────
def test_a_disabled_bridge_writes_no_audit_rows(monkeypatch):
    """Otherwise every urgent event leaves a `skip: bridge_disabled` row and the audit trail
    is noise before the bridge is ever used."""
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "0")
    from core.control_plane import cto
    cto.emit("test", "urgent_thing", agent_id="a:0.0", severity="critical")
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    n = c.execute("SELECT COUNT(*) FROM wake_audit").fetchone()[0] \
        if c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='wake_audit'"
                     ).fetchone()[0] else 0
    assert n == 0


def test_an_enabled_bridge_decides_on_an_urgent_event():
    from core.control_plane import cto
    out = cto.emit("test", "urgent_thing", agent_id="a:0.0", severity="critical")
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = c.execute("SELECT event_id,decision FROM wake_audit ORDER BY id DESC LIMIT 1"
                    ).fetchone()
    assert row[0] == out["event_id"] and row[1] == "wake"


def test_a_broken_bridge_never_breaks_event_recording(monkeypatch):
    from core.control_plane import cto
    from core import wake_bridge as wb
    monkeypatch.setattr(wb, "should_wake", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = cto.emit("test", "urgent_thing", agent_id="a:0.0", severity="critical")
    assert out["event_id"] > 0, "the event is recorded regardless"
