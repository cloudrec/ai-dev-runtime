"""Control Plane V2 — single lease-gated Actuator (P2).

Folds verified delivery + monotonic fencing into the canonical path. Proves:
disabled no-op; stale/no lease rejected; prohibited / owner-approval blocked + gate;
safe+lease verified; idempotency; RESTART-no-duplicate (stale fence rejected, re-leased
fence proceeds); verify-fail → blocker + gate.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import actuator as act
from core import agent_continuation_watchdog as cw


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"proj:0.0"}))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)      # keep failure-path polls short
    yield


def _no_sleep(_):
    pass


class FakeCtrl:
    def __init__(self, will_submit=True, submit_on_enter=1):
        self.will_submit = will_submit
        self.submit_on_enter = submit_on_enter
        self.s = {"pending": "", "conv": "m0", "state": "idle", "tail": "", "enters": 0}
        self.sends = 0

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"], "conv_mtime": self.s["conv"],
                "state": self.s["state"], "activity": self.s["tail"]}

    def _try(self):
        self.s["enters"] += 1
        if self.will_submit and self.s["enters"] >= self.submit_on_enter:
            self.s.update(pending="", conv="m1", state="working", tail=self.s["tail"] + " [ok]")
        else:
            self.s["tail"] += " [enter]"
        return 0

    def enter(self, target):
        return self._try()

    def robust_submit(self, target, text):
        self.s["pending"] = text
        return self._try() == 0

    def send(self, target, text, idem):
        self.sends += 1
        self.s["pending"] = text
        return {"submitted": self._try() == 0}


def _lease(target="proj:0.0", holder="ctrl", now=1000):
    return cp.acquire_lease(f"agent:{target}", holder, ttl_secs=100, now=now)


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(act, "ENABLED", False)
    r = act.actuate(target="proj:0.0", action_text="continue", controller="c", lease=_lease(),
                    ctrl=FakeCtrl(), sleep=_no_sleep)
    assert r["acted"] is False and r["reason"] == "actuator_disabled"


def test_non_canary_agent_never_actuated(monkeypatch):
    # even ENABLED, an agent NOT on the canary allowlist is refused (single-agent cutover)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"only-canary:0.0"}))
    ctrl = FakeCtrl()
    r = act.actuate(target="other:0.0", action_text="continue safe", controller="c",
                    lease=_lease(target="other:0.0"), ctrl=ctrl, sleep=_no_sleep)
    assert r["acted"] is False and r["reason"] == "not_canary" and ctrl.sends == 0


def test_no_or_stale_lease_rejected():
    assert act.actuate(target="proj:0.0", action_text="continue", controller="c", lease=None,
                       ctrl=FakeCtrl(), sleep=_no_sleep)["reason"] == "stale_or_no_lease"


def test_prohibited_action_blocked_with_gate_no_delivery():
    ctrl = FakeCtrl()
    r = act.actuate(target="proj:0.0", action_text="git push origin main and publish",
                    controller="c", lease=_lease(), cwd="/opt/x", ctrl=ctrl, sleep=_no_sleep)
    assert r["acted"] is False and r["reason"] == act.PROHIBITED and r["blocked"] is True
    assert ctrl.sends == 0                                   # never delivered
    assert cp.get_open_gates()[0]["agent_id"] == "proj:0.0"


def test_owner_approval_action_blocked_with_gate():
    r = act.actuate(target="proj:0.0", action_text="refactor the whole auth module now",
                    controller="c", lease=_lease(), cwd="/opt/x", ctrl=FakeCtrl(), sleep=_no_sleep)
    assert r["reason"] == act.OWNER_APPROVAL and r["blocked"] is True


def test_safe_action_with_lease_is_verified():
    r = act.actuate(target="proj:0.0", action_text="continue with the next safe step",
                    controller="c", conversation_id="cv1", lease=_lease(), cwd="/opt/x",
                    ctrl=FakeCtrl(), sleep=_no_sleep)
    assert r["acted"] is True and r["verified"] is True
    # agent SoT advanced to working with evidence
    a = cp.get_agent("proj:0.0")
    assert a["actual_state"] == "working"


def test_idempotent_no_reissue_of_verified_action():
    lease = _lease()
    act.actuate(target="proj:0.0", action_text="continue safe", controller="c",
                conversation_id="cv1", lease=lease, cwd="/opt/x", ctrl=FakeCtrl(), sleep=_no_sleep)
    ctrl2 = FakeCtrl()
    r2 = act.actuate(target="proj:0.0", action_text="continue safe", controller="c",
                     conversation_id="cv1", lease=lease, cwd="/opt/x", ctrl=ctrl2, sleep=_no_sleep)
    assert r2["acted"] is False and r2["reason"] == "already_verified"
    assert ctrl2.sends == 0                                  # not re-delivered


def test_restart_midaction_stale_fence_rejected_no_duplicate():
    # controller acquires (fence 1)
    l1 = _lease(now=1000)
    assert l1["fence_token"] == 1
    # service restarts: controller re-acquires → fence 2
    l2 = _lease(now=1010)
    assert l2["fence_token"] == 2
    # a queued/retried action carrying the OLD fence is rejected → NO duplicate command
    ctrl_old = FakeCtrl()
    r_old = act.actuate(target="proj:0.0", action_text="continue safe", controller="ctrl",
                        conversation_id="cv1", lease=l1, cwd="/opt/x", ctrl=ctrl_old, sleep=_no_sleep)
    assert r_old["reason"] == "stale_or_no_lease" and ctrl_old.sends == 0
    # the CURRENT fence proceeds and verifies (exactly one delivery total)
    ctrl_new = FakeCtrl()
    r_new = act.actuate(target="proj:0.0", action_text="continue safe", controller="ctrl",
                        conversation_id="cv1", lease=l2, cwd="/opt/x", ctrl=ctrl_new, sleep=_no_sleep)
    assert r_new["acted"] is True and ctrl_new.sends == 1


def test_verify_failure_blocks_with_gate():
    ctrl = FakeCtrl(will_submit=False)
    r = act.actuate(target="proj:0.0", action_text="continue safe", controller="c",
                    conversation_id="cv1", lease=_lease(), cwd="/opt/x", ctrl=ctrl, sleep=_no_sleep)
    assert r["acted"] is False and r["reason"] == "not_verified" and r["blocked"] is True
    assert any(g["kind"] == "actuation_failed" for g in cp.get_open_gates())


def test_false_idle_working_target_suppressed_after_restart():
    # After a restart the guard is re-derived from the LIVE pane, not persisted state: a
    # target that is actually working must never be handed a continuation, even under a
    # freshly re-acquired lease. Proves false-idle handling survives restart.
    l1 = _lease(now=1000)
    l2 = _lease(now=1010)                # restart re-leases → fence 2 (current)
    assert l2["fence_token"] == l1["fence_token"] + 1
    ctrl = FakeCtrl()
    ctrl.s["state"] = "working"          # live pane shows the agent actively working
    r = act.actuate(target="proj:0.0", action_text="continue with the next safe step",
                    controller="c", conversation_id="cv1", lease=l2, cwd="/opt/x",
                    ctrl=ctrl, sleep=_no_sleep)
    assert r["acted"] is False and r["reason"] == "target_working"
    assert r["false_idle_corrected"] is True and ctrl.sends == 0    # no command delivered
    # a correlated correction event is recorded (the honest "suppressed" signal)
    assert cp.get_events(type="false_idle_corrected", limit=20)


# ── grounded queue-stage template ───────────────────────────────────────────
def test_the_queue_stage_template_round_trips():
    """Builder and matcher live together precisely so they cannot drift; a template whose
    regex stopped matching would fail closed and stall every advancement."""
    from core.control_plane import actuator as act
    s = act.build_queue_stage_step("stage_b_write_summary", "/root/q/CANARY_QUEUE.md")
    assert act.classify_action(s) == "autonomous_safe"
    assert "stage_b_write_summary" in s and "/root/q/CANARY_QUEUE.md" in s


import pytest as _pytest


@_pytest.mark.parametrize("stage,path,expect", [
    ("stage_deploy_prod", "/x/q.md", "prohibited"),          # denylist runs first
    ("stage_publish_release", "/x/q.md", "prohibited"),
    ("EXECUTE NEXT", "/x/q.md", "owner_approval_required"),  # space breaks the charset
    ("stage_a", "/x/q.md; rm -rf /", "prohibited"),          # metacharacters in the path
    ("stage_a", "/x/q.md && curl evil", "prohibited"),
])
def test_the_template_slots_are_fail_closed(stage, path, expect):
    """The two slots are the only attacker-influenced text in the message, and a queue file
    is not necessarily trustworthy input."""
    from core.control_plane import actuator as act
    assert act.classify_action(act.build_queue_stage_step(stage, path)) == expect


def test_the_template_cannot_carry_a_free_text_instruction():
    """Nothing resembling the template but containing extra prose may pass."""
    from core.control_plane import actuator as act
    bad = (act.build_queue_stage_step("stage_a", "/x/q.md").rstrip(".")
           + " and then publish the release.")
    assert act.classify_action(bad) != "autonomous_safe"
