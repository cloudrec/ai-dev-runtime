"""Actuator-layer UNOBSERVABLE-PANE guard (M2 closeout, 2026-08-04).

M2 was first closed only in `cw.decide` and `ap.evaluate`. The actuator itself
stayed fail-OPEN: a direct `actuate()` call on a pane tmux could not read would
paste blind, because a failed `capture-pane` and a genuinely blank pane both
produced `tail == ""`. An attempt to refuse on "empty tail" alone turned 15
established clean-pane contracts into refusals, so the distinction had to become
a FACT rather than an inference:

    agent_control.pane_capture() -> (capture_ok, tail)
    Controller.snapshot()        -> {"capture_ok": bool, ...}
    actuate()                    -> refuses when capture_ok is False

The guard also refuses a snapshot carrying no observation at all (no capture flag,
no tail, no pending, no state) — the shape a stub or a broken controller produces.

Every test in sections 1–2 FAILS on pre-fix `8e2b1ee` (the actuator acted).
Section 3 pins that the clean-pane contracts that blocked the first attempt still
deliver, so the guard did not over-refuse.
"""
from __future__ import annotations

import pytest

from core import agent_control as ac
from core import agent_continuation_watchdog as cw
from core.control_plane import actuator as act
from core.control_plane import api as cp


SAFE_STEP = "continue with the next safe step"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    yield


def _no_sleep(_):
    pass


def _lease():
    return cp.acquire_lease("agent:cp-canary:0.0", "test", ttl_secs=60)


class SnapCtrl:
    """Controller whose snapshot is supplied verbatim — the exact contract the
    actuator consumes."""

    def __init__(self, snap):
        self.snap = dict(snap)
        self.sends = 0
        self.enters = 0

    def snapshot(self, target, cwd):
        return dict(self.snap)

    def _ok(self):
        self.snap.update(pending="", conv_mtime="m1", state="working",
                         tail=(self.snap.get("tail") or "") + " [ok]")
        return 0

    def enter(self, target):
        self.enters += 1
        return self._ok()

    def robust_submit(self, target, text):
        self.snap["pending"] = text
        return self._ok() == 0

    def send(self, target, text, idem):
        self.sends += 1
        self.snap["pending"] = text
        return {"submitted": self._ok() == 0}


def _actuate(ctrl, conv):
    return act.actuate(target="cp-canary:0.0", action_text=SAFE_STEP,
                       controller="test", conversation_id=conv, lease=_lease(),
                       ctrl=ctrl, sleep=_no_sleep)


# ═════════════ 1. capture failure is refused, with zero keystrokes ═══════════
def test_actuator_refuses_when_capture_failed():
    """Pre-fix: acted=True — a blind paste onto an unreadable pane."""
    ctrl = SnapCtrl({"capture_ok": False, "tail": "", "pending": "",
                     "conv_mtime": "m0", "state": "idle", "activity": ""})
    out = _actuate(ctrl, "cv-capfail")
    assert out["acted"] is False
    assert out["reason"] == "unobservable_pane" and out["why"] == "capture_failed"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_capture_failure_is_refused_even_when_the_state_looks_clean():
    """The dangerous shape: tmux failed, but a cached/derived state still says
    'idle', so every other guard reads the pane as safe to type on."""
    ctrl = SnapCtrl({"capture_ok": False, "tail": "", "pending": "",
                     "conv_mtime": "m0", "state": "idle",
                     "activity": "last seen: repo clean"})
    out = _actuate(ctrl, "cv-capfail-clean")
    assert out["reason"] == "unobservable_pane" and ctrl.sends == 0


def test_snapshot_with_no_observation_at_all_is_refused():
    ctrl = SnapCtrl({})
    out = _actuate(ctrl, "cv-empty-snap")
    assert out["acted"] is False
    assert out["reason"] == "unobservable_pane" and out["why"] == "empty_snapshot"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_blind_refusal_is_recorded_as_an_event():
    ctrl = SnapCtrl({"capture_ok": False, "tail": "", "state": "idle"})
    _actuate(ctrl, "cv-capfail-evt")
    from core.control_plane.cto import cto_brief_since
    types = [e.get("type") for e in (cto_brief_since("t-blind", limit=50).get("events") or [])]
    assert "action_deferred_unobservable_pane" in types


def test_a_hidden_dialog_behind_a_failed_capture_is_never_answered():
    """The pane really shows «Продолжить? (да/нет)», but capture-pane failed so the
    dialog gate sees "". The capture fact is what saves it."""
    ctrl = SnapCtrl({"capture_ok": False, "tail": "", "pending": "",
                     "state": "idle", "conv_mtime": "m0"})
    out = _actuate(ctrl, "cv-hidden-dialog")
    assert out["reason"] == "unobservable_pane"
    assert ctrl.sends == 0 and ctrl.enters == 0


# ═════════════ 2. the capture fact is produced, not inferred ═════════════════
def test_pane_capture_reports_failure_distinctly(monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: (1, "", "no such pane"))
    assert ac.pane_capture("gone:0.0") == (False, "")
    assert ac._pane_tail("gone:0.0") == ""          # legacy shape preserved


def test_pane_capture_reports_success_for_a_blank_pane(monkeypatch):
    """A readable but blank pane is NOT a capture failure — this is the exact
    distinction whose absence broke 15 contracts on the first attempt."""
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: (0, "", ""))
    assert ac.pane_capture("blank:0.0") == (True, "")


def test_controller_snapshot_carries_the_capture_flag(monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: (1, "", "fail"))
    monkeypatch.setattr(ac, "pending_input_text", lambda *a, **k: "")
    monkeypatch.setattr(ac, "conversation_evidence", lambda *a, **k: {})
    monkeypatch.setattr(ac, "agent_status", lambda *a, **k: {"state": "idle",
                                                            "recent_activity": ""})
    snap = cw.Controller().snapshot("cp-canary:0.0", "/tmp")
    assert snap["capture_ok"] is False


# ═════════════ 3. anti-overcorrection: clean panes still act ════════════════
def test_readable_clean_pane_still_delivers():
    ctrl = SnapCtrl({"capture_ok": True, "tail": "❯ ready\nrepo clean", "pending": "",
                     "conv_mtime": "m0", "state": "idle", "activity": "ready"})
    out = _actuate(ctrl, "cv-clean")
    assert out["acted"] is True and out.get("verified") is True


def test_legacy_snapshot_without_the_flag_still_delivers():
    """The 15 pre-existing contracts model a clean pane as tail="" with a state and
    no capture flag. They must keep working — the guard only fires on a REPORTED
    failure or on a snapshot with no observation whatsoever."""
    ctrl = SnapCtrl({"tail": "", "pending": "", "conv_mtime": "m0", "state": "idle",
                     "activity": ""})
    out = _actuate(ctrl, "cv-legacy")
    assert out.get("reason") != "unobservable_pane" and out["acted"] is True


def test_readable_blank_pane_is_not_treated_as_a_capture_failure():
    ctrl = SnapCtrl({"capture_ok": True, "tail": "", "pending": "", "conv_mtime": "m0",
                     "state": "idle", "activity": ""})
    out = _actuate(ctrl, "cv-blank-ok")
    assert out.get("reason") != "unobservable_pane" and out["acted"] is True


def test_waiting_owner_keeps_its_own_reason_when_capture_succeeded():
    ctrl = SnapCtrl({"capture_ok": True, "tail": "", "pending": "", "conv_mtime": "m0",
                     "state": "waiting_owner", "activity": ""})
    out = _actuate(ctrl, "cv-wo")
    assert out["acted"] is False and out["reason"] == "dialog_open"
