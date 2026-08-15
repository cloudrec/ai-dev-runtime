"""Task Contract (Agent Fabric v1, task OWNER-192): fail-closed shape
validation, fail-closed state machine, evidence-gated verification, immutable
history."""
import pytest

from core import task_contract as tc


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


GOOD = {"goal": "add feature X", "acceptance_criteria": ["tests green", "report written"],
        "scope": ["core/x.py"], "do_not_touch": ["configs/.env"],
        "tests_required": True, "live_check_required": False,
        "push_allowed": False, "deploy_allowed": False,
        "owner_decisions": ["merge to main"], "expected_report": "reports/X.md"}


# ── shape ───────────────────────────────────────────────────────────────────

def test_valid_contract_normalizes_with_defaults():
    out = tc.validate_contract({"goal": "g", "acceptance_criteria": ["a"]})
    assert out["push_allowed"] is False and out["deploy_allowed"] is False
    assert out["tests_required"] is True and out["scope"] == []


def test_missing_required_fields_refused():
    with pytest.raises(tc.ContractError, match="goal"):
        tc.validate_contract({"acceptance_criteria": ["a"]})
    with pytest.raises(tc.ContractError, match="acceptance_criteria"):
        tc.validate_contract({"goal": "g"})


def test_unknown_field_refused_loudly():
    # a typo'd power must fail, never silently grant nothing
    with pytest.raises(tc.ContractError, match="pushallowed"):
        tc.validate_contract({"goal": "g", "acceptance_criteria": ["a"],
                              "pushallowed": True})


def test_wrong_types_refused():
    with pytest.raises(tc.ContractError, match="push_allowed"):
        tc.validate_contract({"goal": "g", "acceptance_criteria": ["a"],
                              "push_allowed": "yes"})


# ── state machine ───────────────────────────────────────────────────────────

def test_lifecycle_happy_path_with_evidence():
    c = tc.create(GOOD, task_id=192, agent_ref="tmux:a:0.0")
    assert c["state"] == tc.CREATED
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.AGENT_DONE, by="agent")
    tc.transition(c["id"], tc.VERIFYING, by="verifier")
    out = tc.transition(c["id"], tc.VERIFIED_DONE, by="verifier",
                        evidence={"tests": {"ok": True, "passed": 1977}})
    assert out["state"] == tc.VERIFIED_DONE
    h = tc.history(c["id"])
    assert [t["to"] for t in h] == [tc.CREATED, tc.WORKING, tc.AGENT_DONE,
                                    tc.VERIFYING, tc.VERIFIED_DONE]
    assert h[-1]["evidence"]["tests"]["passed"] == 1977


def test_agent_done_is_a_claim_not_a_result():
    c = tc.create(GOOD)
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.AGENT_DONE, by="agent")
    # no route from AGENT_DONE straight to VERIFIED_DONE
    with pytest.raises(tc.ContractError, match="illegal transition"):
        tc.transition(c["id"], tc.VERIFIED_DONE, by="agent",
                      evidence={"tests": {"ok": True}})


def test_verified_done_without_evidence_is_unrecordable():
    c = tc.create(GOOD)
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.AGENT_DONE, by="agent")
    tc.transition(c["id"], tc.VERIFYING, by="verifier")
    with pytest.raises(tc.ContractError, match="evidence"):
        tc.transition(c["id"], tc.VERIFIED_DONE, by="verifier")


def test_tests_required_contract_demands_tests_evidence():
    c = tc.create(GOOD)          # tests_required=True
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.AGENT_DONE, by="agent")
    tc.transition(c["id"], tc.VERIFYING, by="verifier")
    with pytest.raises(tc.ContractError, match="tests"):
        tc.transition(c["id"], tc.VERIFIED_DONE, by="verifier",
                      evidence={"note": "looks fine"})


def test_live_check_required_demands_live_evidence():
    c = tc.create({**GOOD, "live_check_required": True})
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.AGENT_DONE, by="agent")
    tc.transition(c["id"], tc.VERIFYING, by="verifier")
    with pytest.raises(tc.ContractError, match="live check"):
        tc.transition(c["id"], tc.VERIFIED_DONE, by="verifier",
                      evidence={"tests": {"ok": True}})


def test_verification_failed_reopens_work():
    c = tc.create(GOOD)
    for st in (tc.WORKING, tc.AGENT_DONE, tc.VERIFYING):
        tc.transition(c["id"], st, by="x")
    tc.transition(c["id"], tc.VERIFICATION_FAILED, by="verifier",
                  evidence={"reason": "suite red"})
    out = tc.transition(c["id"], tc.WORKING, by="agent")
    assert out["state"] == tc.WORKING


def test_terminal_states_are_terminal():
    c = tc.create(GOOD)
    tc.transition(c["id"], tc.CANCELLED, by="owner")
    with pytest.raises(tc.ContractError, match="illegal transition"):
        tc.transition(c["id"], tc.WORKING, by="agent")


def test_owner_decision_round_trip():
    c = tc.create(GOOD)
    tc.transition(c["id"], tc.WORKING, by="agent")
    tc.transition(c["id"], tc.OWNER_DECISION, by="agent")
    out = tc.transition(c["id"], tc.WORKING, by="owner")
    assert out["state"] == tc.WORKING


def test_list_filters_by_state_and_task():
    a = tc.create(GOOD, task_id=192)
    b = tc.create(GOOD, task_id=193)
    tc.transition(b["id"], tc.WORKING, by="agent")
    assert [c["id"] for c in tc.list_contracts(state=tc.CREATED)] == [a["id"]]
    assert [c["id"] for c in tc.list_contracts(task_id=193)] == [b["id"]]
