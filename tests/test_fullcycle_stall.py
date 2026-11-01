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
    (row,) = _all_stall_rows(e)                           # the alert is no longer open...
    r = fe.submit_decision(row[0], "answer", key="late", by="stub-gpt", feedback="go")
    assert not r["ok"] and r["reason"] == "review_superseded"   # ...so it cannot be decided
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


# ── F1: a late result found by the fence after the attempt limit is verified ────────
def test_f1_late_result_after_limit_and_owner_resume_is_verified(fc):
    from core.control_plane import api as cp
    e, a, log, root, clock = _hung_add(fc, {"add": ["hang"]}, max_attempts=1)
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "answer")                                 # at the limit → owner gate
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and t["last_reason"].startswith("max_attempts_1:stalled")
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    cp.answer_gate(t["gate_id"], "resume")
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 1             # the stalled attempt's own result
    assert [x["task_id"] for x in h.agent_log(log)].count("tinycalc:add") == 1   # never resent
    hist = [x["to"] for x in tc.history(_task(e, "add")["contract_id"])]
    assert hist[-4:] == [tc.WORKING, tc.AGENT_DONE, tc.VERIFYING, tc.VERIFIED_DONE]



# ── a stall alert is retired by the late result it was about ─────────────────────
def _stall_status(e):
    (row,) = _all_stall_rows(e)
    return e.conn.execute("SELECT status, event_id, decided_by FROM fc_review WHERE id=?",
                          (row[0],)).fetchone()


def test_a_late_done_result_supersedes_the_pending_stall_alert(fc):
    from core import wake_bridge as wb
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    status, eid, _ = _stall_status(e)
    assert status == "pending" and eid
    assert not wb._fullcycle_review_closed(e.conn, eid)      # its doorbell is live
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    e.tick()
    status, eid2, by = _stall_status(e)
    assert status == "superseded" and by == "engine" and eid2 == eid
    assert wb._fullcycle_review_closed(e.conn, eid)          # an undelivered wake retires
    kinds = [(r["kind"], bool(r["payload"].get("stalled"))) for r in fe.pending_reviews(e.project_id)]
    assert kinds == [("review", False)]                     # only the real review is open


def test_superseding_is_idempotent_and_emits_nothing_more(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    e.tick()
    n_events = len(_events())
    for _ in range(4):
        e.tick()
    assert _stall_status(e)[0] == "superseded"
    assert len(_events()) == n_events and len(_all_stall_rows(e)) == 1


def test_an_unresolved_stall_stays_pending(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    for _ in range(5):
        e.tick()
    assert _stall_status(e)[0] == "pending"                  # no result: still alerting


def test_a_decided_stall_is_never_superseded(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    _answer(e, "answer")
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    for _ in range(3):
        e.tick()
    assert _stall_status(e)[0] == "decided"                  # the tech lead's decision stands


def test_a_late_question_keeps_the_alert_slot_pending(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    path = os.path.join(root, ".fullcycle", "results", "add-1.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"task_id": "tinycalc:add", "attempt": 1, "status": "blocked",
                   "summary": "s", "changed_files": [], "question": "q?"}, fh)
    e.tick()
    rows = e.conn.execute("SELECT status FROM fc_review WHERE kind='blocked'").fetchall()
    assert rows == [("pending",)]                           # now a real question: stays open


def test_a_late_result_of_an_older_attempt_supersedes_nothing(fc):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    _write_late_result(root, "add", 7, h.SOLUTIONS["add"])   # not this attempt's result
    for _ in range(3):
        e.tick()
    assert _stall_status(e)[0] == "pending"


# ── a crash after a stall alert: the crash path escalates; the alert is retired ────
def _crash(monkeypatch, a):
    from core.fullcycle.executor import CRASHED
    monkeypatch.setattr(a, "poll", lambda handle: Observation(CRASHED, {"pane": "dead"}))


def _stall_row(e):
    (row,) = _all_stall_rows(e)
    return e.conn.execute("SELECT id, status, decision_body FROM fc_review WHERE id=?",
                          (row[0],)).fetchone()


def test_crash_after_alert_unowned_goes_to_owner_and_retires_the_alert(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    rid = _stall_row(e)[0]
    monkeypatch.setattr(type(a), "owns_agent", False)
    _crash(monkeypatch, a)
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and t["last_reason"] == "agent_crashed_not_owned" and t["gate_id"]
    assert len(_events(fe.OWNER_EVENT)) == 1                 # correct crash escalation
    _, status, body = _stall_row(e)
    assert status == "superseded" and json.loads(body)["reason"] == "crashed"
    # the tech lead's answer to the old alert cannot bypass the owner gate
    r = fe.submit_decision(rid, "answer", key="late", by="stub-gpt", feedback="retry")
    assert not r["ok"] and r["reason"] == "review_superseded"
    for _ in range(3):
        e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and t["gate_id"] and len(h.agent_log(log)) == 1


def test_a_stall_answer_never_bypasses_a_crash_owner_gate(fc, monkeypatch):
    # decided by the tech lead in the same instant the crash is observed: the engine
    # sees the crash first only if the decision is not yet recorded — force the order
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    rid = _stall_row(e)[0]
    monkeypatch.setattr(type(a), "owns_agent", False)
    t = _task(e, "add")
    e._block_for_owner(t, "agent_crashed_not_owned")          # crash path already ran
    fe.ack_review(rid, by="stub-gpt")
    assert fe.submit_decision(rid, "answer", key="race", by="stub-gpt", feedback="go")["ok"]
    for _ in range(3):
        e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and t["gate_id"]          # still the owner's
    assert len(h.agent_log(log)) == 1                          # nothing resent


def test_crash_after_alert_owned_recovers_once_and_retires_the_alert(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc)
    clock.offset = HOUR2 + 1
    e.tick()
    real_poll = type(a).poll
    calls = {"n": 0}
    from core.fullcycle.executor import CRASHED

    def crash_once(handle):
        calls["n"] += 1
        return Observation(CRASHED, {}) if calls["n"] == 1 else real_poll(a, handle)
    monkeypatch.setattr(a, "poll", crash_once)
    monkeypatch.setattr(a, "restart", lambda handle: {"ok": True, "how": "test"})
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 2
    assert _stall_row(e)[1] == "superseded"
    assert [x["attempt"] for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"] == [1, 2]


def test_crash_after_alert_at_the_limit_goes_to_owner(fc, monkeypatch):
    e, a, log, root, clock = _hung_add(fc, {"add": ["hang"]}, max_attempts=1)
    clock.offset = HOUR2 + 1
    e.tick()
    _crash(monkeypatch, a)
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and t["last_reason"] == "max_attempts_1_after_crash"
    assert _stall_row(e)[1] == "superseded"
    assert len(h.agent_log(log)) == 1
