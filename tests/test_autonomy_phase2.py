"""Autonomy phase 2: durable terminal state + safe dead-session recovery.

Removes two v1 limitations, each of which had bitten live:
  * terminal was read from the visible pane, so a finished project was resumed again once
    its completion text scrolled away;
  * an externally killed managed session needed a human to restart it.

Both features are fail-closed: a corrupt state store reads as "not finished" (keep
working), and an unregistered or ambiguous session is never revived.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from core import project_state as ps
from core import session_recovery as sr
from core import commander_autopilot as ap


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


# ═════════════ A. durable terminal state ════════════════════════════════════
def test_terminal_marker_persists_and_is_sticky(tmp_path):
    cwd = str(tmp_path)
    r = ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="suite green",
                           evidence="all tests passed")
    assert r["recorded"] is True
    st = ps.get_state("t:0.0", cwd)
    assert st["status"] == "terminal_pass"
    assert ps.material_change(st, cwd=cwd)["reopen"] is False


def test_pane_scroll_can_never_reopen_a_terminal_marker(tmp_path):
    """The v1 defect: completion text scrolling out of the capture window resumed the
    project. `material_change` cannot even see pane text."""
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x")
    st = ps.get_state("t:0.0", cwd)
    for _ in range(5):
        assert ps.material_change(st, cwd=cwd)["reopen"] is False


def test_non_terminal_status_is_refused(tmp_path):
    r = ps.record_terminal("t:0.0", str(tmp_path), status="working", reason="no")
    assert r["recorded"] is False


def test_owner_command_reopens(tmp_path):
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x")
    st = ps.get_state("t:0.0", cwd)
    assert ps.material_change(st, cwd=cwd, owner_command=True) == {
        "reopen": True, "reason": "owner_command"}


def test_new_queued_task_reopens(tmp_path):
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x")
    st = ps.get_state("t:0.0", cwd)
    assert ps.material_change(st, cwd=cwd, new_queued_task=True)["reason"] == "new_queued_task"


def test_git_head_change_reopens(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    monkeypatch.setattr(ps, "git_head", lambda c: "a" * 40)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x")
    st = ps.get_state("t:0.0", cwd)
    monkeypatch.setattr(ps, "git_head", lambda c: "b" * 40)
    chg = ps.material_change(st, cwd=cwd)
    assert chg["reopen"] is True and chg["reason"] == "git_head_changed"


def test_report_update_reopens(tmp_path):
    cwd = str(tmp_path)
    rp = tmp_path / "report.md"
    rp.write_text("v1")
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x",
                       report_path=str(rp))
    st = ps.get_state("t:0.0", cwd)
    time.sleep(0.02)
    os.utime(str(rp), (time.time() + 60, time.time() + 60))
    assert ps.material_change(st, cwd=cwd)["reason"] == "report_updated"


def test_freshness_deadline_reopens(tmp_path):
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x",
                       freshness_secs=1)
    st = ps.get_state("t:0.0", cwd)
    assert ps.material_change(st, cwd=cwd, now=time.time() + 5)["reason"] == \
        "freshness_deadline_passed"


def test_corrupt_state_fails_closed(tmp_path):
    """A damaged row must read as 'no marker' — keep working, never silently 'finished'."""
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_pass", reason="done", evidence="x")
    conn = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    conn.execute("UPDATE project_state SET status='garbage'")
    conn.commit()
    conn.close()
    assert ps.get_state("t:0.0", cwd) is None


def test_unreadable_store_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", "/nonexistent-dir/nope.db")
    assert ps.get_state("t:0.0", str(tmp_path)) is None
    assert ps.readout() == []


def test_readout_and_audit_trail(tmp_path):
    cwd = str(tmp_path)
    ps.record_terminal("t:0.0", cwd, status="terminal_blocked", reason="hardware",
                       evidence="needs a physical device")
    rows = ps.readout("t:0.0")
    assert rows and rows[0]["status"] == "terminal_blocked"
    audit = ps.audit_trail("t:0.0")
    assert any(a["action"] == "record_terminal" for a in audit)
    ps.reopen("t:0.0", cwd, "owner_command")
    assert ps.get_state("t:0.0", cwd) is None
    assert any(a["action"] == "reopen" for a in ps.audit_trail("t:0.0"))


def test_autopilot_honours_a_sticky_terminal_over_pane_text(tmp_path):
    """End to end: the marker outranks a pane that looks like fresh unfinished work."""
    cwd = str(tmp_path)
    ps.record_terminal("cp-canary:0.0", cwd, status="terminal_pass", reason="done",
                       evidence="x")
    reg = {"cp-canary:0.0": {"root": cwd, "next_step": "continue with the next safe step",
                             "live_actuation": True}}
    d = ap.evaluate("cp-canary:0.0", state="idle",
                    tail="3 tasks (1 done, 0 in progress, 2 open)\n", registry=reg)
    assert d["decision"] == "terminal_sticky", d


def test_autopilot_reopens_on_material_change(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    monkeypatch.setattr(ps, "git_head", lambda c: "a" * 40)
    ps.record_terminal("cp-canary:0.0", cwd, status="terminal_pass", reason="done",
                       evidence="x")
    monkeypatch.setattr(ps, "git_head", lambda c: "b" * 40)
    reg = {"cp-canary:0.0": {"root": cwd, "next_step": "continue with the next safe step",
                             "live_actuation": True}}
    d = ap.evaluate("cp-canary:0.0", state="idle",
                    tail="3 tasks (1 done, 0 in progress, 2 open)\n", registry=reg)
    assert d["decision"] == "poke" and d.get("reopened") == "git_head_changed", d


# ═════════════ B. safe dead-session recovery ════════════════════════════════
REG = {"sessions": {"cp-canary:0.0": {"target": "cp-canary:0.0", "session": "cp-canary",
                                      "cwd": "/root/cp-canary-v2",
                                      "conversation_id": "conv-1",
                                      "resume_shape": "claude --resume {conversation_id}",
                                      "enabled": True}},
       "limits": {"max_recoveries_per_target": 3, "window_secs": 21600,
                  "backoff_base_secs": 0}}


def test_unregistered_target_is_never_revived():
    out = sr.recover("payment:0.0", registry=REG)
    assert out["recovered"] is False and out["reason"] == "not_registered"


def test_shipped_registry_excludes_payment():
    reg = sr.load_registry()
    assert "payment:0.0" not in (reg.get("sessions") or {})
    assert sorted(reg["sessions"]) == ["arbitrage2-opus:0.0", "cp-canary:0.0",
                                       "mess-qa-automation:0.0"]


def test_disabled_entry_is_not_revived():
    reg = {"sessions": {"x:0.0": {**REG["sessions"]["cp-canary:0.0"], "target": "x:0.0",
                                  "enabled": False}}, "limits": {}}
    assert sr.recover("x:0.0", registry=reg)["reason"] == "disabled_in_registry"


def test_broken_registry_yields_nothing_recoverable(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("sessions: [[[")
    assert sr.load_registry(str(p))["sessions"] == {}


def test_alive_pane_is_left_alone(monkeypatch):
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": False})
    assert sr.recover("cp-canary:0.0", registry=REG)["reason"] == "already_alive"


def test_duplicate_claude_for_same_cwd_blocks_recovery(monkeypatch):
    """The one-pane-per-project invariant, proven before acting."""
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "")
    monkeypatch.setattr(sr, "live_claude_for_cwd",
                        lambda cwd, exclude_target="": [{"target": "other:0.0"}])
    out = sr.recover("cp-canary:0.0", registry=REG)
    assert out["recovered"] is False and out["reason"] == "live_claude_exists_for_cwd"


def test_deliberate_stop_is_never_undone(monkeypatch):
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "quarantine: stopped by owner")
    monkeypatch.setattr(sr, "live_claude_for_cwd", lambda cwd, exclude_target="": [])
    assert sr.recover("cp-canary:0.0", registry=REG)["reason"] == "deliberate_stop"


def test_successful_recovery_verifies_before_reporting_ok(monkeypatch):
    _interrupted_mid_task(monkeypatch)
    calls = []
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "❯ ")
    monkeypatch.setattr(sr, "live_claude_for_cwd", lambda cwd, exclude_target="": [])
    monkeypatch.setattr(sr, "choose_summary_if_offered",
                        lambda *a, **k: {"offered": False, "chosen": None})
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": True, "checks": {"all": True}, "pid": "123"})
    out = sr.recover("cp-canary:0.0", registry=REG,
                     run_fn=lambda a: (calls.append(a) or (0, "", "")), sleep=lambda _: None)
    assert out["recovered"] is True and out["reason"] == "verified"
    assert any("respawn-pane" in " ".join(c) for c in calls)
    assert any("claude --resume conv-1" in " ".join(c) for c in calls)


def _interrupted_mid_task(monkeypatch):
    """A session that died with work still open — the case recovery exists for.

    Recovery now requires an open ledger task, so a test that means "this session was
    interrupted" has to say so. Without it the refusal is `no_open_work`, which is the
    2026-08-07 fix working, not a broken test.
    """
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": True, "task_id": "t-1", "reason": "active_task"})

def test_failed_verification_is_not_reported_as_recovered(monkeypatch):
    _interrupted_mid_task(monkeypatch)
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "")
    monkeypatch.setattr(sr, "live_claude_for_cwd", lambda cwd, exclude_target="": [])
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda *a, **k: {"offered": False})
    monkeypatch.setattr(sr, "verify_recovered",
                        lambda t, c: {"ok": False, "checks": {"prompt_ready": False}})
    out = sr.recover("cp-canary:0.0", registry=REG, run_fn=lambda a: (0, "", ""),
                     sleep=lambda _: None)
    assert out["recovered"] is False and out["reason"] == "verify_failed"


def test_summary_choice_is_taken_and_full_replay_is_not(monkeypatch):
    sent = []
    text = ("This conversation is large.\n"
            "  1. Resume from summary (recommended)\n"
            "  2. Resume full session (slow)\n")
    out = sr.choose_summary_if_offered("t:0.0", capture_fn=lambda: text,
                                       send_fn=lambda keys: sent.append(keys))
    assert out["offered"] is True and out["chosen"] == "option_1"
    assert sent[0] == ["1"], sent


def test_no_choice_offered_sends_nothing():
    sent = []
    out = sr.choose_summary_if_offered("t:0.0", capture_fn=lambda: "❯ ",
                                       send_fn=lambda keys: sent.append(keys))
    assert out["offered"] is False and sent == []


def test_crash_loop_quarantines_and_raises_an_owner_blocker(monkeypatch):
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "")
    monkeypatch.setattr(sr, "live_claude_for_cwd", lambda cwd, exclude_target="": [])
    monkeypatch.setattr(sr, "recent_recoveries", lambda t, w, conn=None: 3)
    out = sr.recover("cp-canary:0.0", registry=REG)
    assert out["recovered"] is False
    assert out["reason"] == "quarantined_crash_loop" and out["owner_blocker"] is True
    assert sr.is_quarantined("cp-canary:0.0") is not None


def test_quarantined_target_is_not_retried(monkeypatch):
    conn = sr._db()
    conn.execute("INSERT OR REPLACE INTO session_quarantine VALUES (?,?,?)",
                 ("cp-canary:0.0", "now", "test"))
    conn.commit()
    conn.close()
    assert sr.recover("cp-canary:0.0", registry=REG)["reason"] == "quarantined"


def test_recovery_authorises_no_new_work(monkeypatch):
    _interrupted_mid_task(monkeypatch)
    monkeypatch.setattr(sr, "pane_state", lambda t: {"target": t, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda *a, **k: "❯ ")
    monkeypatch.setattr(sr, "live_claude_for_cwd", lambda cwd, exclude_target="": [])
    monkeypatch.setattr(sr, "choose_summary_if_offered", lambda *a, **k: {"offered": False})
    monkeypatch.setattr(sr, "verify_recovered", lambda t, c: {"ok": True, "checks": {}})
    out = sr.recover("cp-canary:0.0", registry=REG, run_fn=lambda a: (0, "", ""),
                     sleep=lambda _: None)
    assert "authorises no new work" in out["note"]


def test_autopilot_routes_a_dead_registered_session_to_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "load_registry", lambda *a, **k: REG)
    monkeypatch.setattr(sr, "recover",
                        lambda t, **k: {"recovered": True, "reason": "verified"})
    d = ap.evaluate("cp-canary:0.0", state="dead", tail="",
                    registry={"cp-canary:0.0": {"root": str(tmp_path), "next_step": "x",
                                                "live_actuation": True}})
    assert d["decision"] == "watchdog_dead_recovery"
    assert d["recovery"]["recovered"] is True


def test_autopilot_never_recovers_an_unregistered_dead_session(tmp_path):
    d = ap.evaluate("payment:0.0", state="dead", tail="",
                    registry={"payment:0.0": {"root": str(tmp_path), "next_step": "x"}})
    assert d["decision"] == "watchdog_dead"
    assert "NO duplicate" in d["note"]
