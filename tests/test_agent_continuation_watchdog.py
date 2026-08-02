"""Server-side direct-agent continuation watchdog.

Reproduces the 2026-08-03 typed-but-not-submitted bug and proves the fix: verified
submission (submitted + pane changed + prompt consumed + conversation modified +
state transitioned), one safe Enter retry, blocker on failure, idempotency /
duplicate prevention, owner gate for unsafe text, dead pane, false idle while
thinking, and recovery after a service restart.
"""
from __future__ import annotations

import pytest

from core import agent_continuation_watchdog as cw


NOW = 1_700_000_000.0
DWELL = cw.IDLE_CONFIRM_SECS


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(cw, "ENABLED", True)
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)   # keep failure-path polls short
    yield


# ── pure: is_safe_continuation ───────────────────────────────────────────────
def test_safe_continuation_allows_benign_prose():
    assert cw.is_safe_continuation("continue with the next safe step")
    assert cw.is_safe_continuation("proceed to the next checkpoint")


def test_safe_continuation_blocks_destructive_live_payment_credential_publish():
    for bad in ["rm -rf /", "git push origin main", "publish the release",
                "charge the customer via stripe", "export API_KEY=secret",
                "systemctl restart trading", "withdraw funds to wallet",
                "run on mainnet"]:
        assert not cw.is_safe_continuation(bad), bad


# ── pure: verify (the five proofs) ───────────────────────────────────────────
def _snap(tail, pending, conv, state, activity):
    return {"tail": tail, "pending": pending, "conv_mtime": conv, "state": state,
            "activity": activity}


def test_verify_ok_when_all_proofs_hold():
    before = _snap("t0", "continue", "m0", "idle", "a0")
    after = _snap("t1", "", "m1", "working", "a1")
    v = cw.verify(before, after, expected_pending="continue", enter_rc=0)
    assert v["ok"] and v["submitted"] and v["pane_changed"] and v["prompt_consumed"]
    assert v["conversation_modified"] and v["state_transitioned"]


def test_verify_fails_when_prompt_not_consumed_typed_but_not_submitted():
    # the exact bug: pane changed (text pasted) but the line still holds the text
    before = _snap("t0", "continue", "m0", "idle", "a0")
    after = _snap("t0 [enter]", "continue", "m0", "idle", "a0")
    v = cw.verify(before, after, expected_pending="continue", enter_rc=0)
    assert v["prompt_consumed"] is False and v["ok"] is False


def test_verify_fails_when_enter_keystroke_failed():
    before = _snap("t0", "continue", "m0", "idle", "a0")
    after = _snap("t1", "", "m1", "working", "a1")
    assert cw.verify(before, after, expected_pending="continue", enter_rc=1)["ok"] is False


# ── pure: decide ─────────────────────────────────────────────────────────────
def _agent(target="proj:0.0", state="idle", alive=True, is_agent=True, tail="", pending=""):
    return {"target": target, "session": target.split(":")[0], "alive": alive,
            "is_agent": is_agent, "state": state, "_tail": tail, "_pending": pending,
            "claude_cwd": "/opt/proj"}


def _confirmed_prev():
    return {"idle_since_ts": NOW - DWELL - 5, "last_state": "idle"}


def _d(agent, pending, state, prev=None, eligible=True, proactive=False, conv_count=0,
       continuation="continue with the next safe step"):
    return cw.decide(agent=agent, cfg={}, pending=pending, state=state,
                     prev_target=prev or _confirmed_prev(), now_ts=NOW, eligible=eligible,
                     continuation=continuation, proactive=proactive, conv_count=conv_count)


def test_decide_submits_safe_typed_but_unsubmitted_text():
    d = _d(_agent(), "continue with the next safe step", "idle")
    assert d["action"] == "submit" and d["step_text"] == "continue with the next safe step"


def test_decide_blocks_unsafe_typed_text_owner_gate():
    d = _d(_agent(), "git push origin main", "idle")
    assert d["action"] == "blocker" and d["reason"] == "unsafe_pending_text"


def test_decide_skips_when_not_allowlisted():
    assert _d(_agent(), "continue", "idle", eligible=False)["action"] == "skip"


def test_decide_skips_dead_pane():
    assert _d(_agent(alive=False), "continue", "idle")["action"] == "skip"


def test_decide_skips_while_active_or_thinking():
    assert _d(_agent(), "", "working")["action"] == "skip"
    assert _d(_agent(tail="… esc to interrupt"), "continue", "idle")["reason"] == "thinking"


def test_decide_skips_waiting_owner_that_is_the_supervisors_job():
    assert _d(_agent(state="waiting_owner"), "", "waiting_owner")["reason"] == "waiting_owner_supervisor"


def test_decide_requires_idle_dwell_confirmation():
    fresh = {"idle_since_ts": NOW - 1, "last_state": "idle"}   # only 1s idle
    assert _d(_agent(), "continue", "idle", prev=fresh)["reason"] == "idle_not_confirmed"


def test_decide_proactive_deliver_only_when_opted_in_and_capped():
    assert _d(_agent(), "", "idle", proactive=False)["action"] == "skip"
    assert _d(_agent(), "", "idle", proactive=True)["action"] == "deliver"
    assert _d(_agent(), "", "idle", proactive=True, conv_count=cw.MAX_CONTINUATIONS)["action"] == "skip"


# ── fake controller for deliver_and_verify / run_once ────────────────────────
class FakeCtrl:
    def __init__(self, agents, sessions, behavior):
        self.agents = agents
        self.cfg = {"sessions": sessions}
        self.behavior = behavior            # target -> {will_submit, submit_on_enter}
        self.emitted = []
        self.state = {}
        for a in agents:
            self.state[a["target"]] = {
                "pending": a.get("_pending", ""), "conv": "m0",
                "state": a.get("state", "idle"), "tail": a.get("_tail", ""), "enters": 0}

    def inventory(self):
        return {"agents": self.agents}

    def load_config(self):
        return self.cfg

    def snapshot(self, target, cwd):
        s = self.state[target]
        return {"tail": s["tail"], "pending": s["pending"], "conv_mtime": s["conv"],
                "state": s["state"], "activity": s["tail"]}

    def _try_submit(self, target):
        s = self.state[target]
        s["enters"] += 1
        plan = self.behavior.get(target, {"will_submit": True, "submit_on_enter": 1})
        if plan.get("will_submit", True) and s["enters"] >= plan.get("submit_on_enter", 1):
            s["pending"] = ""; s["conv"] = "m1"; s["state"] = "working"
            s["tail"] = s["tail"] + " [submitted]"
        else:
            s["tail"] = s["tail"] + " [enter]"      # text pasted, NOT consumed
        return 0                                     # tmux keystroke rc always 0

    def enter(self, target):
        return self._try_submit(target)

    def send(self, target, text, idem):
        s = self.state[target]
        s["pending"] = text; s["tail"] = s["tail"] + " [pasted]"
        rc = self._try_submit(target)
        return {"submitted": rc == 0, "pane_changed": True}

    def emit(self, target, project, et, payload, dedup_key):
        self.emitted.append((target, et, dedup_key))
        return True


def _no_sleep(_):
    pass


# ── deliver_and_verify ───────────────────────────────────────────────────────
def test_deliver_and_verify_submit_success_first_try():
    ctrl = FakeCtrl([_agent(pending="continue")], {}, {"proj:0.0": {"will_submit": True}})
    out = cw.deliver_and_verify(ctrl, target="proj:0.0", cwd="/opt/proj", action="submit",
                                step_text="continue", expected_pending="continue", sleep=_no_sleep)
    assert out["verify"]["ok"] is True and out["retried"] is False


def test_deliver_and_verify_retries_enter_once_then_succeeds():
    # typed-but-not-submitted: first Enter does nothing, second submits
    ctrl = FakeCtrl([_agent(pending="continue")], {},
                    {"proj:0.0": {"will_submit": True, "submit_on_enter": 2}})
    out = cw.deliver_and_verify(ctrl, target="proj:0.0", cwd="/opt/proj", action="submit",
                                step_text="continue", expected_pending="continue", sleep=_no_sleep)
    assert out["verify"]["ok"] is True and out["retried"] is True


def test_deliver_and_verify_gives_up_when_never_submits():
    ctrl = FakeCtrl([_agent(pending="continue")], {}, {"proj:0.0": {"will_submit": False}})
    out = cw.deliver_and_verify(ctrl, target="proj:0.0", cwd="/opt/proj", action="submit",
                                step_text="continue", expected_pending="continue", sleep=_no_sleep)
    assert out["verify"]["ok"] is False and out["retried"] is True


# ── run_once integration (dwell needs two passes) ────────────────────────────
def _run_twice(ctrl):
    cw.run_once(ctrl, now_ts=NOW, sleep=_no_sleep)                 # seeds idle dwell
    return cw.run_once(ctrl, now_ts=NOW + DWELL + 5, sleep=_no_sleep)  # acts


def test_run_once_submits_typed_but_not_submitted_bug_and_verifies():
    agents = [_agent(pending="continue with the next safe step")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto", "project": "proj"}},
                    {"proj:0.0": {"will_submit": True}})
    res = _run_twice(ctrl)
    acts = res["actions"]
    assert acts and acts[0]["verified"] is True and acts[0]["action"] == "submit"
    assert any(et == cw.EVENT_CONTINUED for _, et, _ in ctrl.emitted)
    assert cw.health()["verified"] >= 1


def test_run_once_retries_then_blocks_when_not_verifiable():
    agents = [_agent(pending="continue with the next safe step")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": False}})
    res = _run_twice(ctrl)
    a = res["actions"][0]
    assert a["blocked"] is True and a["verify"]["ok"] is False
    assert any(et == cw.EVENT_BLOCKED for _, et, _ in ctrl.emitted)


def test_run_once_unsafe_pending_is_blocked_never_submitted():
    agents = [_agent(pending="git push origin main")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": True}})
    res = _run_twice(ctrl)
    assert res["actions"][0]["action"] == "blocker"
    # no Enter/submit was ever attempted
    assert ctrl.state["proj:0.0"]["enters"] == 0
    assert any(et == cw.EVENT_BLOCKED for _, et, _ in ctrl.emitted)


def test_run_once_skips_dead_and_non_allowlisted():
    agents = [_agent(target="dead:0.0", alive=False, pending="continue"),
              _agent(target="other:0.0", pending="continue")]      # 'other' not in config
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {})
    res = _run_twice(ctrl)
    assert res["actions"] == [] and res["health"]["agents_checked"] == 0


def test_duplicate_prevention_same_step_not_repeated():
    agents = [_agent(pending="continue with the next safe step")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": True}})
    _run_twice(ctrl)
    enters_after_first = ctrl.state["proj:0.0"]["enters"]
    # agent went idle again but conversation unchanged (m1) → same step_hash → no repeat
    ctrl.state["proj:0.0"]["state"] = "idle"
    ctrl.state["proj:0.0"]["pending"] = "continue with the next safe step"
    res3 = cw.run_once(ctrl, now_ts=NOW + 2 * DWELL + 20, sleep=_no_sleep)
    assert all(not a.get("verified") for a in res3["actions"]) or res3["actions"] == []
    assert ctrl.state["proj:0.0"]["enters"] == enters_after_first   # no new Enter


def test_recovery_after_restart_does_not_repeat_verified_step():
    agents = [_agent(pending="continue with the next safe step")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": True}})
    _run_twice(ctrl)
    enters = ctrl.state["proj:0.0"]["enters"]
    # simulate a service restart: brand-new controller + module reload, SAME db file
    ctrl2 = FakeCtrl([_agent(pending="continue with the next safe step")],
                     {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": True}})
    ctrl2.state["proj:0.0"]["conv"] = "m1"      # same conversation as after the submit
    res = cw.run_once(ctrl2, now_ts=NOW + 100, sleep=_no_sleep)
    cw.run_once(ctrl2, now_ts=NOW + 100 + DWELL + 5, sleep=_no_sleep)
    # idempotency ledger (persisted in the db) blocks re-submitting the done step
    assert ctrl2.state["proj:0.0"]["enters"] == 0
    assert enters >= 1


def test_health_surface_reports_last_action():
    agents = [_agent(pending="continue with the next safe step")]
    ctrl = FakeCtrl(agents, {"proj": {"mode": "auto"}}, {"proj:0.0": {"will_submit": True}})
    _run_twice(ctrl)
    h = cw.health()
    assert h["enabled"] is True and h["last_run_at"] is not None
    assert "continued" in (h["last_action"] or "")
