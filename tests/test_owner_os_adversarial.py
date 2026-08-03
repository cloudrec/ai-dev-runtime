"""Adversarial regression tests for the 2026-08-03 Fable audit.

Each test here FAILED on the pre-audit code (HEAD 60f9967) and pins a real
production behaviour, not a fabricated fixture contract:

  * The commander-autopilot acceptance tests fed `tick()` an inventory carrying
    `_tail` / `claude_conversation` keys that the REAL `agent_control.agent_list()`
    NEVER returns — so background-subagent detection, task-footer parsing and
    per-conversation dedupe were all vacuously "proven" while being dead code in
    production (every agent evaluated with tail="" and conversation_id="").
  * `classify_state` returned `idle` for the REAL mess-qa-automation pane that
    showed "✻ Waiting for 1 background agent to finish" — a running Fable
    subagent — making the agent a poke candidate mid-work.
  * The stuck-shell watchdog was unreachable in production: `tick()` never
    passed `conv_age_fn`, so `conv_age_secs` was always None.
  * The Actuator trusted a caller-supplied `policy_class`, letting any internal
    caller bypass the deny-by-default classifier with policy_class="autonomous_safe".
"""
from __future__ import annotations

import pytest

from core import agent_control as ac
from core import commander_autopilot as ap
from core import agent_continuation_watchdog as cw
from core.control_plane import actuator as act
from core.control_plane import api as cp


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    yield


def _no_sleep(_):
    pass


class FakeCtrl:
    def __init__(self):
        self.s = {"pending": "", "conv": "m0", "state": "idle", "tail": ""}
        self.sends = 0

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"],
                "conv_mtime": self.s["conv"], "state": self.s["state"],
                "activity": self.s["tail"]}

    def _ok(self):
        self.s.update(pending="", conv="m1", state="working",
                      tail=self.s["tail"] + " [ok]")
        return 0

    def enter(self, target):
        return self._ok()

    def robust_submit(self, target, text):
        self.s["pending"] = text
        return self._ok() == 0

    def send(self, target, text, idem):
        self.sends += 1
        self.s["pending"] = text
        return {"submitted": self._ok() == 0}


# The EXACT live pane content observed on mess-qa-automation:0.0 (2026-08-03,
# read-only capture) while a background Fable subagent was running.
MESS_REAL_TAIL = """\
  4. Then safe to rotate/clear and resume from checkpoint — no duplicate work.

  Waiting on Phase A.

✻ Waiting for 1 background agent to finish

  8 tasks (3 done, 1 in progress, 4 open)
  ◼ Fable 5: audit ALL code + live config, prove findings, implement…
  ◻ Fable 5: bump version, build+sign staged APK, verify cert/sha/ve…

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
"""

REG = {
    "cp-canary:0.0": {"root": "/root/cp-canary-v2", "live_actuation": True,
                      "next_step": "continue with the next safe canary note."},
}
FOOTER = "  3 tasks (0 done, 1 in progress, 2 open)"


# ── 1. classify_state: a running background subagent must count as WORKING ───
def test_real_mess_pane_with_running_subagent_is_working():
    """Pre-fix: classify_state -> 'idle' for the real mess pane, so the
    autopilot acceptance report listed it as a poke candidate while its Fable
    subagent was mid-audit."""
    assert ac.classify_state(True, True, MESS_REAL_TAIL) == "working"


def test_live_minute_spinner_without_token_counter_is_working():
    """Pre-fix: the live minute-form spinner '(19m 23s ·' (real payment pane)
    matched NO active pattern — classification depended entirely on the token
    counter being in the same captured tail."""
    tail = "✶ Transmuting… (19m 23s · doing things)\n\n❯ \n"
    assert ac.classify_state(True, True, tail) == "working"


def test_past_tense_spinner_still_idle():
    # guard: the new patterns must not turn a finished pane into false-working
    tail = "✻ Baked for 4s\n\n❯ \n"
    assert ac.classify_state(True, True, tail) == "idle"


# The EXACT live cp-canary pane (2026-08-03 19:5x): the "· 1 shell still running"
# spinner line is STALE SCROLLBACK — the very next block says that background command
# COMPLETED, and the last spinner line is past-tense. The pane sat like this for 40+
# minutes classified "working", permanently blocking watchdog/autopilot/rotation.
CANARY_STALE_TAIL = """\
● Command running background (foreground sleep blocked by harness). Waiting for
  it to finish.

✻ Baked for 8s · 1 shell still running

● Background command "Sleep 10s then echo probe string" completed (exit code 0)

  Read 1 file

● Output:

  canary-shell-activity-probe

  Exit 0. Waiting.

✻ Baked for 4s

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · ← 3 agents
"""


def test_stale_shell_marker_in_scrollback_is_not_working():
    """Pre-fix: the stale '· 1 shell still running' deep in scrollback matched the
    active-run regex over the whole 1500-char tail → permanent false-working."""
    assert ac.classify_state(True, True, CANARY_STALE_TAIL) == "idle"


def test_live_status_region_is_last_spinner_line_to_end():
    region = ac.live_status_region(CANARY_STALE_TAIL)
    assert "1 shell still running" not in region
    assert "Baked for 4s" in region
    # no spinner line at all → conservative fallback to the whole tail
    assert ac.live_status_region("plain text\nno spinner") == "plain text\nno spinner"


def test_live_shell_marker_on_last_spinner_line_is_still_working():
    tail = "● doing things\n\n✻ Baked for 8s · 1 shell still running\n\n❯ \n"
    assert ac.classify_state(True, True, tail) == "working"


# ── 2. autopilot tick must work on the REAL agent_list contract ──────────────
def _real_contract_agent(target, state, cwd="/opt/mess"):
    """An inventory entry with exactly the keys agent_list() really returns —
    no `_tail`, no `claude_conversation`."""
    return {"session": target.split(":")[0], "window": "0", "pane": "0",
            "target": target, "pane_id": "%1", "pid": 1, "cwd": cwd,
            "command": "claude", "window_name": "w", "alive": True,
            "claude": {"pid": 2}, "is_agent": True, "claude_pid": 2,
            "claude_cwd": cwd, "state": state}


def test_tick_fetches_real_tail_and_sees_background_subagent():
    """Pre-fix: tick() read a.get('_tail') which is never present in the real
    inventory, so the subagent-running pane evaluated with tail='' and was
    POKED. Post-fix tick fetches the pane tail itself."""
    reg = {"mess-qa-automation:0.0": {"root": "/opt/mess", "live_actuation": False,
                                      "next_step": "continue the next safe QA step."}}
    inv = {"agents": [_real_contract_agent("mess-qa-automation:0.0", "idle")]}
    res = ap.tick(inventory=inv, registry=reg, ctrl=FakeCtrl(), evaluate_only=True,
                  tail_fn=lambda t: MESS_REAL_TAIL, conv_fn=lambda cwd: ("cv-m", None))
    r = res["results"][0]
    assert r["decision"] == "skip_progressing", r
    assert r["background_subagent"] is True


def test_tick_uses_real_conversation_id_for_dedupe():
    """Pre-fix: tick() read a.get('claude_conversation') (never present), so the
    Actuator idempotency key collapsed to conversation_id='' — a NEW conversation
    (post-/clear) could never be poked with the same step again."""
    inv = {"agents": [_real_contract_agent("cp-canary:0.0", "idle", "/root/cp-canary-v2")]}
    r1 = ap.tick(inventory=inv, registry=REG, ctrl=FakeCtrl(),
                 tail_fn=lambda t: FOOTER, conv_fn=lambda cwd: ("conv-A", None))
    assert r1["poked"] == 1
    # same conversation → deduped
    c2 = FakeCtrl()
    r2 = ap.tick(inventory=inv, registry=REG, ctrl=c2,
                 tail_fn=lambda t: FOOTER, conv_fn=lambda cwd: ("conv-A", None))
    assert r2["poked"] == 0 and c2.sends == 0
    # NEW conversation (context rotation happened) → the safe step may be
    # delivered again exactly once
    c3 = FakeCtrl()
    r3 = ap.tick(inventory=inv, registry=REG, ctrl=c3,
                 tail_fn=lambda t: FOOTER, conv_fn=lambda cwd: ("conv-B", None))
    assert r3["poked"] == 1 and c3.sends == 1


def test_stuck_shell_watchdog_reachable_without_explicit_conv_age_fn():
    """Pre-fix: run_loop's tick() never passed conv_age_fn, so conv_age_secs was
    always None and watchdog_stuck_shell could NEVER fire in production."""
    import time
    inv = {"agents": [_real_contract_agent("cp-canary:0.0", "shell_running",
                                           "/root/cp-canary-v2")]}
    old = time.time() - 7200          # conversation quiet for 2h
    res = ap.tick(inventory=inv, registry=REG, ctrl=FakeCtrl(), evaluate_only=True,
                  tail_fn=lambda t: FOOTER, conv_fn=lambda cwd: ("cv1", old))
    assert res["results"][0]["decision"] == "watchdog_stuck_shell"


def test_end_state_met_is_reported_when_footer_all_done():
    """An idle agent whose task footer shows zero unfinished work has MET the
    documented end-state — the decision must say so, not a generic skip."""
    ev = ap.evaluate("cp-canary:0.0", state="idle",
                     tail="4 tasks (4 done, 0 in progress, 0 open)", registry=REG)
    assert ev["decision"] == "end_state_met"


def test_autopilot_run_ledger_dedupes_repeated_skips(tmp_path, monkeypatch):
    """Pre-fix: every tick inserted one autopilot_run row per agent (~7200/day
    for 5 agents) even when the decision never changed."""
    import sqlite3
    inv = {"agents": [_real_contract_agent("cp-canary:0.0", "working",
                                           "/root/cp-canary-v2")]}
    for _ in range(3):
        ap.tick(inventory=inv, registry=REG, ctrl=FakeCtrl(), evaluate_only=True,
                tail_fn=lambda t: "", conv_fn=lambda cwd: (None, None))
    import os
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    n = conn.execute("SELECT count(*) FROM autopilot_run WHERE target='cp-canary:0.0' "
                     "AND decision='skip_progressing'").fetchone()[0]
    conn.close()
    assert n == 1, f"expected 1 deduped ledger row, got {n}"


# ── 2b. waiting_owner is never a poke target; glyph noise must not block pokes ─
def test_waiting_owner_is_never_poked():
    """Pre-fix: POKE_STATES included waiting_owner, so the autopilot would deliver
    text onto a pane that may be showing a tool-permission dialog — the exact
    interaction the safety boundary forbids."""
    ev = ap.evaluate("cp-canary:0.0", state="waiting_owner", tail=FOOTER, registry=REG)
    assert ev["decision"] != "poke"
    assert ev["decision"] == "skip_other_state"


def test_past_tense_spinner_near_tip_does_not_block_a_real_poke():
    """Pre-fix: is_progressing matched the bare ✻ glyph in the last 400 chars, so an
    idle agent whose tail tip retained '✻ Worked for 4s' was silently never poked —
    the autopilot's entire purpose defeated by rendering noise."""
    tail = FOOTER + "\n\n✻ Baked for 4s\n\n❯ \n"
    ev = ap.evaluate("cp-canary:0.0", state="idle", tail=tail, registry=REG)
    assert ev["decision"] == "poke"


def test_compacting_is_live_work_never_poked():
    tail = FOOTER + "\n✻ Compacting conversation…\n❯ \n"
    assert ac.classify_state(True, True, tail) == "working"
    assert ap.evaluate("cp-canary:0.0", state="idle", tail=tail,
                       registry=REG)["decision"] == "skip_progressing"


def test_actuator_refuses_to_paste_onto_pending_input():
    """Pre-fix: the actuator pasted the step onto a NON-EMPTY input line — agent_send
    does not clear the line, so the queued text and the step would CONCATENATE and
    submit as one command. The queued text is never safety-classified."""
    lease = cp.acquire_lease("agent:cp-canary:0.0", "autopilot", ttl_secs=60)
    ctrl = FakeCtrl()
    ctrl.s["pending"] = "rm -rf /opt/production   # queued by someone else"
    out = act.actuate(target="cp-canary:0.0",
                      action_text="continue with the next safe step",
                      controller="autopilot", conversation_id="cvp",
                      lease=lease, ctrl=ctrl, sleep=_no_sleep)
    assert out["acted"] is False
    assert out["reason"] == "pending_input_present"
    assert ctrl.sends == 0, "nothing may be pasted while the input line is occupied"


def test_actuator_submits_same_pending_text_instead_of_repasting():
    """The original live failure: the step was typed but Enter never landed. When the
    pending line IS this exact action, the actuator must press Enter (submit) — never
    paste a second copy, which would deliver the step twice on one line."""
    lease = cp.acquire_lease("agent:cp-canary:0.0", "autopilot", ttl_secs=60)
    ctrl = FakeCtrl()
    step = "continue with the next safe step"
    ctrl.s["pending"] = "  Continue with the  next safe step "   # same text, different ws/case
    out = act.actuate(target="cp-canary:0.0", action_text=step,
                      controller="autopilot", conversation_id="cvs",
                      lease=lease, ctrl=ctrl, sleep=_no_sleep)
    assert out["acted"] is True and out["verified"] is True
    assert ctrl.sends == 0, "must submit the already-typed line, never paste a duplicate"
    assert ctrl.s["pending"] == "", "the typed line must be consumed by the Enter"


def test_pending_guard_defers_without_burning_the_idempotency_key():
    """A deferral must not create a verified/blocked ledger row — once the input line
    clears, the SAME step must still be deliverable exactly once."""
    lease = cp.acquire_lease("agent:cp-canary:0.0", "autopilot", ttl_secs=60)
    ctrl = FakeCtrl()
    ctrl.s["pending"] = "some other queued note"
    step = "continue with the next safe step"
    out = act.actuate(target="cp-canary:0.0", action_text=step, controller="autopilot",
                      conversation_id="cvq", lease=lease, ctrl=ctrl, sleep=_no_sleep)
    assert out["reason"] == "pending_input_present"
    # line clears → the same action now delivers (and only then is it recorded verified)
    ctrl.s["pending"] = ""
    out2 = act.actuate(target="cp-canary:0.0", action_text=step, controller="autopilot",
                       conversation_id="cvq", lease=lease, ctrl=ctrl, sleep=_no_sleep)
    assert out2["acted"] is True and out2["verified"] is True
    assert ctrl.sends == 1


def test_waiting_input_is_a_poke_candidate_actuator_guards_the_line():
    """waiting_input (unsubmitted text) IS a poke state at the autopilot layer — the
    actuator's pending-input guard is what decides: same text → submit; different
    text → defer. Never concatenate."""
    ev = ap.evaluate("cp-canary:0.0", state="waiting_input", tail=FOOTER, registry=REG)
    assert ev["decision"] == "poke"


def test_every_registry_next_step_is_autonomous_safe():
    """PRODUCTION registry invariant: every documented next_step must classify
    autonomous_safe — a registry edit that introduces an unsafe step must fail CI,
    not be silently skipped at runtime."""
    reg = ap.load_registry()
    assert reg, "registry must load"
    for target, entry in reg.items():
        step = entry.get("next_step", "")
        assert act.classify_action(step) == act.AUTONOMOUS_SAFE, (target, step)


# ── 2b2. recall-ghost text must never read as pending input ──────────────────
# EXACT styled bytes captured live from cp-canary:0.0 (2026-08-03): the input box
# shows the DIM (SGR 2) recall ghost of the last submitted command. In a plain
# capture it is indistinguishable from typed text — it deferred every autopilot
# poke (pending_input_present) and would have blocked rotation indefinitely.
GHOST_STYLED = " \x1b[2mcontinue with the next safe canary note\x1b[0m"
TYPED_STYLED = " continue with the next safe canary note\x1b[0m"


def test_dim_recall_ghost_is_not_pending_input():
    assert ac.prompt_text_from_styled(GHOST_STYLED) == ""


def test_real_typed_text_is_pending_input():
    assert ac.prompt_text_from_styled(TYPED_STYLED) == "continue with the next safe canary note"


def test_menu_selection_is_not_pending_input():
    assert ac.prompt_text_from_styled(" 1. Yes, proceed") == ""


# ── 2c. deliver_and_verify retry race (observed LIVE on cp-canary 2026-08-03) ─
def _scripted_clock(monkeypatch, values):
    seq = list(values)
    state = {"t": values[-1]}

    def fake_now():
        if seq:
            state["t"] = seq.pop(0)
        else:
            state["t"] += 100          # exhaust any loop deterministically
        return state["t"]
    monkeypatch.setattr(cw, "_now_ts", fake_now)


class RaceCtrl:
    """Enter reports rc=0 but consumption is controlled per snapshot index —
    reproduces the live race where the first Enter lands BETWEEN the verify
    poll and the retry's re-paste."""

    def __init__(self, step, consume_at=None, foreign_at=None):
        self.step = step
        self.s = {"pending": step, "conv": "m0", "state": "idle", "tail": "t0"}
        self.snaps = 0
        self.pastes = 0
        self.consume_at = consume_at
        self.foreign_at = foreign_at

    def snapshot(self, target, cwd):
        i = self.snaps
        self.snaps += 1
        if self.consume_at is not None and i >= self.consume_at and self.s["pending"] == self.step:
            # the slow Enter finally lands: line consumed, agent starts working
            self.s.update(pending="", conv="m1", state="working", tail="t1")
        if self.foreign_at is not None and i >= self.foreign_at:
            self.s["pending"] = "unrelated text someone queued"
        return {"tail": self.s["tail"], "pending": self.s["pending"],
                "conv_mtime": self.s["conv"], "state": self.s["state"],
                "activity": self.s["tail"]}

    def enter(self, target):
        return 0

    def robust_submit(self, target, text):
        self.pastes += 1
        self.s["pending"] = text
        return True

    def send(self, target, text, idem):
        self.pastes += 1
        self.s["pending"] = text
        return {"submitted": True}


def test_slow_enter_consumed_between_polls_is_not_repasted(monkeypatch):
    """LIVE 2026-08-03 cp-canary: the first Enter was slow; the old retry blindly
    robust_submit-ed and left a DUPLICATE copy of the step on the input line.
    The retry must re-check the line first and never paste a second copy."""
    step = "continue with the next safe step"
    # snapshots: 0=before, 1=poll1, 2=poll-timeout, 3=fresh recheck (consumed HERE), 4=re-poll
    ctrl = RaceCtrl(step, consume_at=3)
    _scripted_clock(monkeypatch, [0, 0, 0.5, 1.5, 10, 10.5])
    out = cw.deliver_and_verify(ctrl, target="x:0.0", cwd="/x", action="submit",
                                step_text=step, expected_pending=step, sleep=_no_sleep)
    assert out["verify"]["ok"] is True
    assert ctrl.pastes == 0, "slow-but-landed Enter must never trigger a duplicate paste"
    assert out["retried"] is False


def test_foreign_text_at_retry_time_is_never_cleared_or_overwritten(monkeypatch):
    """If DIFFERENT text appears on the line between the poll and the retry, the old
    path C-u-wiped it and pasted ours over it — destroying never-classified input.
    Post-fix: no clear, no paste; the failure surfaces to the caller."""
    step = "continue with the next safe step"
    ctrl = RaceCtrl(step, foreign_at=3)
    _scripted_clock(monkeypatch, [0, 0, 0.5, 1.5, 10, 10.5])
    out = cw.deliver_and_verify(ctrl, target="x:0.0", cwd="/x", action="submit",
                                step_text=step, expected_pending=step, sleep=_no_sleep)
    assert ctrl.pastes == 0, "foreign queued text must never be cleared or overwritten"
    assert out["retried"] is False
    assert out["verify"]["ok"] is False
    assert ctrl.s["pending"] == "unrelated text someone queued"


# ── 3. Actuator: caller-supplied policy_class must not bypass the classifier ──
def test_actuator_recomputes_policy_class_forbidden_text_blocked():
    """Pre-fix: actuate(..., policy_class='autonomous_safe') SKIPPED
    classify_action entirely, so a forbidden action text would have been
    delivered to the pane. The classifier must always be recomputed."""
    lease = cp.acquire_lease("agent:cp-canary:0.0", "adversary", ttl_secs=60)
    ctrl = FakeCtrl()
    out = act.actuate(target="cp-canary:0.0", action_text="rm -rf / and push to prod",
                      controller="adversary", conversation_id="cv1",
                      lease=lease, ctrl=ctrl, sleep=_no_sleep,
                      policy_class="autonomous_safe")
    assert out["acted"] is False and out.get("blocked") is True
    assert ctrl.sends == 0, "forbidden text must never reach the pane"


def test_actuator_honours_caller_downgrade():
    # a caller may make policy stricter, never looser
    lease = cp.acquire_lease("agent:cp-canary:0.0", "adversary", ttl_secs=60)
    out = act.actuate(target="cp-canary:0.0", action_text="continue with the next safe step",
                      controller="adversary", conversation_id="cv2",
                      lease=lease, ctrl=FakeCtrl(), sleep=_no_sleep,
                      policy_class="owner_approval_required")
    assert out["acted"] is False and out["reason"] == "owner_approval_required"
