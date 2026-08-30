"""M2 — UNOBSERVABLE-PANE guard (2026-08-04 targeted review of f9c06ee).

The finding: `agent_control._pane_tail` returns "" when `tmux capture-pane` fails,
and every tail-based guard reads "" as "clear" —
`pane_shows_dialog("")` is False and `dialog_signature("")` is empty. If capture
failed while `send-keys` still worked, the watchdog and the autopilot would act on a
pane they cannot see: a Russian or English permission dialog, foreign queued text,
or live work would all be invisible, and the keystroke would answer or corrupt it.

Fix: an alive pane that captured NOTHING is UNOBSERVABLE ⇒ never a keystroke,
applied in `cw.decide` (before every path that reaches the keyboard) and in
`ap.evaluate` (never a poke candidate). Text on the input line comes from the same
styled capture as the tail, so `pending` proves the capture succeeded — only an
all-empty snapshot is the capture-failure signature. Section 5 pins that nothing
else moved, including that pin.

Sections 1–4 FAIL on f9c06ee (pre-fix): `decide` returned deliver and `evaluate`
returned poke, with zero refusal.
"""
from __future__ import annotations

import pytest

from core import agent_continuation_watchdog as cw
from core import commander_autopilot as ap
from core.control_plane import actuator as act
from core.control_plane import api as cp


NOW = 1_700_000_000.0

# What the pane was REALLY showing while capture-pane failed — never seen by the
# watchdog, which is the whole point of the finding.
RU_DIALOG_HIDDEN = "Точно удалить все данные?\nПродолжить? (да/нет)"
EN_DIALOG_HIDDEN = "Do you want to proceed?\n❯ 1. Yes\n  2. No"

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


def _confirmed_prev():
    return {"idle_since_ts": NOW - cw.IDLE_CONFIRM_SECS - 5, "last_state": "idle"}


def _agent(tail="", target="proj:0.0"):
    return {"target": target, "session": "proj", "alive": True, "is_agent": True,
            "state": "idle", "_tail": tail, "claude_cwd": "/opt/proj"}


def _decide(agent, pending, proactive=False):
    return cw.decide(agent=agent, cfg={}, pending=pending, state="idle",
                     prev_target=_confirmed_prev(), now_ts=NOW, eligible=True,
                     continuation=SAFE_STEP, proactive=proactive, conv_count=0)


# ═════════════ 1. decide: a blind pane never receives a keystroke ═════════════
def test_decide_refuses_proactive_delivery_on_a_blind_pane():
    """Pre-fix: action == deliver — a blind paste onto an unreadable pane."""
    d = _decide(_agent(tail=""), "", proactive=True)
    assert d["action"] == "skip" and d["reason"] == "unobservable_pane"


@pytest.mark.parametrize("whitespace", ["", "   ", "\n\n", " \t \n "])
def test_decide_treats_whitespace_only_tail_as_unobservable(whitespace):
    """A capture that yields only blank lines is just as blind as an empty one."""
    d = _decide(_agent(tail=whitespace), "", proactive=True)
    assert d["action"] == "skip" and d["reason"] == "unobservable_pane"


# ═════════════ 2. the hidden-dialog scenario, RU and EN ══════════════════════
def test_blind_pane_hides_a_russian_dialog_nothing_is_pasted():
    """The pane really shows «Продолжить? (да/нет)»; capture-pane failed, so the
    dialog is invisible. Pre-fix the continuation was pasted+Entered — and that
    Enter answers the RU dialog. The dialog gate cannot help here: it sees ""."""
    assert cw.pane_shows_dialog("") is False           # the gap the guard closes
    assert cw.pane_shows_dialog(RU_DIALOG_HIDDEN) is True
    d = _decide(_agent(tail=""), "", proactive=True)
    assert d["action"] == "skip" and d["reason"] == "unobservable_pane"


def test_blind_pane_hides_an_english_numbered_dialog_no_paste_is_made():
    assert cw.pane_shows_dialog("") is False
    assert cw.pane_shows_dialog(EN_DIALOG_HIDDEN) is True
    d = _decide(_agent(tail=""), "", proactive=True)
    assert d["action"] == "skip" and d["reason"] == "unobservable_pane"


# ═════════════ 3. run_once: the production path stays hands-off ══════════════
class BlindPaneCtrl:
    """Production-shaped controller whose capture-pane fails: snapshot returns an
    empty tail (exactly what `_pane_tail` yields on rc != 0) while send-keys works."""

    def __init__(self, pending=""):
        self._pending = pending
        self.enters = 0
        self.sends = 0
        self.emitted = []

    def inventory(self):
        return {"agents": [{"target": "proj:0.0", "session": "proj", "alive": True,
                            "is_agent": True, "state": "idle", "claude_cwd": "/opt/proj"}]}

    def load_config(self):
        return {"sessions": {"proj": {"mode": "auto", "proactive_continue": True,
                                      "safe_continuation": SAFE_STEP}}}

    def snapshot(self, target, cwd):
        return {"tail": "", "pending": self._pending, "conv_mtime": "m0",
                "state": "idle", "activity": ""}

    def enter(self, target):
        self.enters += 1
        return 0

    def robust_submit(self, target, text):
        self.enters += 1
        return True

    def send(self, target, text, idem):
        self.sends += 1
        return {"submitted": True}

    def emit(self, target, project, et, payload, dedup_key):
        self.emitted.append(et)
        return True


def test_run_once_never_types_on_an_unreadable_pane():
    """Production contract: the inventory carries no _tail, run_once backfills it
    from the snapshot, and the snapshot is empty because capture-pane failed.
    Pre-fix the continuation was delivered onto that unreadable pane."""
    ctrl = BlindPaneCtrl("")
    cw.run_once(ctrl, now_ts=NOW, sleep=_no_sleep)                      # seed dwell
    cw.run_once(ctrl, now_ts=NOW + cw.IDLE_CONFIRM_SECS + 5, sleep=_no_sleep)
    assert ctrl.enters == 0 and ctrl.sends == 0


# ═════════════ 4. autopilot: never a poke candidate ══════════════════════════
class ActFakeCtrl:
    def __init__(self, tail="", state="idle"):
        self.s = {"pending": "", "conv": "m0", "state": state, "tail": tail}
        self.sends = 0
        self.enters = 0

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"],
                "conv_mtime": self.s["conv"], "state": self.s["state"],
                "activity": self.s["tail"]}

    def _ok(self):
        self.s.update(pending="", conv="m1", state="working",
                      tail=(self.s["tail"] or "") + " [ok]")
        return 0

    def enter(self, target):
        self.enters += 1
        return self._ok()

    def robust_submit(self, target, text):
        self.s["pending"] = text
        return self._ok() == 0

    def send(self, target, text, idem):
        self.sends += 1
        self.s["pending"] = text
        return {"submitted": self._ok() == 0}


def _lease():
    return cp.acquire_lease("agent:cp-canary:0.0", "test", ttl_secs=60)


@pytest.mark.parametrize("tail", ["", "   ", "\n"])
def test_autopilot_never_pokes_an_unobservable_pane(tail):
    """Pre-fix: `poke` — the autopilot proposed a keystroke for a pane whose
    active-marker, background-subagent and dialog guards had all read ""."""
    reg = {"cp-canary:0.0": {"root": "/tmp", "next_step": SAFE_STEP,
                             "autonomous_safe": True}}
    d = ap.evaluate("cp-canary:0.0", state="idle", tail=tail, registry=reg)
    assert d["decision"] == "skip_unobservable_pane", (tail, d)


def test_autopilot_still_evaluates_a_readable_idle_pane():
    """Anti-overcorrection: a readable pane with unfinished work is still a poke
    candidate — the guard must not swallow the normal path."""
    reg = {"cp-canary:0.0": {"root": "/tmp", "next_step": SAFE_STEP,
                             "autonomous_safe": True}}
    d = ap.evaluate("cp-canary:0.0", state="idle",
                    tail="❯ ready\n3 tasks (1 done, 0 in progress, 2 open)",
                    registry=reg)
    assert d["decision"] != "skip_unobservable_pane"


# ═════════════ 5. anti-overcorrection: nothing else changed ══════════════════
def test_visible_pane_still_submits_the_safe_pending_text():
    d = _decide(_agent(tail="❯ ready\nrepo clean"), SAFE_STEP)
    assert d["action"] == "submit" and d["step_text"] == SAFE_STEP


def test_visible_pane_still_delivers_proactively():
    d = _decide(_agent(tail="❯ ready\nrepo clean"), "", proactive=True)
    assert d["action"] == "deliver" and d["step_text"] == SAFE_STEP


def test_unsafe_pending_is_still_a_blocker_not_a_silent_skip():
    """The guard must not swallow the unsafe-text signal: «удали старый
    scratchpad» still raises a blocker."""
    d = _decide(_agent(tail=""), "удали старый scratchpad")
    assert d["action"] == "blocker" and d["reason"] == "unsafe_pending_text"


def test_pending_text_proves_the_pane_was_readable_and_still_submits():
    """SCOPE PIN for the blindness signal: `pending` is read from the same styled
    capture as the tail, so text on the input line means the capture SUCCEEDED —
    an empty tail there is a fake/edge shape, not a capture failure. Treating it
    as blind would have broken the missed-Enter recovery this system exists for
    (it regressed 23 existing contracts before the signal was narrowed)."""
    d = _decide(_agent(tail=""), SAFE_STEP)
    assert d["action"] == "submit" and d["step_text"] == SAFE_STEP




def test_waiting_owner_with_empty_tail_still_reports_dialog_open():
    """Ordering pin: an explicit waiting_owner snapshot is a KNOWN dialog, not an
    unreadable pane — it must keep its own reason for the ledger."""
    ctrl = ActFakeCtrl(tail="", state="waiting_owner")
    out = act.actuate(target="cp-canary:0.0", action_text=SAFE_STEP,
                      controller="test", conversation_id="cv-wo-order",
                      lease=_lease(), ctrl=ctrl, sleep=_no_sleep)
    assert out["acted"] is False and out["reason"] == "dialog_open"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_actuator_still_delivers_to_a_clean_visible_canary():
    ctrl = ActFakeCtrl(tail="❯ ready\nrepo clean")
    out = act.actuate(target="cp-canary:0.0", action_text=SAFE_STEP,
                      controller="test", conversation_id="cv-visible",
                      lease=_lease(), ctrl=ctrl, sleep=_no_sleep)
    assert out["acted"] is True and out.get("verified") is True
