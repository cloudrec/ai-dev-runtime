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
        # a real pane CLEARS the input line when the text is submitted
        self.s.update(tail=EXECUTED_TAIL, state="working", conv_mtime="m1", activity="a2",
                      pending="")
        return True and 0

    def robust_submit(self, target, text):
        self.pastes += 1
        self.s.update(tail=EXECUTED_TAIL, state="working", conv_mtime="m1", pending="")
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


# ═════════ 5. the dwell gate must let a queued line be recovered ═════════════
class WaitingInputCtrl:
    """Production shape of the incident: the pane is at `waiting_input` with our exact
    safe step queued. Nothing else is running. The watchdog must eventually SUBMIT it."""

    def __init__(self):
        self.enters = 0
        self.pastes = 0
        self.state = "waiting_input"

    def inventory(self):
        return {"agents": [{"target": "cp-canary:0.0", "session": "cp-canary",
                            "alive": True, "is_agent": True, "state": self.state,
                            "claude_cwd": "/root/cp-canary-v2"}]}

    def load_config(self):
        return {"sessions": {"cp-canary": {"mode": "auto"}}}

    def snapshot(self, target, cwd):
        if self.state == "waiting_input":
            return {"tail": QUEUED_TAIL, "pending": STEP, "conv_mtime": "m0",
                    "state": "waiting_input", "activity": "a0", "capture_ok": True}
        return {"tail": EXECUTED_TAIL, "pending": "", "conv_mtime": "m1",
                "state": "working", "activity": "a1", "capture_ok": True}

    def enter(self, target):
        self.enters += 1
        self.state = "working"
        return 0

    def robust_submit(self, target, text):
        self.pastes += 1
        self.state = "working"
        return True

    def send(self, target, text, idem):
        self.pastes += 1
        return {"submitted": True}

    def emit(self, target, project, et, payload, dedup_key):
        return True


def test_waiting_input_accumulates_dwell_so_a_queued_line_can_be_recovered(monkeypatch,
                                                                          tmp_path):
    """Pre-fix: `idle_since_ts` was only set for state == 'idle', so a pane parked at
    `waiting_input` never cleared the dwell gate and decide() returned
    `idle_not_confirmed` forever — the queued line was NEVER submitted."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("CONTINUATION_WATCHDOG_SESSIONS", "cp-canary")
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "CONTINUATION_VIA_ACTUATOR", False, raising=False)
    ctrl = WaitingInputCtrl()
    now = 1_700_000_000.0
    cw.run_once(ctrl, now_ts=now, sleep=lambda _: None)                  # seed dwell
    out = cw.run_once(ctrl, now_ts=now + cw.IDLE_CONFIRM_SECS + 5, sleep=lambda _: None)
    assert ctrl.enters + ctrl.pastes >= 1, ("the queued line must be submitted", out)
    assert ctrl.state == "working"


def test_dwell_still_resets_when_the_agent_is_actually_working(monkeypatch, tmp_path):
    """Anti-overcorrection: a working pane must not accumulate dwell."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("CONTINUATION_WATCHDOG_SESSIONS", "cp-canary")
    monkeypatch.setattr(cw, "ENABLED", True)
    ctrl = WaitingInputCtrl()
    ctrl.state = "working"
    now = 1_700_000_000.0
    cw.run_once(ctrl, now_ts=now, sleep=lambda _: None)
    cw.run_once(ctrl, now_ts=now + cw.IDLE_CONFIRM_SECS + 5, sleep=lambda _: None)
    assert ctrl.enters == 0 and ctrl.pastes == 0


# ═════════ 6. a completed spinner in scrollback must not pin "thinking" ══════
COMPLETED_SPINNER_TAIL = (
    "● Note appended.\n"
    "\n"
    "✻ Sautéed for 6s\n"                     # finished — NOT live execution
    "\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    f"❯ {STEP}\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    "  [CAVEMAN]\n"
)
LIVE_SPINNER_TAIL = (
    "● Working.\n"
    "\n"
    "✻ Wibbling… (57s · ↓ 1.7k tokens · esc to interrupt)\n"
    "\n"
    "────────────────────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────────────────────────\n"
)


def _decide(tail, pending, state="waiting_input"):
    agent = {"target": "cp-canary:0.0", "session": "cp-canary", "alive": True,
             "is_agent": True, "state": state, "_tail": tail,
             "claude_cwd": "/root/cp-canary-v2"}
    return cw.decide(agent=agent, cfg={}, pending=pending, state=state,
                     prev_target={"idle_since_ts": 1.0, "last_state": "waiting_input"},
                     now_ts=1.0 + cw.IDLE_CONFIRM_SECS + 5, eligible=True,
                     continuation=STEP, proactive=False, conv_count=0)


def test_completed_spinner_in_scrollback_does_not_block_recovery():
    """Pre-fix the watchdog used its own regex over the WHOLE tail, and that regex matches
    a BARE spinner glyph — so a finished '✻ Sautéed for 6s' line pinned the agent at
    'thinking' forever and the queued step was never submitted (observed live on
    cp-canary). Now the shared detector runs over the live status region only."""
    d = _decide(COMPLETED_SPINNER_TAIL, "continue with the next safe canary note")
    assert d["action"] == "submit", d


def test_live_spinner_still_blocks_as_thinking():
    """Anti-overcorrection: genuine in-flight execution must still be left alone."""
    d = _decide(LIVE_SPINNER_TAIL, "continue with the next safe canary note")
    assert d["action"] == "skip" and d["reason"] == "thinking", d


def test_watchdog_and_classifier_agree_on_what_is_running():
    """The divergence between three 'is it running' definitions was the defect itself."""
    from core import agent_control as ac
    for tail, expected in ((COMPLETED_SPINNER_TAIL, False), (LIVE_SPINNER_TAIL, True)):
        assert cw._live_active_marker(tail) is expected
        assert bool(ac._STATE_ACTIVE_RUN_RE.search(ac.live_status_region(tail))) is expected


# ═════════ 7. a stale "verified" record must not block a queued line ═════════
def test_queued_line_is_submitted_even_when_recorded_verified(monkeypatch, tmp_path):
    """2026-08-04 live: the old verifier recorded a never-executed step as `verified`;
    that stale record then made the actuator answer `already_verified` and refuse to
    recover the very line it mis-recorded. A line still sitting in the input box is proof
    the record is wrong — submit it (one Enter on existing text cannot duplicate)."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    from core.control_plane import actuator as act, api as cp
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)

    ctrl = QueuedCtrl()
    ctrl.s.update(tail=QUEUED_TAIL, pending=STEP, state="waiting_input")
    conv = "cv-stale-verified"
    lease = cp.acquire_lease("agent:cp-canary:0.0", "t", ttl_secs=60)
    # first attempt records the action; force the stale 'verified' shape
    act.actuate(target="cp-canary:0.0", action_text=STEP, controller="t",
                conversation_id=conv, lease=lease, ctrl=ctrl, sleep=lambda _: None)
    ah = act._action_hash(STEP) if hasattr(act, "_action_hash") else None
    from core.control_plane import store
    conn = store.connect()
    try:
        conn.execute("UPDATE cp_action SET verified=1, blocked=0, outcome='verified'")
        conn.commit()
    finally:
        conn.close()

    ctrl2 = QueuedCtrl()
    ctrl2.s.update(tail=QUEUED_TAIL, pending=STEP, state="waiting_input")
    lease2 = cp.acquire_lease("agent:cp-canary:0.0", "t", ttl_secs=60)
    out = act.actuate(target="cp-canary:0.0", action_text=STEP, controller="t",
                      conversation_id=conv, lease=lease2, ctrl=ctrl2, sleep=lambda _: None)
    assert out.get("reason") != "already_verified", out
    assert ctrl2.enters >= 1, "the queued line must be submitted"
    assert ctrl2.pastes == 0, "submitting an existing line must never paste a copy"


def test_already_verified_still_short_circuits_when_nothing_is_queued(monkeypatch, tmp_path):
    """Anti-overcorrection: with a clean input line the dedupe must still hold."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    from core.control_plane import actuator as act, api as cp
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    ctrl = QueuedCtrl()
    conv = "cv-clean-dedupe"
    lease = cp.acquire_lease("agent:cp-canary:0.0", "t", ttl_secs=60)
    act.actuate(target="cp-canary:0.0", action_text=STEP, controller="t",
                conversation_id=conv, lease=lease, ctrl=ctrl, sleep=lambda _: None)
    from core.control_plane import store
    conn = store.connect()
    try:
        conn.execute("UPDATE cp_action SET verified=1, blocked=0, outcome='verified'")
        conn.commit()
    finally:
        conn.close()
    clean = QueuedCtrl()
    clean.s.update(tail=EXECUTED_TAIL, pending="", state="idle")
    lease2 = cp.acquire_lease("agent:cp-canary:0.0", "t", ttl_secs=60)
    out = act.actuate(target="cp-canary:0.0", action_text=STEP, controller="t",
                      conversation_id=conv, lease=lease2, ctrl=clean, sleep=lambda _: None)
    assert out["reason"] == "already_verified" and clean.enters == 0 and clean.pastes == 0
