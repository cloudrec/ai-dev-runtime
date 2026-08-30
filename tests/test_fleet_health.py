"""Unit tests for core/fleet_health.py: threshold, dedupe, recovery, reminder — plus a
safe simulated end-to-end run that never touches a real server or the real Telegram
transport."""
import json
import os
import tempfile

import pytest

from core import fleet_health as fh

HOST = {"id": "test_host", "label": "Test Host", "ip": "10.0.0.1", "role": "unit test",
       "checks": [{"type": "tcp", "port": 22}]}


def test_topology_loads_real_config_five_hosts():
    hosts = fh.load_topology()
    ids = [h["id"] for h in hosts]
    assert ids == ["management", "ru_prod", "ru2", "nl_edge", "fi_edge"]
    for h in hosts:
        assert h["ip"]
        assert h["checks"], f"{h['id']} has no checks (would be ICMP-only-equivalent)"
        for c in h["checks"]:
            assert c["type"] in ("tcp", "http", "https")


# --------------------------------------------------------------------- state machine

def test_below_threshold_does_not_alert():
    st = None
    now = 1000.0
    for i in range(fh.DEFAULT_FAIL_THRESHOLD - 1):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now + i)
        assert alert is None
    assert st["state"] != "down"


def test_threshold_crossed_fires_down_alert_once():
    st = None
    now = 1000.0
    alert = None
    for i in range(fh.DEFAULT_FAIL_THRESHOLD):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now + i)
    assert alert is not None
    assert alert["kind"] == "down"
    assert st["state"] == "down"
    # first_fail_ts is the FIRST failing probe, not the one that crossed threshold
    assert alert["first_fail_ts"] == now


def test_dedupe_no_repeat_alert_before_reminder_window():
    st = None
    now = 1000.0
    for i in range(fh.DEFAULT_FAIL_THRESHOLD):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now + i)
    assert alert["kind"] == "down"
    # keep failing, well inside the reminder window — must NOT alert again
    for i in range(1, 20):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"},
                                     st, now_ts=now + fh.DEFAULT_FAIL_THRESHOLD + i,
                                     reminder_interval_secs=6 * 3600)
        assert alert is None, "repeated identical DOWN alert before reminder window"
    assert st["state"] == "down"


def test_reminder_fires_after_interval_while_still_down():
    st = None
    now = 1000.0
    for i in range(fh.DEFAULT_FAIL_THRESHOLD):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now + i,
                                     reminder_interval_secs=100)
    assert alert["kind"] == "down"
    last_alert_ts = st["last_alert_ts"]
    st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st,
                                 now_ts=last_alert_ts + 101, reminder_interval_secs=100)
    assert alert is not None
    assert alert["kind"] == "reminder"


def test_recovery_requires_consecutive_ok_and_reports_duration():
    st = None
    now = 1000.0
    for i in range(fh.DEFAULT_FAIL_THRESHOLD):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now + i)
    down_started_ts = alert["first_fail_ts"]
    fail_end = now + fh.DEFAULT_FAIL_THRESHOLD

    # first success alone must not clear it if recovery_threshold > 1
    st, alert = fh.evaluate_host("h", {"ok": True, "summary": "up"}, st,
                                 now_ts=fail_end + 1, recovery_threshold=2)
    if fh.DEFAULT_RECOVERY_THRESHOLD > 1:
        assert alert is None
        assert st["state"] == "down"

    st, alert = fh.evaluate_host("h", {"ok": True, "summary": "up"}, st,
                                 now_ts=fail_end + 2, recovery_threshold=2)
    assert alert is not None
    assert alert["kind"] == "recovered"
    assert alert["first_fail_ts"] == down_started_ts
    assert alert["duration_secs"] == pytest.approx(fail_end + 2 - down_started_ts)
    assert st["state"] == "up"


def test_flap_below_threshold_resets_without_alert():
    st = None
    now = 1000.0
    st, alert = fh.evaluate_host("h", {"ok": False, "summary": "down"}, st, now_ts=now)
    assert alert is None
    st, alert = fh.evaluate_host("h", {"ok": True, "summary": "up"}, st, now_ts=now + 1)
    assert alert is None
    assert st["consecutive_fail"] == 0
    assert st["first_fail_ts"] is None


def test_format_alert_contains_required_fields():
    st = None
    now = 1000.0
    for i in range(fh.DEFAULT_FAIL_THRESHOLD):
        st, alert = fh.evaluate_host("h", {"ok": False, "summary": "tcp:22 failed"}, st, now_ts=now + i)
    msg = fh.format_alert(HOST, alert)
    assert "DOWN" in msg
    assert HOST["ip"] in msg
    assert HOST["label"] in msg
    assert "First failure" in msg
    assert "tcp:22 failed" in msg


# ------------------------------------------------------------------------- run_once

def test_run_once_persists_state_and_calls_send_on_threshold(tmp_path):
    state_path = str(tmp_path / "state.json")
    sent = []

    def fake_probe(host):
        return {"ok": False, "checks": [{"ok": False, "detail": "tcp:22 failed"}],
               "summary": "tcp:22 failed"}

    def fake_send(msg):
        sent.append(msg)
        return (True, "telegram:123", None)

    clock = {"t": 1000.0}
    def fake_now():
        clock["t"] += 1
        return clock["t"]

    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        summary = fh.run_once([HOST], probe_fn=fake_probe, send_fn=fake_send,
                              state_path=state_path, now_fn=fake_now)

    assert summary["any_down"] is True
    assert len(sent) == 1
    assert "DOWN" in sent[0]

    with open(state_path) as f:
        saved = json.load(f)
    assert saved["test_host"]["state"] == "down"

    # a second run without recovery must not resend within the reminder window
    summary2 = fh.run_once([HOST], probe_fn=fake_probe, send_fn=fake_send,
                           state_path=state_path, now_fn=fake_now)
    assert len(sent) == 1


def test_run_once_recovery_sends_recovered(tmp_path):
    state_path = str(tmp_path / "state.json")
    sent = []
    probe_ok = {"v": False}

    def fake_probe(host):
        if probe_ok["v"]:
            return {"ok": True, "checks": [], "summary": "tcp:22 open"}
        return {"ok": False, "checks": [], "summary": "tcp:22 failed"}

    def fake_send(msg):
        sent.append(msg)
        return (True, "telegram:1", None)

    clock = {"t": 1000.0}
    def fake_now():
        clock["t"] += 1
        return clock["t"]

    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        fh.run_once([HOST], probe_fn=fake_probe, send_fn=fake_send, state_path=state_path, now_fn=fake_now)
    assert len(sent) == 1 and "DOWN" in sent[0]

    probe_ok["v"] = True
    for _ in range(fh.DEFAULT_RECOVERY_THRESHOLD):
        fh.run_once([HOST], probe_fn=fake_probe, send_fn=fake_send, state_path=state_path, now_fn=fake_now)

    assert len(sent) == 2
    assert "RECOVERED" in sent[1]


def test_probe_host_never_touches_a_real_server_when_mocked(tmp_path, monkeypatch):
    """Safety check for the end-to-end 'simulated failure' requirement: prove that a
    down-server condition can be driven entirely through the public probe_fn seam
    without opening any socket, i.e. no real server is ever contacted."""
    calls = []

    def refuse_any_network(*a, **k):
        calls.append((a, k))
        raise AssertionError("real network call attempted during simulated test")

    monkeypatch.setattr(fh.socket, "create_connection", refuse_any_network)
    monkeypatch.setattr(fh.subprocess, "run", refuse_any_network)

    state_path = str(tmp_path / "state.json")

    def simulated_down(host):
        return {"ok": False, "checks": [], "summary": "SIMULATED failure, no network used"}

    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        summary = fh.run_once([HOST], probe_fn=simulated_down, send_fn=lambda m: (True, "sim", None),
                              state_path=state_path)
    assert summary["any_down"] is True
    assert calls == []


# ------------------------------------------------------------ ChatGPT-wake fallback
# Added after discovering the 2026-08-18 19:35-20:00 UTC RU-PROD outage's DOWN and
# RECOVERED alerts both failed Telegram delivery with nowhere else to go — unlike the
# rest of Owner OS, this module had no fallback. These tests cover the fix.

def test_wake_fallback_not_called_when_telegram_succeeds(tmp_path):
    fallback_calls = []
    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        fh.run_once([HOST], probe_fn=lambda h: {"ok": False, "checks": [], "summary": "down"},
                   send_fn=lambda m: (True, "telegram:1", None),
                   wake_fallback_fn=lambda host, alert: fallback_calls.append((host, alert)) or {"event_id": 1},
                   state_path=str(tmp_path / "state.json"))
    assert fallback_calls == [], "a working Telegram send must never trigger the fallback"


def test_wake_fallback_called_when_telegram_fails(tmp_path):
    fallback_calls = []
    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        fh.run_once([HOST], probe_fn=lambda h: {"ok": False, "checks": [], "summary": "down"},
                   send_fn=lambda m: (False, None, "telegram send failed: Bad Request: chat not found"),
                   wake_fallback_fn=lambda host, alert: fallback_calls.append((host, alert)) or {"event_id": 42},
                   state_path=str(tmp_path / "state.json"))
    assert len(fallback_calls) == 1
    host, alert = fallback_calls[0]
    assert host["id"] == "test_host"
    assert alert["kind"] == "down"


def test_wake_fallback_result_recorded_in_run_once_results(tmp_path):
    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        summary = fh.run_once([HOST], probe_fn=lambda h: {"ok": False, "checks": [], "summary": "down"},
                              send_fn=lambda m: (False, None, "err"),
                              wake_fallback_fn=lambda host, alert: {"event_id": 99},
                              state_path=str(tmp_path / "state.json"))
    down_result = [r for r in summary["results"] if r["alert"]][0]
    assert down_result["wake_fallback"] == {"event_id": 99}


def test_wake_fallback_disabled_still_dead_letters_cleanly(tmp_path):
    """wake_fallback_fn=None must not break the run — Telegram-only behavior preserved."""
    for _ in range(fh.DEFAULT_FAIL_THRESHOLD):
        summary = fh.run_once([HOST], probe_fn=lambda h: {"ok": False, "checks": [], "summary": "down"},
                              send_fn=lambda m: (False, None, "err"),
                              wake_fallback_fn=None,
                              state_path=str(tmp_path / "state.json"))
    down_result = [r for r in summary["results"] if r["alert"]][0]
    assert down_result["wake_fallback"] is None
    assert summary["any_down"] is True


def test_emit_wake_fallback_uses_cto_emit_and_returns_its_result(monkeypatch):
    """Isolated unit test of emit_wake_fallback itself, stubbing cto.emit so no real
    control_plane.db write happens here — that's covered by the control-plane test
    suite's own conventions."""
    calls = []

    def fake_emit(source, type_, *, severity, owner_action_required, payload,
                 action_taken, dedup_key, dedup_window_secs):
        calls.append(locals())
        return {"event_id": 7}

    import core.control_plane.cto as cto_module
    monkeypatch.setattr(cto_module, "emit", fake_emit)

    alert = {"kind": "down", "detail": "tcp:22 failed", "first_fail_ts": 1000.0,
             "duration_secs": 30.0}
    result = fh.emit_wake_fallback(HOST, alert)

    assert result == {"event_id": 7}
    assert len(calls) == 1
    c = calls[0]
    assert c["source"] == "fleet_health"
    assert c["type_"] == "fleet_host_down"
    assert c["severity"] == "critical"
    assert c["owner_action_required"] is True
    assert c["payload"]["host_id"] == "test_host"
    assert c["dedup_key"] == f"fleet:test_host:down:1000"


def test_emit_wake_fallback_recovered_is_high_not_critical(monkeypatch):
    calls = []

    def fake_emit(source, type_, *, severity, owner_action_required, **kw):
        calls.append({"severity": severity, "owner_action_required": owner_action_required,
                      "type_": type_})
        return {"event_id": 8}

    import core.control_plane.cto as cto_module
    monkeypatch.setattr(cto_module, "emit", fake_emit)

    alert = {"kind": "recovered", "detail": "up", "first_fail_ts": 1000.0, "duration_secs": 30.0}
    fh.emit_wake_fallback(HOST, alert)

    assert calls[0]["severity"] == "high"
    assert calls[0]["owner_action_required"] is False
    assert calls[0]["type_"] == "fleet_host_recovered"


def test_emit_wake_fallback_never_raises_when_cto_unavailable(monkeypatch):
    """control_plane may be unavailable in some contexts — must degrade to None, not
    crash the probe run."""
    import sys
    monkeypatch.setitem(sys.modules, "core.control_plane.cto", None)
    alert = {"kind": "down", "detail": "x", "first_fail_ts": 1.0, "duration_secs": 1.0}
    # None in sys.modules makes `from ... import emit` raise ImportError — exercised here.
    result = fh.emit_wake_fallback(HOST, alert)
    assert result is None
