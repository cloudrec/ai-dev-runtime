"""D2 — an agent that runs out of context; D2b — no rotation under a running attempt.

D2: the agent ends its attempt with `status: context_exhausted` and a compact handoff.
The task goes to the tech lead as a "blocked" review carrying the (validated) handoff.
"answer" continues in a CLEAN session — only after a safe stop; an adapter that owns its
agent starts that session itself, a live agent's clean session goes through the owner
gate `clean_session_required` (the engine holds no session-lifecycle grant). Bounded by
max_attempts. A malformed handoff is reported, never dropped or silently altered.

D2b: core.agent_context_budget never writes a handoff into, or /clears, a pane whose Full
Cycle attempt may be running (fail closed).

Mocked: the agent (fixture script), the reviewer (driven by hand). Real: engine, tmux,
git, contracts, the context-budget evaluate() path up to its first side effect.
"""
from __future__ import annotations

import json
import os

import pytest

from core import task_contract as tc
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from core.fullcycle.tmux_adapter import HANDOFF_MAX, clean_handoff, parse_result
from tests.test_fullcycle_e2e import _events, _state, fc, h  # noqa: F401


def _blocked(e):
    return [r for r in fe.pending_reviews(e.project_id) if r["kind"] == "blocked"]


def _answer(e, choice, key="k1", feedback="carry on from the handoff"):
    (rv,) = _blocked(e)
    fe.ack_review(rv["review_id"], by="stub-gpt")
    return fe.submit_decision(rv["review_id"], choice, key=key, by="stub-gpt", feedback=feedback)


def _exhausted(fc, behaviours=None, **kw):
    e, a, log, root = fc(behaviours or {"add": ["exhausted", "correct"]}, **kw)
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.BLOCKED, 15)
    return e, a, log, root


# ── handoff validation (pure) ────────────────────────────────────────────────
@pytest.mark.parametrize("raw,text,problem", [
    ("next: write add.py", "next: write add.py", ""),
    (None, "", "handoff_missing"),
    (42, "", "handoff_not_text"),
    (["a"], "", "handoff_not_text"),
    ("   \n\t ", "", "handoff_empty"),
    ("ok\x00\x1b[31mred", "ok[31mred", "handoff_control_chars_removed"),
    ("line1\nline2\tx", "line1\nline2\tx", ""),
])
def test_clean_handoff(raw, text, problem):
    assert clean_handoff(raw) == (text, problem)


def test_an_over_long_handoff_is_cut_and_says_so():
    text, problem = clean_handoff("x" * (HANDOFF_MAX + 50))
    assert len(text) == HANDOFF_MAX and problem == f"handoff_truncated_to_{HANDOFF_MAX}"


def test_parse_result_accepts_context_exhausted_only_for_its_attempt():
    handle = {"task_id": "p:t", "attempt": 2}
    r = parse_result(json.dumps({"task_id": "p:t", "attempt": 2, "status": "context_exhausted",
                                 "summary": "s", "changed_files": [], "handoff": 7}), handle)
    assert r["status"] == "context_exhausted" and r["handoff_problem"] == "handoff_not_text"
    assert parse_result(json.dumps({"task_id": "p:t", "attempt": 1,
                                    "status": "context_exhausted"}), handle) is None


def _handoff(rv):
    """The handoff is the agent's own text: it travels only as labelled untrusted data."""
    return rv["payload"]["agent_report"]["fields"].get("handoff", "")


# ── the engine path ──────────────────────────────────────────────────────────
def test_exhaustion_goes_to_the_tech_lead_with_the_handoff(fc):
    e, a, log, root = _exhausted(fc)
    (rv,) = _blocked(e)
    assert rv["payload"]["context_exhausted"] is True
    assert _handoff(rv).startswith("HANDOFF add:")
    assert fe.untrusted("HANDOFF", _handoff(rv)) in rv["payload"]["question"]
    assert rv["payload"]["handoff_problem"] == ""
    assert _events(fe.OWNER_EVENT) == []                 # routine: not an owner gate
    for _ in range(3):
        e.tick()
    assert len(_blocked(e)) == 1 and len(h.agent_log(log)) == 1


def test_answer_continues_in_a_clean_session_with_the_handoff(fc, monkeypatch):
    e, a, log, root = _exhausted(fc)
    restarts = []
    real = a.restart
    monkeypatch.setattr(a, "restart", lambda handle: restarts.append(handle["attempt"]) or real(handle))
    _answer(e, "answer")
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 2
    assert restarts[:1] == [1]                           # fresh session for attempt 2
    fb = [x for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"][1]["feedback"]
    assert "HANDOFF add:" in fb and "carry on from the handoff" in fb


def test_no_clean_session_and_no_resend_before_a_safe_stop(fc, monkeypatch):
    e, a, log, root = _exhausted(fc)
    quiet = {"v": None}
    monkeypatch.setattr(a, "quiescent", lambda handle: quiet["v"])
    monkeypatch.setattr(a, "restart", lambda handle: pytest.fail("restart before a safe stop"))
    _answer(e, "answer")
    for _ in range(4):
        e.tick()
    assert len(h.agent_log(log)) == 1
    quiet["v"] = False
    e.tick()
    assert len(h.agent_log(log)) == 1


def test_a_live_agent_gets_its_clean_session_through_the_owner_gate(fc, monkeypatch):
    e, a, log, root = _exhausted(fc)
    monkeypatch.setattr(type(a), "owns_agent", False)
    monkeypatch.setattr(a, "restart", lambda handle: pytest.fail("the engine never restarts a live agent"))
    _answer(e, "answer")
    e.tick()
    t = next(t for t in e._tasks() if t["key"] == "add")
    assert t["state"] == tc.OWNER_DECISION and t["last_reason"] == "clean_session_required"
    (owner_ev,) = _events(fe.OWNER_EVENT)
    assert "HANDOFF add:" in json.loads(owner_ev[2])["handoff"]
    assert len(h.agent_log(log)) == 1
    cp.answer_gate(t["gate_id"], "resume")               # owner: the session is clean
    st = h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    fb = [x for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"][1]["feedback"]
    assert "HANDOFF add:" in fb


def test_a_failed_clean_session_goes_to_the_owner(fc, monkeypatch):
    e, a, log, root = _exhausted(fc)
    monkeypatch.setattr(a, "restart", lambda handle: {"ok": False, "reason": "tmux gone"})
    _answer(e, "answer")
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.BLOCKED
    assert _state(st, "add")["last_reason"] == "clean_session_failed"
    assert len(h.agent_log(log)) == 1


def test_cancel_ends_the_task(fc):
    e, a, log, root = _exhausted(fc)
    _answer(e, "cancel", feedback="")
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.CANCELLED
    assert len(h.agent_log(log)) == 1


def test_repeated_exhaustion_is_bounded(fc):
    e, a, log, root = _exhausted(fc, {"add": ["exhausted"]}, max_attempts=2)
    _answer(e, "answer", key="a1")
    h.wait_for(lambda: e.tick() and len(_blocked(e)) == 1 and _blocked(e)[0]["attempt"] == 2, 20)
    _answer(e, "answer", key="a2")
    e.tick()
    st = e.status()
    assert _state(st, "add")["state"] == tc.BLOCKED
    assert _state(st, "add")["last_reason"] == "max_attempts_2:context_exhausted"
    assert len(h.agent_log(log)) == 2


def test_a_duplicate_decision_continues_once(fc):
    e, a, log, root = _exhausted(fc)
    (rv,) = _blocked(e)
    fe.ack_review(rv["review_id"], by="stub-gpt")
    for _ in range(2):
        assert fe.submit_decision(rv["review_id"], "answer", key="dup", by="stub-gpt",
                                  feedback="go")["ok"]
    h.run(e, h.StubReviewer(), until=lambda s: _state(s, "add")["state"] == tc.VERIFIED_DONE,
          timeout=30)
    assert [x["attempt"] for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"] == [1, 2]


@pytest.mark.parametrize("handoff,problem", [(None, "handoff_missing"), ("", "handoff_empty"),
                                             ({"x": 1}, "handoff_not_text")])
def test_a_malformed_handoff_still_reaches_the_tech_lead(fc, handoff, problem):
    e, a, log, root = fc({"add": ["hang"]})
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 1, 15)
    path = os.path.join(root, ".fullcycle", "results", "add-1.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"task_id": "tinycalc:add", "attempt": 1, "status": "context_exhausted",
            "summary": "s", "changed_files": []}
    if handoff is not None:
        body["handoff"] = handoff
    with open(path, "w") as fh:
        json.dump(body, fh)
    e.tick()
    (rv,) = _blocked(e)
    assert _handoff(rv) == "" and rv["payload"]["handoff_problem"] == problem
    _answer(e, "answer")
    h.run(e, h.StubReviewer(), until=lambda s: len(h.agent_log(log)) == 2, timeout=20)
    assert "(none was provided)" in h.agent_log(log)[1]["feedback"]


def test_exhaustion_after_a_stall_alert_uses_the_same_review(fc):
    e, a, log, root = fc({"add": ["hang"]})
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 1, 15)
    base = e.clock
    e.clock = lambda: base() + fe.ATTEMPT_TIMEOUT + 1
    e.tick()
    (stall,) = _blocked(e)
    assert stall["payload"]["stalled"] == "attempt_timeout"
    path = os.path.join(root, ".fullcycle", "results", "add-1.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"task_id": "tinycalc:add", "attempt": 1, "status": "context_exhausted",
                   "summary": "s", "changed_files": [], "handoff": "H"}, fh)
    e.tick()
    (rv,) = _blocked(e)
    assert rv["review_id"] == stall["review_id"]
    assert rv["payload"]["context_exhausted"] and _handoff(rv) == "H"
    assert rv["payload"]["after_stall"] == "attempt_timeout"


# ── D2b ──────────────────────────────────────────────────────────────────────
def _target(a):
    return a.target


def test_in_flight_predicate_follows_the_attempt(fc):
    e, a, log, root = fc({"add": ["hang"]})
    assert fe.attempt_in_flight_for_agent(_target(a)) is False
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 1, 15)
    assert fe.attempt_in_flight_for_agent(_target(a)) is True
    assert fe.attempt_in_flight_for_agent("some-other-session:0.0") is False


def test_in_flight_predicate_releases_at_a_safe_stop(fc):
    e, a, log, root = _exhausted(fc)
    assert fe.attempt_in_flight_for_agent(_target(a)) is False   # BLOCKED: may rotate


def test_in_flight_predicate_fails_closed(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 1, 15)
    monkeypatch.setattr(tc, "get", lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert fe.attempt_in_flight_for_agent(_target(a)) is True


def test_in_flight_predicate_without_full_cycle_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "empty.db"))
    assert fe.attempt_in_flight_for_agent("any:0.0") is False


@pytest.mark.parametrize("pct_text", ["50% context used", "90% context used"])
def test_context_budget_never_touches_a_pane_with_a_running_attempt(monkeypatch, tmp_path, pct_text):
    from core import agent_context_budget as acb
    monkeypatch.setattr(acb, "fullcycle_holds", lambda target: True)
    monkeypatch.setattr(acb, "_rotation_row", lambda key: {})
    monkeypatch.setattr(acb.ac, "pending_input_text", lambda key, tail="": "")
    for name in ("write_handoff", "_save_rotation"):
        monkeypatch.setattr(acb, name, lambda *a_, **k: pytest.fail(f"{name} under a running attempt"))
    monkeypatch.setattr(acb.ac, "agent_send", lambda *a_, **k: pytest.fail("/clear sent"))
    monkeypatch.setattr(acb.ac, "submit_clear", lambda *a_, **k: pytest.fail("/clear sent"))
    if acb.detect_context_pct(pct_text) is None:
        pytest.skip("context text format not recognised by this build")
    agent = {"target": "fc:0.0", "recent_activity": pct_text, "cwd": str(tmp_path)}
    out = acb.evaluate(agent, {"root": str(tmp_path)}, {"agent_key": "fc:0.0", "state": "idle"},
                       {}, act=True, dispatch=True)
    assert out["notification_state"] == "context_deferred_fullcycle_attempt"
    assert not os.listdir(tmp_path)


def test_context_budget_hold_check_fails_closed(monkeypatch):
    from core import agent_context_budget as acb
    import core.fullcycle.engine as eng
    monkeypatch.setattr(eng, "attempt_in_flight_for_agent",
                        lambda t: (_ for _ in ()).throw(RuntimeError("x")))
    assert acb.fullcycle_holds("x:0.0") is True
