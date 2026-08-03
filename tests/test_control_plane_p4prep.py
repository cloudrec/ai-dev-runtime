"""Control Plane V2 — P4 PREPARATION (all live actuation flags OFF by default).

Routes the continuation watchdog through the canonical lease-gated Actuator, and adds
blocker-resolution events. Proves: the bridge acquires a lease + verifies via the actuator;
routing is a safe no-op when the actuator is disabled (dormant); a blocked action that later
verifies emits a correlated blocker_resolved and closes the SYSTEM gate — but never an
owner-decision gate.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import actuator as act, resolutions
from core import agent_continuation_watchdog as cw


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"proj:0.0"}))
    yield


class FakeCtrl:
    def __init__(self, will_submit=True):
        self.will_submit = will_submit
        self.s = {"pending": "", "conv": "m0", "state": "idle", "tail": "", "enters": 0}
        self.sends = 0

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"], "conv_mtime": self.s["conv"],
                "state": self.s["state"], "activity": self.s["tail"]}

    def _try(self):
        self.s["enters"] += 1
        if self.will_submit:
            self.s.update(pending="", conv="m1", state="working", tail="[ok]")
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


# ── bridge routes watchdog → actuator under a lease (flags ON in test) ───────
def test_bridge_routes_through_actuator_and_verifies(monkeypatch):
    monkeypatch.setattr(act, "ENABLED", True)
    ctrl = FakeCtrl()
    out = cw.deliver_via_actuator("proj:0.0", "continue with the next safe step", "cv1",
                                  "/opt/x", ctrl)
    assert out["acted"] is True and out["verified"] is True
    # a lease was taken for the agent by the watchdog controller
    h = cp.lease_holder("agent:proj:0.0")
    assert h and h["holder"] == "continuation_watchdog"


def test_routing_is_safe_noop_when_actuator_disabled(monkeypatch):
    monkeypatch.setattr(act, "ENABLED", False)          # default posture
    ctrl = FakeCtrl()
    out = cw.deliver_via_actuator("proj:0.0", "continue with the next safe step", "cv1",
                                  "/opt/x", ctrl)
    assert out["acted"] is False and out["reason"] == "actuator_disabled"
    assert ctrl.sends == 0                              # nothing delivered — dormant


def test_run_once_route_flag_on_but_actuator_off_delivers_nothing(monkeypatch):
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "ROUTE_VIA_ACTUATOR", True)
    monkeypatch.setattr(act, "ENABLED", False)

    agents = [{"target": "arb:0.0", "session": "arb", "is_agent": True, "alive": True,
               "state": "idle", "claude_cwd": "/opt/arbitrage2",
               "_pending": "continue with the next safe step"}]

    class Ctrl(FakeCtrl):
        def inventory(self):
            return {"agents": agents}

        def load_config(self):
            return {"sessions": {"arb": {"mode": "auto", "project": "arbitrage2"}}}

        def emit(self, *a, **k):
            return True

    c = Ctrl()
    # two ticks to pass the idle dwell
    cw.run_once(c, now_ts=1000, sleep=lambda _: None)
    res = cw.run_once(c, now_ts=1000 + cw.IDLE_CONFIRM_SECS + 5, sleep=lambda _: None)
    # routed to a disabled actuator → no verified continuation, nothing sent
    assert all(not a.get("verified") for a in res["actions"])
    assert c.sends == 0


# ── blocker resolution: blocked → later verified emits blocker_resolved ──────
def test_blocked_then_verified_resolves_system_gate_not_owner_gate(monkeypatch):
    monkeypatch.setattr(act, "ENABLED", True)
    lease = cp.acquire_lease("agent:proj:0.0", "continuation_watchdog", now=1000)
    # an independent OWNER-DECISION gate that must NOT be auto-closed
    owner_gate = cp.open_gate(agent_id="proj:0.0", reason="stop selling?", kind="business")

    # first actuate FAILS → blocked + actuation_failed system gate
    r1 = act.actuate(target="proj:0.0", action_text="continue with the next safe step",
                     controller="continuation_watchdog", conversation_id="cv1", lease=lease,
                     cwd="/opt/x", ctrl=FakeCtrl(will_submit=False), sleep=lambda _: None)
    assert r1["blocked"] is True
    assert any(g["kind"] == "actuation_failed" for g in cp.get_open_gates())

    # retry actuate SUCCEEDS → verified → blocker_resolved + system gate closed
    r2 = act.actuate(target="proj:0.0", action_text="continue with the next safe step",
                     controller="continuation_watchdog", conversation_id="cv1", lease=lease,
                     cwd="/opt/x", ctrl=FakeCtrl(will_submit=True), sleep=lambda _: None)
    assert r2["acted"] is True
    from core.control_plane import cto
    assert any(e["type"] == "blocker_resolved" for e in cto.cto_brief_since("t")["events"])
    kinds = [g["kind"] for g in cp.get_open_gates()]
    assert "actuation_failed" not in kinds          # system blocker cleared
    assert "business" in kinds                       # owner-decision gate preserved


def test_resolve_blocker_never_closes_owner_decision_gate():
    cp.open_gate(agent_id="a:0.0", reason="scope?", kind="unverified_owner_decision")
    cp.open_gate(agent_id="a:0.0", reason="fail", kind="actuation_failed")
    resolutions.resolve_blocker("a:0.0", reason="cleared")
    kinds = [g["kind"] for g in cp.get_open_gates()]
    assert "actuation_failed" not in kinds and "unverified_owner_decision" in kinds
