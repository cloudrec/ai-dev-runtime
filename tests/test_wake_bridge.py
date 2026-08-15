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
    # A wake needs a target. Binding one here keeps every other test about the behaviour it
    # actually covers; the unbound case is asserted explicitly in its own test.
    wb.bind_chat("https://chatgpt.com/c/default-test-chat")
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


def test_the_phrase_carries_no_free_pane_text():
    """Task 211: the phrase now carries SYSTEM-composed context (event id, trigger class,
    project, agent ref) so ChatGPT can act on it directly — but never anything that passed
    through a pane or a free-form field like correlation_id."""
    d = wb.should_wake(event_id=60, severity="critical", event_type="task_failed",
                       project_id="mess", agent_id="mess-agent:0.0",
                       correlation_id="secret-correlation-value")
    assert "secret-correlation-value" not in d["phrase"]
    assert "event=60" in d["phrase"]
    assert "trigger=failure" in d["phrase"]
    assert "project=mess" in d["phrase"]
    assert "agent=mess-agent:0.0" in d["phrase"]
    assert d["phrase"].endswith(wb.WAKE_PHRASE)


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


# ── the rotatable active control chat ──────────────────────────────────────
def test_no_active_chat_fails_closed():
    """With nowhere to wake, guessing a conversation would be exactly the arbitrary
    behaviour this design forbids. Both the registry and the legacy row must be empty —
    bind_chat keeps them in lockstep, so clearing one alone is not an unbound state."""
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    c.execute("DELETE FROM wake_target"); c.execute("DELETE FROM wake_route"); c.commit()
    d = wb.should_wake(event_id=100, severity="critical")
    assert d["wake"] is False and d["reason"] == "no_route_bound"


def test_binding_a_chat_enables_waking_and_returns_the_target():
    wb.bind_chat("https://chatgpt.com/c/aaaa-1111")
    d = wb.should_wake(event_id=101, severity="critical")
    assert d["wake"] is True
    assert d["conversation"] == "https://chatgpt.com/c/aaaa-1111"
    assert d["phrase"].endswith(wb.WAKE_PHRASE) and "event=101" in d["phrase"]


def test_rebinding_moves_the_target_without_touching_anything_else():
    """A chat fills up and gets replaced; rotation must not need a reinstall."""
    wb.bind_chat("https://chatgpt.com/c/old-one")
    r = wb.bind_chat("https://chatgpt.com/c/new-one")
    assert r["action"] == "rebind" and r["previous"] == "https://chatgpt.com/c/old-one"
    assert wb.active_chat()["conversation"] == "https://chatgpt.com/c/new-one"
    d = wb.should_wake(event_id=102, severity="critical")
    assert d["conversation"] == "https://chatgpt.com/c/new-one", "wakes follow the pointer"


@pytest.mark.parametrize("bad", [
    "https://example.com/evil", "not-a-url", "", "https://chatgpt.com/c/../etc/passwd",
    "javascript:alert(1)"])
def test_only_a_conversation_url_may_be_bound(bad):
    assert wb.bind_chat(bad)["ok"] is False


def test_an_invalid_stored_target_fails_closed(tmp_path, monkeypatch):
    """Corruption must not become a wake at an arbitrary URL — wherever it is stored."""
    import sqlite3, os
    wb.bind_chat("https://chatgpt.com/c/good")
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    c.execute("UPDATE wake_target SET conversation='https://evil.example/x' WHERE id=1")
    c.execute("UPDATE wake_route SET conversation='https://evil.example/x'")
    c.commit()
    assert wb.active_chat()["reason"] == "active_chat_invalid"
    assert wb.should_wake(event_id=103, severity="critical")["wake"] is False


def test_the_pointer_is_read_fresh_on_every_wake():
    """The URL must never be cached in code or a unit file."""
    wb.bind_chat("https://chatgpt.com/c/first")
    assert wb.should_wake(event_id=104, severity="critical")["conversation"].endswith("first")
    wb.bind_chat("https://chatgpt.com/c/second")
    assert wb.should_wake(event_id=105, severity="critical")["conversation"].endswith("second")


def test_bind_audit_records_the_move_and_no_conversation_content():
    wb.bind_chat("https://chatgpt.com/c/one", note="rotated after chat filled up")
    wb.bind_chat("https://chatgpt.com/c/two")
    h = wb.bind_history()
    assert h[0]["action"] == "rebind" and h[0]["previous"].endswith("one")
    assert set(h[0]) == {"at", "action", "conversation", "previous", "by", "note"}, \
        "the audit stores the pointer move only — never message content"


# ── the global send choke point ────────────────────────────────────────────
def test_only_one_submission_is_allowed_inside_the_cooldown():
    """The owner saw the phrase TWICE. Neither was a duplicate of the same event: one came
    from the companion and one from a direct out-of-band call that bypassed the bridge,
    recorded nothing and consumed no cooldown. Per-event dedupe cannot catch that."""
    a = wb.claim_send("companion", event_id=1, now=1000.0)
    b = wb.claim_send("operator_script", event_id=None, now=1000.0 + 55)
    assert a["allowed"] is True
    assert b["allowed"] is False and b["reason"].startswith("global_cooldown_active")


def test_a_second_send_is_allowed_once_the_cooldown_expires():
    wb.claim_send("companion", event_id=1, now=1000.0)
    later = wb.claim_send("companion", event_id=2, now=1000.0 + wb.COOLDOWN_SECS + 1)
    assert later["allowed"] is True


def test_every_attempt_is_recorded_even_when_refused():
    """An out-of-band send must be VISIBLE even when it is blocked."""
    wb.claim_send("companion", event_id=1, now=1000.0)
    wb.claim_send("rogue_script", event_id=None, now=1000.0 + 10)
    import os, sqlite3
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    rows = c.execute("SELECT source,allowed FROM wake_send ORDER BY id").fetchall()
    assert ("rogue_script", 0) in rows, rows
    assert ("companion", 1) in rows


def test_the_kill_switch_blocks_the_claim(monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "1")
    assert wb.claim_send("companion", event_id=1)["allowed"] is False


def test_a_disabled_bridge_blocks_the_claim(monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "0")
    r = wb.claim_send("companion", event_id=1)
    assert r["allowed"] is False and r["reason"] == "bridge_disabled"
