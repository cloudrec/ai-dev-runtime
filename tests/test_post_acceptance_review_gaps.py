"""Post-acceptance local review (checkpoint 7ad6b72) — the registry owner gate.

Finding: `config/commander_autopilot.yaml` documents `live_actuation: false  # owner gate`
per project, and `evaluate()` parsed the flag into its assessment — but NOTHING ever read
it when deciding to actuate. The only real gate was the env allowlist
`CONTROL_PLANE_CANARY_AGENTS`. Two gates were documented; one existed.

Consequence: adding an agent to that env var (a single systemd drop-in edit — exactly what
the acceptance run did for the canary) would actuate it even though its registry entry
still said `live_actuation: false`. For payment / arbitrage2-opus / mess-qa-automation the
registry flag is the owner's own written record of "not approved", so it must bind.

Fixed by enforcing BOTH gates in `deliver_next_step`, checked only for targets inside the
allowlist so the non-canary path keeps returning the actuator's `not_canary` refusal
unchanged. These tests FAIL on pre-fix `7ad6b72`.
"""
from __future__ import annotations

import pytest

from core import commander_autopilot as ap
from core.control_plane import actuator as act


SAFE_STEP = "continue with the next safe canary note"

# The real shape of the shipped registry: canary approved, everything else owner-gated.
REG = {
    "cp-canary:0.0": {"root": "/root/cp-canary-v2", "next_step": SAFE_STEP,
                      "live_actuation": True},
    "payment:0.0": {"root": "/opt/payment-orchestrator",
                    "next_step": "continue the read-only connection-mapping recovery",
                    "live_actuation": False},
    "mess-qa-automation:0.0": {"root": "/opt/mess-qa-automation",
                               "next_step": "continue the next safe QA/test step",
                               "live_actuation": False},
}


class FakeCtrl:
    """Records every pane interaction. Any non-zero count is a gate failure."""

    def __init__(self):
        self.sends = 0
        self.enters = 0
        self.s = {"tail": "❯ ready\nrepo clean", "pending": "", "conv_mtime": "m0",
                  "state": "idle", "activity": "ready", "capture_ok": True}

    def snapshot(self, target, cwd):
        return dict(self.s)

    def _ok(self):
        self.s.update(pending="", conv_mtime="m1", state="working",
                      tail=self.s["tail"] + " [ok]")
        return 0

    def send(self, target, text, idem):
        self.sends += 1
        self.s["pending"] = text
        return {"submitted": self._ok() == 0}

    def enter(self, target):
        self.enters += 1
        return self._ok()

    def robust_submit(self, target, text):
        self.s["pending"] = text
        return self._ok() == 0


def _no_sleep(_):
    pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(act, "ENABLED", True)
    yield


# ═══ 1. an allowlisted agent whose registry withholds live_actuation is refused ═══
@pytest.mark.parametrize("target", ["payment:0.0", "mess-qa-automation:0.0"])
def test_allowlisted_but_registry_gated_agent_is_never_actuated(monkeypatch, target):
    """The dangerous case: the env allowlist was widened but the registry still says the
    owner has NOT approved this agent. Pre-fix: acted=True, keystrokes delivered."""
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({target}))
    ctrl = FakeCtrl()
    out = ap.deliver_next_step(target, SAFE_STEP, conversation_id="cv1",
                               cwd="/tmp", ctrl=ctrl, sleep=_no_sleep, registry=REG)
    assert out["acted"] is False
    assert out["reason"] == "registry_live_actuation_disabled", out
    assert ctrl.sends == 0 and ctrl.enters == 0, "no keystroke may reach a gated agent"


def test_allowlisted_target_absent_from_the_registry_is_denied_by_default(monkeypatch):
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"stranger:0.0"}))
    ctrl = FakeCtrl()
    out = ap.deliver_next_step("stranger:0.0", SAFE_STEP, ctrl=ctrl, sleep=_no_sleep,
                               registry=REG)
    assert out["acted"] is False and out["reason"] == "registry_live_actuation_disabled"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_tick_honours_the_registry_gate_even_with_a_widened_allowlist(monkeypatch):
    """End to end through the production tick: allowlist widened to payment, registry
    still withholding it. The tick must evaluate and refuse, touching no pane."""
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0", "payment:0.0"}))
    ctrl = FakeCtrl()
    inv = {"agents": [{"target": "payment:0.0", "session": "payment", "alive": True,
                       "is_agent": True, "state": "idle", "claude_cwd": "/opt/x",
                       "_tail": "3 tasks (1 done, 0 in progress, 2 open)",
                       "claude_conversation": "cv-pay"}]}
    out = ap.tick(inventory=inv, registry={"payment:0.0": REG["payment:0.0"]}, ctrl=ctrl)
    r = out["results"][0] if isinstance(out, dict) and out.get("results") else None
    assert r is not None, out
    assert r["actuation"]["reason"] == "registry_live_actuation_disabled", r
    assert r["delivered"] is False
    assert ctrl.sends == 0 and ctrl.enters == 0


# ═══ 2. anti-overcorrection: the approved canary path is unchanged ════════════
def test_approved_canary_still_actuates(monkeypatch):
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    ctrl = FakeCtrl()
    out = ap.deliver_next_step("cp-canary:0.0", SAFE_STEP, conversation_id="cv-ok",
                               cwd="/root/cp-canary-v2", ctrl=ctrl, sleep=_no_sleep,
                               registry=REG)
    assert out["acted"] is True and out["verified"] is True


def test_non_canary_target_still_reports_not_canary(monkeypatch):
    """Scope pin: the pre-existing refusal path and its reason string are untouched, so
    the acceptance-era evidence (`not_canary` for owner-gated agents) still holds."""
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    ctrl = FakeCtrl()
    out = ap.deliver_next_step("payment:0.0", REG["payment:0.0"]["next_step"],
                               ctrl=ctrl, sleep=_no_sleep, registry=REG)
    assert out["acted"] is False and out["reason"] == "not_canary"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_unsafe_step_still_blocked_before_any_gate(monkeypatch):
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    ctrl = FakeCtrl()
    out = ap.deliver_next_step("cp-canary:0.0", "git push and deploy to prod",
                               ctrl=ctrl, sleep=_no_sleep, registry=REG)
    assert out["acted"] is False and out["reason"] == "unsafe_step_blocked"
    assert ctrl.sends == 0 and ctrl.enters == 0


def test_shipped_registry_grants_live_actuation_to_the_approved_set_only():
    """CI invariant over the REAL shipped file. Owner-approved set (2026-08-04): the
    canary plus the two managed sessions. payment and owneros must NEVER appear — payment
    is excluded under every revision of this policy. Any other grant fails loudly."""
    reg = ap.load_registry()
    granted = sorted(t for t, e in reg.items() if e.get("live_actuation"))
    assert granted == ["arbitrage2-opus:0.0", "cp-canary:0.0", "mess-qa-automation:0.0"], granted
    for never in ("payment:0.0", "owneros-direct-fix:0.0"):
        assert never not in granted, never


def test_every_shipped_next_step_is_autonomous_safe():
    """The delivered text must pass the safety classifier for every granted project — the
    autopilot may never instruct a build/sign/publish/release/restart or any trading action."""
    reg = ap.load_registry()
    for target, entry in reg.items():
        step = entry.get("next_step", "")
        assert ap.classify_safety(step) == "autonomous_safe", (target, step)
    for banned in ("publish", "release", "deploy", "restart", "sign", "trade", "order",
                   "venue", "key", "payment"):
        for target, entry in reg.items():
            assert banned not in entry.get("next_step", "").lower(), (target, banned)
