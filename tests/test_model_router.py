"""Tests for core/model_router.py (task 209) — the standing cost-aware model
routing policy. Not to be confused with core/model_routing.py (Night Shift
budget policy), which this suite does not touch."""
from __future__ import annotations

import sqlite3

import pytest

from core.model_router import (
    SONNET, OPUS, FABLE, MODEL_IDS, PARTITION, ESCALATION_CATEGORIES,
    RouterError, route, record_outcome, effectiveness, policy,
)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "rt.db"))
    yield c
    c.close()


def _escalation(model, **overrides):
    """A valid escalation_reason for `model` (opus/fable) — task 213."""
    from core.model_router import ESCALATION_CATEGORIES
    reason = {
        "category": ESCALATION_CATEGORIES[model][0],
        "evidence": "sonnet attempt failed twice on this exact unit; see decision 1,2",
        "expected_benefit": "resolves the blocking failure without another round-trip",
    }
    reason.update(overrides)
    return reason


# ── partition coverage ───────────────────────────────────────────────────────

def test_every_class_maps_to_its_model(conn):
    for model, classes in PARTITION.items():
        for cls in classes:
            kw = ({"escalation_reason": _escalation(model), "context_pack": "pack"}
                  if model in (OPUS, FABLE) else {})
            out = route(cls, conn=conn, **kw)
            assert out["model"] == model, f"{cls} expected {model}, got {out['model']}"
            assert out["model_id"] == MODEL_IDS[model]


def test_unknown_class_defaults_to_sonnet_non_strict(conn):
    out = route("some_made_up_class", conn=conn)
    assert out["model"] == SONNET
    assert "unknown_class_defaults_to_sonnet" in out["reason"]


def test_unknown_class_strict_refuses(conn):
    with pytest.raises(RouterError, match="unknown task_class"):
        route("some_made_up_class", strict=True, conn=conn)


# ── risk floors ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("risk", ["money", "security", "high"])
def test_risk_floors_sonnet_class_to_opus(conn, risk):
    out = route("tests", risk=risk, conn=conn,
               escalation_reason=_escalation(OPUS), context_pack="pack")
    assert out["model"] == OPUS
    assert "floors to opus" in out["reason"]


def test_risk_floor_never_lowers_fable(conn):
    # a class already at fable stays at fable even with a risk floor
    out = route("hardest_unresolved_bug", risk="security", conn=conn,
               escalation_reason=_escalation(FABLE), context_pack="pack")
    assert out["model"] == FABLE


def test_normal_and_low_risk_do_not_floor(conn):
    assert route("tests", risk="normal", conn=conn)["model"] == SONNET
    assert route("tests", risk="low", conn=conn)["model"] == SONNET


def test_invalid_risk_refused(conn):
    with pytest.raises(RouterError, match="unknown risk"):
        route("tests", risk="yolo", conn=conn)


# ── escalation ladder ─────────────────────────────────────────────────────────

def test_sonnet_failure_escalates_to_opus(conn):
    out = route("tests", prior_attempts=[{"model": SONNET, "outcome": "failure"}], conn=conn,
               escalation_reason=_escalation(OPUS), context_pack="pack")
    assert out["model"] == OPUS
    assert "sonnet_insufficient" in out["reason"]


def test_sonnet_uncertain_escalates_to_opus(conn):
    out = route("tests", prior_attempts=[{"model": SONNET, "outcome": "uncertain"}], conn=conn,
               escalation_reason=_escalation(OPUS), context_pack="pack")
    assert out["model"] == OPUS


def test_opus_failure_escalates_to_fable(conn):
    out = route("architecture",
                prior_attempts=[{"model": OPUS, "outcome": "failure"}], conn=conn,
                escalation_reason=_escalation(FABLE), context_pack="pack")
    assert out["model"] == FABLE
    assert "escalate_to_fable" in out["reason"]


def test_sonnet_opus_disagreement_escalates_to_fable(conn):
    out = route("tests", prior_attempts=[
        {"model": SONNET, "outcome": "disagreement"},
        {"model": OPUS, "outcome": "disagreement"},
    ], conn=conn, escalation_reason=_escalation(FABLE), context_pack="pack")
    assert out["model"] == FABLE


def test_invalid_prior_attempt_model_refused(conn):
    with pytest.raises(RouterError, match="invalid prior_attempts model"):
        route("tests", prior_attempts=[{"model": "gpt5", "outcome": "failure"}], conn=conn)


def test_invalid_prior_attempt_outcome_refused(conn):
    with pytest.raises(RouterError, match="invalid prior_attempts outcome"):
        route("tests", prior_attempts=[{"model": SONNET, "outcome": "vibes"}], conn=conn)


# ── de-escalation for clear findings ─────────────────────────────────────────

def test_clear_finding_deescalates_to_sonnet_after_fable_attempts(conn):
    out = route("clear_finding_implementation", prior_attempts=[
        {"model": OPUS, "outcome": "failure"},
    ], conn=conn)
    assert out["model"] == SONNET
    assert "fable findings implement at sonnet unless high-risk" in out["reason"]


def test_clear_finding_held_at_opus_when_risk_money(conn):
    out = route("clear_finding_implementation", risk="money", prior_attempts=[
        {"model": OPUS, "outcome": "failure"},
    ], conn=conn, escalation_reason=_escalation(OPUS), context_pack="pack")
    assert out["model"] == OPUS


# ── context-pack economics ───────────────────────────────────────────────────

def test_opus_requires_context_pack_and_flags_missing(conn):
    out = route("architecture", conn=conn)
    assert out["requires_context_pack"] is True
    assert out["context_pack_missing"] is True
    assert "generate a compact context pack with sonnet first" in out["reason"]


def test_fable_requires_context_pack_and_flags_missing(conn):
    out = route("hardest_unresolved_bug", conn=conn)
    assert out["requires_context_pack"] is True
    assert out["context_pack_missing"] is True


def test_context_pack_provided_clears_missing_flag(conn):
    out = route("architecture", context_pack="compact summary here", conn=conn)
    assert out["requires_context_pack"] is True
    assert out["context_pack_missing"] is False


def test_sonnet_never_requires_context_pack(conn):
    out = route("tests", conn=conn)
    assert out["requires_context_pack"] is False
    assert out["context_pack_missing"] is False


# ── durability ────────────────────────────────────────────────────────────────

def test_decision_and_outcome_are_durable_rows(conn):
    out = route("tests", task_ref="task-1", conn=conn)
    did = out["decision_id"]
    row = conn.execute("SELECT task_ref, model FROM router_decision WHERE id=?",
                       (did,)).fetchone()
    assert row == ("task-1", SONNET)

    rec = record_outcome(did, outcome="success", tokens_in=100, tokens_out=50,
                         usd=0.02, retries=1, conn=conn)
    assert rec["decision_id"] == did
    orow = conn.execute("SELECT decision_id, outcome, tokens_in FROM router_outcome "
                        "WHERE id=?", (rec["id"],)).fetchone()
    assert orow == (did, "success", 100)

    # multiple outcomes per decision allowed (retries)
    record_outcome(did, outcome="retry", conn=conn)
    n = conn.execute("SELECT COUNT(*) FROM router_outcome WHERE decision_id=?",
                     (did,)).fetchone()[0]
    assert n == 2


def test_unknown_decision_id_refused(conn):
    with pytest.raises(RouterError, match="unknown decision_id"):
        record_outcome(999999, outcome="success", conn=conn)


def test_bad_outcome_refused(conn):
    out = route("tests", conn=conn)
    with pytest.raises(RouterError, match="unknown outcome"):
        record_outcome(out["decision_id"], outcome="vibes", conn=conn)


# ── effectiveness / policy ───────────────────────────────────────────────────

def test_effectiveness_aggregates_per_model_and_class(conn):
    d1 = route("tests", conn=conn)["decision_id"]
    d2 = route("tests", conn=conn)["decision_id"]
    d3 = route("architecture", conn=conn,
              escalation_reason=_escalation(OPUS), context_pack="pack")["decision_id"]

    record_outcome(d1, outcome="success", tokens_in=10, tokens_out=5, usd=0.01, conn=conn)
    record_outcome(d2, outcome="failure", tokens_in=20, tokens_out=10, usd=0.02, conn=conn)
    record_outcome(d3, outcome="success", tokens_in=100, tokens_out=50, usd=0.5, conn=conn)

    eff = effectiveness(days=30, conn=conn)
    pairs = {(p["model"], p["task_class"]): p for p in eff["pairs"]}

    st = pairs[(SONNET, "tests")]
    assert st["attempts"] == 2
    assert st["outcomes"] == 2
    assert st["successes"] == 1
    assert st["failures"] == 1
    assert st["success_rate"] == 0.5
    assert st["tokens_in"] == 30
    assert st["tokens_out"] == 15
    assert round(st["usd"], 2) == 0.03

    at = pairs[(OPUS, "architecture")]
    assert at["attempts"] == 1
    assert at["success_rate"] == 1.0

    assert eff["by_model"][SONNET]["attempts"] == 2
    assert eff["by_model"][SONNET]["success_rate"] == 0.5
    assert eff["by_model"][OPUS]["attempts"] == 1


def test_effectiveness_success_rate_none_without_outcomes(conn):
    route("tests", conn=conn)
    eff = effectiveness(days=30, conn=conn)
    pair = next(p for p in eff["pairs"] if p["model"] == SONNET and p["task_class"] == "tests")
    assert pair["success_rate"] is None
    assert pair["outcomes"] == 0


def test_policy_returns_full_partition():
    p = policy()
    assert p["partition"] == PARTITION
    assert p["model_ids"] == MODEL_IDS
    assert isinstance(p["ladder"], list) and len(p["ladder"]) >= 1


def test_clear_finding_does_not_deescalate_past_its_own_sonnet_failure(conn):
    """A sonnet failure on the unit itself means the finding was NOT clear —
    routing it back to sonnet would loop. The ladder wins."""
    from core import model_router as mr
    r = mr.route("clear_finding_implementation",
                 prior_attempts=[{"model": "sonnet", "outcome": "failure"}],
                 conn=conn, escalation_reason=_escalation(OPUS), context_pack="pack")
    assert r["model"] == "opus"
    r2 = mr.route("clear_finding_implementation",
                  prior_attempts=[{"model": "sonnet", "outcome": "failure"},
                                  {"model": "opus", "outcome": "uncertain"}],
                  conn=conn, escalation_reason=_escalation(FABLE), context_pack="pack")
    assert r2["model"] == "fable"
    # a sonnet SUCCESS on a different aspect does not block de-escalation
    r3 = mr.route("clear_finding_implementation",
                  prior_attempts=[{"model": "fable", "outcome": "success"}],
                  conn=conn)
    assert r3["model"] == "sonnet"


# ── task 213: HARD escalation gate ───────────────────────────────────────────
# Opus/Fable eligibility from the partition/risk-floor/ladder above is
# necessary but not sufficient — dispatch also needs a valid, structured
# escalation_reason (+ context_pack). Missing/invalid on ANY path (base
# class, risk floor, escalation ladder) de-escalates to sonnet; it never
# raises, so routine dispatch is never blocked, just never expensive.

def test_no_escalation_reason_denies_opus_class(conn):
    out = route("architecture", conn=conn)
    assert out["model"] == SONNET
    assert out["requested_model"] == OPUS
    assert out["escalation_valid"] is False
    assert "hard gate de-escalates to sonnet (requested opus)" in out["reason"]


def test_no_escalation_reason_denies_fable_class(conn):
    out = route("hardest_unresolved_bug", conn=conn)
    assert out["model"] == SONNET
    assert out["requested_model"] == FABLE
    assert out["escalation_valid"] is False


def test_risk_floor_without_escalation_reason_still_lands_on_sonnet(conn):
    """A money/security/high risk floor is real, but it does not itself
    substitute for a recorded justification — never silently run the
    expensive tier just because the floor fired."""
    out = route("tests", risk="money", conn=conn)
    assert out["model"] == SONNET
    assert out["requested_model"] == OPUS
    assert "floors to opus" in out["reason"]
    assert "hard gate de-escalates to sonnet" in out["reason"]


def test_escalation_ladder_without_escalation_reason_still_lands_on_sonnet(conn):
    out = route("tests", prior_attempts=[{"model": SONNET, "outcome": "failure"}], conn=conn)
    assert out["model"] == SONNET
    assert out["requested_model"] == OPUS


def test_wrong_category_for_tier_denied(conn):
    """A fable-only category can't justify opus, and vice versa."""
    out = route("architecture", conn=conn,
               escalation_reason=_escalation(FABLE), context_pack="pack")
    assert out["model"] == SONNET
    assert out["escalation_valid"] is False


def test_empty_evidence_denied(conn):
    out = route("architecture", conn=conn, context_pack="pack",
               escalation_reason=_escalation(OPUS, evidence=""))
    assert out["model"] == SONNET


def test_empty_expected_benefit_denied(conn):
    out = route("architecture", conn=conn, context_pack="pack",
               escalation_reason=_escalation(OPUS, expected_benefit="  "))
    assert out["model"] == SONNET


def test_missing_context_pack_denies_opus_even_with_valid_reason(conn):
    out = route("architecture", conn=conn, escalation_reason=_escalation(OPUS))
    assert out["model"] == SONNET
    assert out["escalation_valid"] is False


def test_valid_escalation_reason_grants_opus(conn):
    out = route("architecture", conn=conn,
               escalation_reason=_escalation(OPUS), context_pack="delta pack ref")
    assert out["model"] == OPUS
    assert out["requested_model"] == OPUS
    assert out["escalation_valid"] is True
    assert "hard gate" not in out["reason"]


def test_valid_escalation_reason_grants_fable(conn):
    out = route("hardest_unresolved_bug", conn=conn,
               escalation_reason=_escalation(FABLE), context_pack="delta pack ref")
    assert out["model"] == FABLE
    assert out["escalation_valid"] is True


def test_concrete_fix_deescalates_to_sonnet_even_with_opus_eligible_history(conn):
    """clear_finding_implementation (a 'concrete post-audit fix') must land on
    sonnet by default — the class-level de-escalation already does this, and
    the hard gate does not reopen the door without its own justification."""
    out = route("clear_finding_implementation",
               prior_attempts=[{"model": OPUS, "outcome": "failure"}], conn=conn)
    assert out["model"] == SONNET


def test_escalation_gate_never_raises(conn):
    """Fail-toward-cheap, not fail-toward-blocked: a garbage escalation_reason
    is a denial, not an exception."""
    out = route("architecture", conn=conn, escalation_reason="not a dict",
               context_pack="pack")
    assert out["model"] == SONNET


def test_escalation_audit_fields_persisted(conn):
    out = route("architecture", conn=conn,
               escalation_reason=_escalation(OPUS), context_pack="delta pack ref")
    row = conn.execute(
        "SELECT requested_model, escalation_category, escalation_evidence,"
        " escalation_expected_benefit, escalation_valid FROM router_decision WHERE id=?",
        (out["decision_id"],)).fetchone()
    assert row[0] == OPUS
    assert row[1] == ESCALATION_CATEGORIES[OPUS][0]
    assert row[2] and row[3]
    assert row[4] == 1


def test_sonnet_class_never_needs_escalation_reason(conn):
    """Sonnet-tier work is never gated — only opus/fable requests are."""
    out = route("routine_implementation", conn=conn)
    assert out["model"] == SONNET
    assert out["requested_model"] == SONNET
    assert out["escalation_valid"] is True
