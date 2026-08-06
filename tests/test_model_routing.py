"""Tier policy: the lowest tier that can do the job, and never a silent escalation."""
from __future__ import annotations

import pytest

from core import model_routing as mr


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


# ── tier 0: the rule that saves the most money ─────────────────────────────
@pytest.mark.parametrize("cls", ["poll", "state_reduce", "dedupe", "test", "health"])
def test_routine_work_uses_no_model_at_all(cls):
    r = mr.route(cls)
    assert r["allow"] is True and r["tier"] == mr.TIER_NONE
    assert r["estimated_usd"] == 0.0


def test_liveness_is_never_paid_for(monkeypatch):
    """Tier 0 stays available even with the kill switch on and the budget gone — refusing
    to check health for cost reasons would be its own failure."""
    monkeypatch.setattr(mr, "KILL_SWITCH", True)
    assert mr.route("health")["allow"] is True
    mr.record_spend(project="p", task_class="research", tier=2, model="m",
                    usd=mr.DAILY_BUDGET_USD * 2)
    assert mr.route("poll")["allow"] is True


def test_an_unknown_task_class_defaults_low_not_high():
    r = mr.route("something_nobody_mapped")
    assert r["tier"] == mr.TIER_LOCAL, "an unknown task is not a licence for the big model"


@pytest.mark.parametrize("cls,tier", [
    ("classify", mr.TIER_LOCAL), ("research", mr.TIER_CHEAP),
    ("complex_code", mr.TIER_SONNET), ("architecture", mr.TIER_OPUS)])
def test_each_class_starts_at_its_lowest_safe_tier(cls, tier):
    assert mr.route(cls)["tier"] == tier


# ── escalation must be justified ───────────────────────────────────────────
def test_escalation_without_a_reason_is_refused():
    r = mr.route("research", escalate_to=mr.TIER_OPUS)
    assert r["allow"] is False and r["reason"] == "escalation_requires_recorded_reason"


def test_escalation_with_a_reason_is_allowed_and_recorded():
    r = mr.route("research", escalate_to=mr.TIER_SONNET,
                 escalation_reason="two cheap attempts produced contradictory specs")
    assert r["allow"] is True and r["tier"] == mr.TIER_SONNET
    assert "contradictory specs" in r["reason"]


def test_escalation_never_exceeds_the_top_tier():
    r = mr.route("research", escalate_to=99, escalation_reason="because")
    assert r["tier"] == mr.TIER_OPUS


def test_a_lower_escalate_to_never_downgrades_below_the_class_floor():
    r = mr.route("architecture", escalate_to=mr.TIER_LOCAL, escalation_reason="cheaper")
    assert r["tier"] == mr.TIER_OPUS, "the floor protects correctness, not cost"


# ── budgets, ceilings, loops, kill switch ──────────────────────────────────
def test_a_task_over_its_ceiling_is_refused():
    r = mr.route("research", estimated_usd=mr.PER_TASK_CEILING_USD + 0.01)
    assert r["allow"] is False and "per_task_ceiling_exceeded" in r["reason"]


def test_the_daily_budget_stops_dispatch():
    mr.record_spend(project="p", task_class="research", tier=2, model="m",
                    usd=mr.DAILY_BUDGET_USD - 0.01)
    r = mr.route("research", estimated_usd=0.05)
    assert r["allow"] is False and "daily_budget_exhausted" in r["reason"]


def test_the_kill_switch_stops_every_paid_tier(monkeypatch):
    monkeypatch.setattr(mr, "KILL_SWITCH", True)
    for cls in ("classify", "research", "complex_code", "architecture"):
        assert mr.route(cls)["allow"] is False


def test_identical_work_repeated_is_a_loop_not_progress():
    fp = "same-work-fingerprint"
    for _ in range(mr.LOOP_THRESHOLD):
        mr.record_spend(project="p", task_class="research", tier=2, model="m", usd=0.001,
                        fingerprint=fp)
    r = mr.route("research", fingerprint=fp, estimated_usd=0.001)
    assert r["allow"] is False and r["reason"] == "loop_detected_identical_work_repeated"


def test_different_work_is_not_caught_by_loop_detection():
    for i in range(mr.LOOP_THRESHOLD + 2):
        mr.record_spend(project="p", task_class="research", tier=2, model="m", usd=0.001,
                        fingerprint=f"unique-{i}")
    assert mr.route("research", fingerprint="unique-new", estimated_usd=0.001)["allow"] is True


# ── the acceptance metric ──────────────────────────────────────────────────
def test_cost_is_attributed_by_project_model_and_tier():
    mr.record_spend(project="mess", task_class="research", tier=2, model="cheap-1",
                    usd=0.02, artefact="spec.md")
    mr.record_spend(project="canary", task_class="review", tier=3, model="sonnet-1",
                    usd=0.08, artefact="review.md")
    rep = mr.cost_report()
    assert rep["by_project"] == {"mess": 0.02, "canary": 0.08}
    assert rep["by_model"] == {"cheap-1": 0.02, "sonnet-1": 0.08}
    assert rep["by_tier"] == {2: 0.02, 3: 0.08}
    assert rep["total_usd"] == 0.1


def test_artefacts_per_dollar_is_reported():
    mr.record_spend(project="p", task_class="spec", tier=2, model="m", usd=0.5,
                    artefact="one.md")
    rep = mr.cost_report()
    assert rep["artefacts"] == 1 and rep["artefacts_per_usd"] == 2.0
    assert rep["remaining_usd"] == round(mr.DAILY_BUDGET_USD - 0.5, 6)


def test_spend_with_no_artefact_still_counts_against_the_budget():
    """Money spent that produced nothing is exactly what the report must expose."""
    mr.record_spend(project="p", task_class="research", tier=2, model="m", usd=0.25)
    rep = mr.cost_report()
    assert rep["total_usd"] == 0.25 and rep["artefacts"] == 0
    assert rep["artefacts_per_usd"] == 0.0
