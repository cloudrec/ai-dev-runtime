"""D1 — an attempt that goes silent (tech-lead decision, Stage 4).

After FULLCYCLE_ATTEMPT_TIMEOUT (7200 s) without a result, or UNKNOWN_POLL_LIMIT (20)
unreadable agent statuses in a row, the engine asks the tech lead ONCE per attempt (a
"blocked" review carrying `stalled`). It never kills, restarts or resends on its own; a
resend (the tech lead's "answer") or a cancel waits until the old attempt is proven over:
its late result is verified instead, and otherwise the agent must be provably idle.

Same harness as test_fullcycle_e2e: real engine, tmux, git, contracts; the agent is the
fixture script; the reviewer is driven by hand here. The clock is shifted, not slept.
"""
from __future__ import annotations

import json
import os

import pytest

from core import task_contract as tc
from core.fullcycle import engine as fe
from core.fullcycle.executor import UNKNOWN, Observation
from tests.test_fullcycle_e2e import _events, _state, fc, h  # noqa: F401

HOUR2 = fe.ATTEMPT_TIMEOUT


class Clock:
    def __init__(self, e):
        self.base, self.offset = e.clock, 0.0
        e.clock = self

    def __call__(self):
        return self.base() + self.offset


def _task(e, key):
    return next(t for t in e._tasks() if t["key"] == key)


def _stall_reviews(e):
    return [r for r in fe.pending_reviews(e.project_id) if r["payload"].get("stalled")]


def _all_stall_rows(e):
    rows = e.conn.execute("SELECT id, task_id, attempt, payload FROM fc_review WHERE kind='blocked'"
                          ).fetchall()
    return [r for r in rows if json.loads(r[3]).get("stalled")]


def _hung_add(fc, behaviours=None, **kw):
    e, a, log, root = fc(behaviours or {"add": ["hang", "correct"]}, **kw)
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.WORKING, 15)
    h.wait_for(lambda: len(h.agent_log(log)) == 1, 10)
    clock = Clock(e)
    a.restart = lambda handle: pytest.fail("the engine must never restart on a stall")
    return e, a, log, root, clock


def _answer(e, choice, key="k1"):
    (rv,) = _stall_reviews(e)
    fe.ack_review(rv["review_id"], by="stub-gpt")
    return fe.submit_decision(rv["review_id"], choice, key=key, by="stub-gpt",
                              feedback="the agent looked stuck")


def _write_late_result(root, task_key, attempt, files):
    for rel, body in files.items():
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(body)
    path = os.path.join(root, ".fullcycle", "results", f"{task_key}-{attempt}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"task_id": f"tinycalc:{task_key}", "attempt": attempt, "status": "done",
                   "summary": "late", "changed_files": list(files)}, fh)


# ── the alert ────────────────────────────────────────────────────────────────
def test_no_alert_before_the_deadline(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 - 60
    for _ in range(3):
        e.tick()
    assert _stall_reviews(e) == []


def test_a_silent_attempt_alerts_the_tech_lead_exactly_once(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    for _ in range(6):
        e.tick()
    (rv,) = _stall_reviews(e)
    assert rv["kind"] == "blocked" and rv["payload"]["stalled"] == "attempt_timeout"
    assert rv["payload"]["timeout_s"] == HOUR2 and rv["attempt"] == 1
    review_events = [json.loads(p) for _, _, p, _ in _events(fe.REVIEW_EVENT)]
    assert [x.get("stalled") for x in review_events] == ["attempt_timeout"]
    assert _events(fe.OWNER_EVENT) == []                 # routine: tech lead, not owner
    assert len(h.agent_log(log)) == 1                    # nothing resent
    assert _state(e.status(), "add")["state"] == tc.WORKING


def test_the_deadline_and_the_once_survive_an_engine_restart(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    assert len(_all_stall_rows(e)) == 1
    e.release()
    e2 = fe.Engine("tinycalc", a, holder="e2")
    Clock(e2).offset = HOUR2 + 5
    for _ in range(3):
        e2.tick()
    assert len(_all_stall_rows(e2)) == 1 and len(_events(fe.REVIEW_EVENT)) == 1


def test_lost_status_alerts_after_the_unknown_streak_once(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    monkeypatch.setattr(a, "poll", lambda handle: Observation(UNKNOWN, {"reason": "boom"}))
    for _ in range(fe.UNKNOWN_POLL_LIMIT - 1):
        e.tick()
    assert _stall_reviews(e) == []
    for _ in range(5):
        e.tick()
    (rv,) = _stall_reviews(e)
    assert rv["payload"]["stalled"] == "agent_status_unknown"
    assert rv["payload"]["unknown_polls"] == fe.UNKNOWN_POLL_LIMIT


def test_a_readable_status_resets_the_unknown_streak(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    real = a.poll
    n = {"i": 0}

    def flaky(handle):
        n["i"] += 1
        return real(handle) if n["i"] % 10 == 0 else Observation(UNKNOWN, {})
    monkeypatch.setattr(a, "poll", flaky)
    for _ in range(3 * fe.UNKNOWN_POLL_LIMIT):
        e.tick()
    assert _stall_reviews(e) == []


# ── late completion ──────────────────────────────────────────────────────────
def test_a_late_result_after_the_alert_is_verified_not_resent(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    assert len(_stall_reviews(e)) == 1
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE
               and _state(s, "mul")["state"] == tc.WORKING, timeout=30)
    assert _state(st, "add")["attempt"] == 1
    assert [x["task_id"] for x in h.agent_log(log)] == ["tinycalc:add", "tinycalc:mul"]


def test_answering_a_stall_after_the_late_result_is_stale(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    e.tick()                                             # verified → review requested
    assert _state(e.status(), "add")["state"] == tc.VERIFYING
    _answer(e, "answer")
    e.tick()
    assert [x["task_id"] for x in h.agent_log(log)] == ["tinycalc:add"]   # no resend


def test_a_late_result_while_the_resend_is_fenced_wins(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    monkeypatch.setattr(a, "quiescent", lambda handle: False)   # agent looks busy
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "answer")
    for _ in range(3):
        e.tick()
    assert len(h.agent_log(log)) == 1                    # fenced: nothing resent
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 1
    assert "tinycalc:add" not in [x["task_id"] for x in h.agent_log(log)][1:]


# ── the resend ───────────────────────────────────────────────────────────────
def test_a_resend_waits_until_the_agent_is_provably_idle(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    quiet = {"v": False}
    monkeypatch.setattr(a, "quiescent", lambda handle: quiet["v"])
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "answer")
    for _ in range(4):
        e.tick()
    assert len(h.agent_log(log)) == 1                    # never two attempts at once
    quiet["v"] = None                                    # status lost: still fenced
    for _ in range(2):
        e.tick()
    assert len(h.agent_log(log)) == 1
    quiet["v"] = True
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 2
    log_ = h.agent_log(log)
    assert [(x["task_id"], x["attempt"]) for x in log_][:2] == [("tinycalc:add", 1), ("tinycalc:add", 2)]
    assert "no result and was stopped" in log_[1]["feedback"]


def test_a_duplicate_decision_resends_once(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    (rv,) = _stall_reviews(e)
    fe.ack_review(rv["review_id"], by="stub-gpt")
    first = fe.submit_decision(rv["review_id"], "answer", key="dup", by="stub-gpt", feedback="go")
    again = fe.submit_decision(rv["review_id"], "answer", key="dup", by="stub-gpt", feedback="go")
    assert first["ok"] and again["ok"]
    other = fe.submit_decision(rv["review_id"], "cancel", key="other", by="stub-gpt", feedback="x")
    assert not other["ok"] and other["reason"] == "review_already_decided"
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
          timeout=30)
    assert [x["attempt"] for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"] == [1, 2]


def test_stalls_are_bounded_by_max_attempts(fc):
    e, a, log, root, clock = _hung_add(fc, {"add": ["hang"]}, max_attempts=2)
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "answer", key="a1")
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 2, 15)
    clock.offset += HOUR2 + 1                            # attempt 2 stalls too
    e.tick()
    (rv,) = _stall_reviews(e)
    assert rv["attempt"] == 2
    _answer(e, "answer", key="a2")
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.BLOCKED      # limit reached → owner gate
    assert _state(st, "add")["last_reason"].startswith("max_attempts_2:stalled")
    assert len(h.agent_log(log)) == 2


# ── cancel ───────────────────────────────────────────────────────────────────
def test_a_cancel_holds_the_next_task_until_the_attempt_is_over(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    quiet = {"v": False}
    monkeypatch.setattr(a, "quiescent", lambda handle: quiet["v"])
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "cancel")
    for _ in range(4):
        e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.WORKING      # still in flight: holds the agent
    assert len(h.agent_log(log)) == 1                    # mul not sent beside it
    quiet["v"] = True
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.CANCELLED


# ── a late "blocked" result after the alert ──────────────────────────────────
def test_a_late_question_reaches_the_tech_lead_through_the_same_review(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    (rv,) = _stall_reviews(e)
    path = os.path.join(root, ".fullcycle", "results", "add-1.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"task_id": "tinycalc:add", "attempt": 1, "status": "blocked",
                   "summary": "stuck", "changed_files": [], "question": "ints or floats?"}, fh)
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.BLOCKED
    (pending,) = [r for r in fe.pending_reviews(e.project_id) if r["kind"] == "blocked"]
    assert pending["review_id"] == rv["review_id"]
    assert pending["payload"]["question"] == "ints or floats?"
    assert not pending["payload"].get("stalled")
    qs = [json.loads(p).get("question") for _, _, p, _ in _events(fe.REVIEW_EVENT)]
    assert qs[-1] == "ints or floats?"
    _answer_plain = fe.submit_decision(rv["review_id"], "answer", key="q", by="stub-gpt",
                                       feedback="ints")
    assert _answer_plain["ok"]
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
          timeout=30)


# ── the live adapter's idle evidence (agent_control.classify_state) ─────────────
@pytest.mark.parametrize("state,expected", [
    ("waiting_input", True), ("dead", True), ("stale", True),
    ("working", False), ("shell_running", False), ("waiting_owner", False),
    ("something_new", None)])
def test_live_adapter_quiescent_maps_agent_states(state, expected):
    from core.fullcycle.tmux_adapter import AgentControlAdapter as TmuxAdapter

    class AC:
        @staticmethod
        def agent_status(target, actor):
            return {"state": state}
    assert TmuxAdapter("s:0.0", ac=AC).quiescent({}) is expected


def test_live_adapter_quiescent_is_unknown_when_status_fails():
    from core.fullcycle.tmux_adapter import AgentControlAdapter as TmuxAdapter

    class AC:
        @staticmethod
        def agent_status(target, actor):
            raise RuntimeError("tmux gone")
    assert TmuxAdapter("s:0.0", ac=AC).quiescent({}) is None


def test_the_base_adapter_never_claims_idle():
    from core.fullcycle.executor import ExecutorAdapter
    assert ExecutorAdapter.quiescent(None, {}) is None
