"""Commander Autopilot — auto-discovery, auto-next-step, false-idle, background-subagent,
restart persistence, dedupe, safety boundaries. Actuation is routed through the lease-gated
Actuator (confined to CANARY_AGENTS), so non-canary agents are evaluate-only (owner-gated).
"""
from __future__ import annotations

import pytest

from core import commander_autopilot as ap
from core.control_plane import actuator as act
from core import agent_continuation_watchdog as cw
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
            self.s.update(pending="", conv="m1", state="working", tail=self.s["tail"] + " [ok]")
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


REG = {
    "cp-canary:0.0": {"root": "/root/cp-canary-v2", "live_actuation": True,
                      "next_step": "continue with the next safe canary note; append a dated line."},
    "payment:0.0": {"root": "/opt/payment-orchestrator", "live_actuation": False,
                    "next_step": "continue the read-only recovery and write a progress report."},
}
FOOTER = "  3 tasks (0 done, 1 in progress, 2 open)"


# ── auto-discovery / registry ────────────────────────────────────────────────
def test_registry_has_five_critical_projects():
    reg = ap.load_registry()
    for t in ("owneros-direct-fix:0.0", "cp-canary:0.0", "payment:0.0",
              "arbitrage2-opus:0.0", "mess-qa-automation:0.0"):
        assert t in reg and reg[t].get("end_state") and reg[t].get("next_step")


def test_parse_task_footer():
    tf = ap.parse_task_footer("5 tasks (1 done, 1 in progress, 3 open)")
    assert tf["present"] and tf["done"] == 1 and tf["open"] == 3 and tf["has_unfinished"] is True
    assert ap.parse_task_footer("4 tasks (4 done, 0 in progress, 0 open)")["has_unfinished"] is False


# ── evaluate: auto-next-step decision + false-idle + background subagent ─────
def test_idle_with_unfinished_safe_is_poke():
    ev = ap.evaluate("cp-canary:0.0", state="idle", tail=FOOTER, registry=REG)
    assert ev["decision"] == "poke" and ev["safety"] == "autonomous_safe"


def test_working_is_skip_progressing():
    assert ap.evaluate("cp-canary:0.0", state="working", tail=FOOTER, registry=REG)["decision"] \
        == "skip_progressing"


def test_shell_running_is_skip():
    assert ap.evaluate("cp-canary:0.0", state="shell_running", tail=FOOTER, registry=REG)["decision"] \
        == "skip_progressing"


def test_background_subagent_counts_as_working():
    tail = FOOTER + "\n· 3 agents running · spawned 2 subagents working"
    ev = ap.evaluate("cp-canary:0.0", state="idle", tail=tail, registry=REG)
    assert ev["background_subagent"] is True and ev["decision"] == "skip_progressing"


def test_active_exec_marker_not_poked():
    ev = ap.evaluate("cp-canary:0.0", state="idle", tail=FOOTER + "\nesc to interrupt", registry=REG)
    assert ev["decision"] == "skip_progressing"


def test_no_unfinished_work_is_end_state_met():
    # a footer with ZERO unfinished work is the documented end-state, reported as such
    tail = "4 tasks (4 done, 0 in progress, 0 open)"
    assert ap.evaluate("cp-canary:0.0", state="idle", tail=tail, registry=REG)["decision"] \
        == "end_state_met"


def test_no_footer_and_no_step_is_skip_no_work():
    reg = {"x:0.0": {"root": "/tmp", "next_step": ""}}
    assert ap.evaluate("x:0.0", state="idle", tail="", registry=reg)["decision"] == "skip_no_work"


# ── safety boundaries ────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "run the payment payout and transfer funds",
    "execute a live trade on mainnet",
    "git push to promote traffic to prod DB",
    "rotate the api_key credential in .env",
    "publish and deploy the release",
    "rm -rf the build dir",
])
def test_unsafe_next_step_is_not_poked(bad):
    reg = {"x:0.0": {"root": "/tmp", "next_step": bad, "live_actuation": True}}
    ev = ap.evaluate("x:0.0", state="idle", tail=FOOTER, registry=reg)
    assert ev["decision"] == "skip_unsafe" and ev["safety"] != "autonomous_safe"


def test_deliver_blocks_unsafe_step():
    out = ap.deliver_next_step("cp-canary:0.0", "git push and deploy to prod", ctrl=FakeCtrl())
    assert out["acted"] is False and out["reason"] == "unsafe_step_blocked"


# ── watchdogs ────────────────────────────────────────────────────────────────
def test_dead_agent_watchdog_no_duplicate():
    ev = ap.evaluate("cp-canary:0.0", state="dead", tail="", registry=REG)
    assert ev["decision"] == "watchdog_dead" and "NO duplicate" in ev["note"]


def test_stuck_shell_watchdog():
    ev = ap.evaluate("cp-canary:0.0", state="shell_running", tail=FOOTER, conv_age_secs=3600,
                     registry=REG)
    assert ev["decision"] == "watchdog_stuck_shell"


def test_false_completion_watchdog():
    ev = ap.evaluate("cp-canary:0.0", state="completed", tail=FOOTER, registry=REG)  # open tasks
    assert ev["decision"] == "watchdog_false_completion"


# ── actuation via Actuator: canary pokes, non-canary owner-gated ─────────────
def test_deliver_to_canary_actuates_and_verifies():
    out = ap.deliver_next_step("cp-canary:0.0", "continue with the next safe canary note",
                               conversation_id="cv1", cwd="/root/cp-canary-v2", ctrl=FakeCtrl(),
                               sleep=_no_sleep)
    assert out["acted"] is True and out["verified"] is True


def test_deliver_non_canary_is_owner_gated():
    out = ap.deliver_next_step("payment:0.0", "continue the read-only recovery and report",
                               ctrl=FakeCtrl(), sleep=_no_sleep)
    assert out["acted"] is False and out["reason"] == "not_canary"


def test_tick_pokes_canary_and_gates_others():
    inv = {"agents": [
        {"target": "cp-canary:0.0", "state": "idle", "_tail": FOOTER, "claude_conversation": "cv1"},
        {"target": "payment:0.0", "state": "idle", "_tail": FOOTER, "claude_conversation": "cv2"},
    ]}
    res = ap.tick(inventory=inv, registry=REG, ctrl=FakeCtrl(), now=1000)
    by = {r["target"]: r for r in res["results"]}
    assert by["cp-canary:0.0"]["delivered"] is True
    assert by["payment:0.0"]["decision"] == "poke_owner_gated"    # evaluated, not actuated
    assert res["poked"] == 1 and res["owner_gated"] == 1


# ── restart persistence + dedupe (Actuator fence/idempotency) ────────────────
def test_restart_persistence_and_dedupe_no_reissue():
    inv = {"agents": [{"target": "cp-canary:0.0", "state": "idle", "_tail": FOOTER,
                       "claude_conversation": "cv1"}]}
    r1 = ap.tick(inventory=inv, registry=REG, ctrl=FakeCtrl(), now=1000)
    assert r1["poked"] == 1
    # a fresh FakeCtrl == a restart; same conversation+step → Actuator idempotency blocks re-issue
    ctrl2 = FakeCtrl()
    r2 = ap.tick(inventory=inv, registry=REG, ctrl=ctrl2, now=1100)
    assert r2["poked"] == 0                       # no duplicate poke
    assert ctrl2.sends == 0                        # nothing re-delivered
    by = {r["target"]: r for r in r2["results"]}
    assert by["cp-canary:0.0"]["actuation_reason"] == "already_verified"
