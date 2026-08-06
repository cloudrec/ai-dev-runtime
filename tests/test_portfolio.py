"""Portfolio brain: record and rank ideas, dispatch nothing."""
from __future__ import annotations

import pytest

from core import portfolio as pf


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


# ── the safety property: recording is not doing ────────────────────────────
def test_proposing_an_idea_dispatches_nothing():
    """Recording and ranking is safe; acting needs the owner's permitted-work set."""
    from core import os_task_queue as q
    out = pf.propose(pf.KIND_OPPORTUNITY, project="seo", title="bundle audits as a service",
                     expected_revenue=500, confidence=0.4, effort=3, risk=2)
    assert out["dispatched"] is False and out["state"] == pf.STATE_PROPOSED
    assert q._list("1=1") == [], "no task may be created by proposing an idea"


def test_nothing_approves_its_own_idea():
    """Approval is an owner act. The module records `decided_by` so that stays checkable."""
    item = pf.propose(pf.KIND_EXPERIMENT, project="p", title="try a landing page")
    assert item["state"] == pf.STATE_PROPOSED
    r = pf.decide(item["id"], pf.STATE_APPROVED, by="owner", note="worth one week")
    assert r["decided_by"] == "owner"
    assert pf.ranked(state=pf.STATE_APPROVED)[0]["decision_note"] == "worth one week"


def test_an_invalid_state_is_refused():
    item = pf.propose(pf.KIND_EXPERIMENT, project="p", title="x")
    assert pf.decide(item["id"], "shipped_to_prod")["ok"] is False


# ── scoring is explainable and cannot be gamed to infinity ─────────────────
def test_higher_revenue_and_confidence_rank_higher():
    a = pf.score(expected_revenue=1000, confidence=0.9, effort=2, risk=1)
    b = pf.score(expected_revenue=1000, confidence=0.2, effort=2, risk=1)
    c = pf.score(expected_revenue=100, confidence=0.9, effort=2, risk=1)
    assert a > b and a > c


def test_effort_and_risk_reduce_the_score():
    cheap = pf.score(expected_revenue=100, confidence=1, effort=1, risk=1)
    costly = pf.score(expected_revenue=100, confidence=1, effort=10, risk=1)
    risky = pf.score(expected_revenue=100, confidence=1, effort=1, risk=10)
    assert cheap > costly and cheap > risky


def test_zero_effort_cannot_produce_an_infinite_score():
    """A single optimistic zero would otherwise park a pet idea at the top forever."""
    s = pf.score(expected_revenue=100, confidence=1, effort=0, risk=0)
    assert s == pytest.approx(100 / (0.1 * 0.1), rel=1e-6)


def test_confidence_is_clamped_to_a_probability():
    assert pf.score(expected_revenue=10, confidence=5, effort=1, risk=1) == \
           pf.score(expected_revenue=10, confidence=1, effort=1, risk=1)


def test_reuse_of_existing_assets_is_rewarded():
    plain = pf.score(expected_revenue=100, confidence=1, effort=2, risk=1, reuse=1)
    reuses = pf.score(expected_revenue=100, confidence=1, effort=2, risk=1, reuse=2)
    assert reuses > plain


def test_the_inputs_are_stored_beside_the_score():
    """A ranking nobody can argue with is a ranking nobody can trust."""
    pf.propose(pf.KIND_OPPORTUNITY, project="p", title="idea", expected_revenue=200,
               confidence=0.6, effort=2, risk=1.5, reuse=1.2, evidence="from the audit log")
    row = pf.ranked()[0]
    assert row["expected_revenue"] == 200 and row["confidence"] == 0.6
    assert row["effort"] == 2 and row["risk"] == 1.5 and row["reuse"] == 1.2
    assert row["evidence"] == "from the audit log"


def test_ranking_is_by_score_then_oldest_first():
    pf.propose(pf.KIND_OPPORTUNITY, project="p", title="low", expected_revenue=10,
               confidence=1, effort=1, risk=1)
    pf.propose(pf.KIND_OPPORTUNITY, project="p", title="high", expected_revenue=900,
               confidence=1, effort=1, risk=1)
    assert [r["title"] for r in pf.ranked()] == ["high", "low"]


# ── idle capacity: managed work always wins ────────────────────────────────
def test_the_portfolio_yields_to_managed_project_work():
    from core import os_task_queue as q
    t = q.enqueue("cp-canary:0.0", "real project work")
    q.set_state(t["id"], q.WORKING)
    cap = pf.has_idle_capacity()
    assert cap["idle"] is False and "in flight" in cap["reason"]


def test_idle_capacity_exists_when_nothing_is_running():
    assert pf.has_idle_capacity()["idle"] is True


def test_open_experiments_are_capped(monkeypatch):
    monkeypatch.setattr(pf, "MAX_OPEN_EXPERIMENTS", 1)
    item = pf.propose(pf.KIND_EXPERIMENT, project="p", title="one")
    pf.decide(item["id"], pf.STATE_APPROVED)
    cap = pf.has_idle_capacity()
    assert cap["idle"] is False and "experiment limit" in cap["reason"]


# ── durable across restarts, no chat context needed ────────────────────────
def test_goals_and_items_survive_a_reconnect():
    pf.set_goal("mess", "ship the redesign without regressions")
    item = pf.propose(pf.KIND_PROMOTION, project="mess", title="write launch notes")
    assert pf.goals()["mess"] == "ship the redesign without regressions"
    assert any(r["id"] == item["id"] for r in pf.ranked())
