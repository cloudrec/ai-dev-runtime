"""Canonical multi-signal state estimation — false-idle correction.

Defect: agent_status reported idle while the pane showed `Pouncing… (8s · thinking)`. An
actively thinking/executing agent must never be classified idle from prompt/layout alone.
Tests: whimsical spinners (Pouncing/Noodling/Beboppin/Hyperspacing/Osmosing), prompt-visible
-while-thinking, conversation-movement, and the actuator false-idle guard.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import state_estimator as se, actuator as act
from core import agent_control as ac


# ── strengthened active-marker regex catches whimsical spinners ──────────────
@pytest.mark.parametrize("word", ["Pouncing", "Noodling", "Beboppin", "Hyperspacing",
                                   "Osmosing", "Shimmying"])
def test_whimsical_spinner_is_active(word):
    assert se.has_active_marker(f"✶ {word}… (8s · thinking)") is True
    assert se.has_active_marker(f"· {word}… (12s · ↑ 2.3k tokens)") is True


def test_esc_to_interrupt_and_thinking_are_active():
    assert se.has_active_marker("… esc to interrupt") is True
    assert se.has_active_marker("(15s · thinking)") is True


def test_plain_idle_prompt_has_no_active_marker():
    assert se.has_active_marker("❯ \n  auto mode on") is False


# ── estimator precedence ─────────────────────────────────────────────────────
def test_active_marker_overrides_idle_base():
    # base classifier said idle, but the tail is a live spinner → working
    out = se.estimate(base_state="idle", tail="✶ Pouncing… (8s · thinking)")
    assert out["state"] == "working" and out["active"] is True


def test_conversation_movement_overrides_idle():
    out = se.estimate(base_state="idle", tail="❯ ", conv_moved=True)
    assert out["state"] == "working"


def test_prompt_visible_while_thinking_is_unknown_not_idle():
    # no active marker in THIS snapshot, but idle not sustained → unknown, never idle
    out = se.estimate(base_state="idle", tail="❯ ", conv_moved=False, idle_confirmed=False)
    assert out["state"] == "unknown" and out["reason"] == "idle_unconfirmed_or_conflicting"


def test_quiet_sustained_idle_is_idle():
    out = se.estimate(base_state="idle", tail="❯ ", conv_moved=False, idle_confirmed=True)
    assert out["state"] == "idle"


def test_pending_input_is_waiting():
    out = se.estimate(base_state="idle", tail="❯ deploy now", pending="deploy now")
    assert out["state"] == "waiting_input"


def test_cpu_activity_overrides_idle():
    assert se.estimate(base_state="idle", tail="❯ ", cpu_active=True)["state"] == "working"


# ── actuator false-idle guard: never command a working agent ─────────────────
class WorkingCtrl:
    def __init__(self, tail, state="idle"):
        self.t = tail
        self.st = state
        self.sends = 0

    def snapshot(self, target, cwd):
        return {"tail": self.t, "pending": "", "conv_mtime": "m0", "state": self.st,
                "activity": self.t}

    def send(self, target, text, idem):
        self.sends += 1
        return {"submitted": True}

    def enter(self, target):
        return 0

    def robust_submit(self, target, text):
        return True


def test_actuator_suppresses_continuation_to_working_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(act, "ENABLED", True)
    lease = cp.acquire_lease("agent:arb:0.0", "ctrl", now=1000)
    ctrl = WorkingCtrl(tail="✶ Pouncing… (8s · thinking)")     # actively thinking
    r = act.actuate(target="arb:0.0", action_text="continue with the next safe step",
                    controller="ctrl", conversation_id="cv", lease=lease, cwd="/opt/x",
                    ctrl=ctrl, sleep=lambda _: None)
    assert r["acted"] is False and r["reason"] == "target_working"
    assert r.get("false_idle_corrected") is True
    assert ctrl.sends == 0                                      # never delivered
    from core.control_plane import cto
    assert any(e["type"] == "false_idle_corrected" for e in cto.cto_brief_since("t")["events"])
