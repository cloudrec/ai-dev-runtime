"""Queued-but-unsubmitted input is a DELIVERY FAILURE (2026-08-04 MESS incident).

What happened live: the autopilot delivered
`continue the next safe internal qa audit step; run the tests; commit locally` to
mess-qa-automation:0.0. The text was TYPED INTO THE INPUT LINE and never submitted — the
agent sat at `waiting_input` doing nothing — yet `verify()` returned ok=True and the run
was recorded as `verified`, and the acceptance report claimed detect→resume→verify PASS.

Two holes, both fixed here:
  1. `ok` accepted `state_transitioned` alone as the final clause. Merely TYPING changes
     the pane's `activity`, so that flag flips for text that was never executed. Real
     progress evidence is now required: a transcript write, a live active-execution
     marker, or a working/shell_running state.
  2. `prompt_consumed` trusted the snapshot's `pending`, which read EMPTY while the text
     was plainly queued in the rendered input box. The input box is now inspected too.

Delivery must be judged by EXECUTION, never by text appearing on screen.
"""
from __future__ import annotations

import pytest

from core import agent_continuation_watchdog as cw


STEP = "continue the next safe internal qa audit step; run the tests; commit locally"

# The real MESS pane shape: the step sitting in the input box, nothing running.
QUEUED_TAIL = (
    "● Prior note appended.\n"
    "\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    f"❯ {STEP}\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    "  [CAVEMAN]\n"
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 3 agents\n"
)
# The same pane after the step actually ran.
EXECUTED_TAIL = (
    "● Running 1 shell command…\n"
    "\n"
    "✽ Wibbling… (57s · ↓ 1.7k tokens)\n"
    "\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    "  [CAVEMAN]\n"
)
BEFORE = {"tail": "● Prior note appended.\n", "pending": "", "conv_mtime": "m0",
          "state": "idle", "activity": "idle"}


def _after(tail, *, pending="", conv="m0", state="idle"):
    return {"tail": tail, "pending": pending, "conv_mtime": conv, "state": state,
            "activity": tail}


# ═════════ 1. the exact live failure is now a failure ════════════════════════
def test_queued_text_with_empty_pending_is_not_a_successful_delivery():
    """THE incident: pending reads empty, the text is visibly queued, nothing ran.
    Pre-fix: ok=True (state_transitioned carried it). Now: ok=False."""
    v = cw.verify(BEFORE, _after(QUEUED_TAIL), expected_pending=STEP, enter_rc=0)
    assert v["queued_input"] is True
    assert v["prompt_consumed"] is False
    assert v["progressed"] is False
    assert v["ok"] is False, v


def test_queued_text_reported_in_pending_is_also_a_failure():
    v = cw.verify(BEFORE, _after(QUEUED_TAIL, pending=STEP), expected_pending=STEP,
                  enter_rc=0)
    assert v["queued_input"] is True and v["ok"] is False


def test_pane_activity_change_alone_is_never_acceptance():
    """A pane/activity delta is exactly what typing produces — it must not be proof.
    Pre-fix this returned ok=True."""
    v = cw.verify(BEFORE, _after("some new output but nothing running\n"),
                  expected_pending=STEP, enter_rc=0)
    assert v["state_transitioned"] is True      # still reported for diagnostics
    assert v["progressed"] is False and v["ok"] is False, v


# ═════════ 2. real execution evidence is accepted ════════════════════════════
def test_execution_with_active_marker_is_accepted():
    v = cw.verify(BEFORE, _after(EXECUTED_TAIL, state="working"),
                  expected_pending=STEP, enter_rc=0)
    assert v["queued_input"] is False and v["progressed"] is True and v["ok"] is True


def test_transcript_write_is_accepted_even_when_the_pane_returns_to_rest():
    """A fast step can finish before the poll — a transcript write still proves it ran."""
    v = cw.verify(BEFORE, _after("● Done.\n─────\n❯ \n─────\n", conv="m1"),
                  expected_pending=STEP, enter_rc=0)
    assert v["conversation_modified"] is True and v["ok"] is True


def test_unsubmitted_enter_is_never_ok_even_with_progress_markers():
    v = cw.verify(BEFORE, _after(QUEUED_TAIL, conv="m1", state="working"),
                  expected_pending=STEP, enter_rc=0)
    assert v["queued_input"] is True and v["ok"] is False, "queued text is disqualifying"


def test_failed_enter_is_never_ok():
    v = cw.verify(BEFORE, _after(EXECUTED_TAIL, state="working"),
                  expected_pending=STEP, enter_rc=1)
    assert v["ok"] is False


# ═════════ 3. input-box extraction does not invent queued text ═══════════════
def test_input_region_is_empty_when_no_box_is_rendered():
    """Guessing from the last output lines would report ordinary output as queued
    input and fail every real delivery."""
    assert cw.input_region("just some output\nmore output\nand more\n") == ""
    assert cw.text_is_queued({"tail": f"ran: {STEP}\noutput line\n", "pending": ""},
                             STEP) is False


def test_input_region_reads_the_box_between_the_last_two_rules():
    assert STEP in cw.input_region(QUEUED_TAIL)
    assert cw.input_region(EXECUTED_TAIL).strip() == ""


# ═════════ 4. the retry submits into the SAME session, without duplicating ═══
class QueuedCtrl:
    """Pane where the paste lands but the Enter does not — the live failure. A bare
    Enter (submit) then executes it. Counts every keystroke so a duplicate paste shows."""

    def __init__(self):
        self.pastes = 0
        self.enters = 0
        self.executed = False
        self.s = {"tail": "● idle\n", "pending": "", "conv_mtime": "m0",
                  "state": "idle", "activity": "a0"}

    def snapshot(self, target, cwd):
        return dict(self.s)

    def send(self, target, text, idem):
        self.pastes += 1
        self.s.update(tail=QUEUED_TAIL, activity="a1")      # typed, NOT submitted
        return {"submitted": True}

    def enter(self, target):
        self.enters += 1
        self.executed = True
        self.s.update(tail=EXECUTED_TAIL, state="working", conv_mtime="m1", activity="a2")
        return 0

    def robust_submit(self, target, text):
        self.pastes += 1
        self.s.update(tail=EXECUTED_TAIL, state="working", conv_mtime="m1")
        return True


def test_delivery_retries_with_a_bare_submit_and_never_repastes():
    ctrl = QueuedCtrl()
    out = cw.deliver_and_verify(ctrl, target="mess-qa-automation:0.0", cwd="/opt/x",
                                action="deliver", step_text=STEP, expected_pending=STEP,
                                sleep=lambda _: None)
    assert out["verify"]["ok"] is True, out
    assert out["retried"] is True
    assert ctrl.enters == 1, "the retry must be a bare submit"
    assert ctrl.pastes == 1, "the step must never be pasted twice"
    assert ctrl.executed is True


class NeverSubmitsCtrl(QueuedCtrl):
    """Nothing consumes the line — delivery must be reported as FAILED, not verified."""

    def enter(self, target):
        self.enters += 1
        return 0                                    # Enter 'succeeds' but nothing runs

    def robust_submit(self, target, text):
        self.pastes += 1
        return True                                 # still queued


def test_delivery_that_never_executes_is_reported_failed():
    ctrl = NeverSubmitsCtrl()
    out = cw.deliver_and_verify(ctrl, target="mess-qa-automation:0.0", cwd="/opt/x",
                                action="deliver", step_text=STEP, expected_pending=STEP,
                                sleep=lambda _: None)
    assert out["verify"]["ok"] is False, "queued forever must never read as delivered"
    assert out["verify"]["queued_input"] is True
