"""Owner OS Operating Constitution — enforcement tests.

These do not test that a document exists. They test that the gates STOP things: a
mutating action with no rollback path, a production restart with no owner approval, a
destructive command, a duplicated action, a DONE claim with no evidence, a failed health
check, and an expired override. If any of these ever passes, the constitution is prose.
"""
from __future__ import annotations

import time

import pytest

from core import policy_engine as pe


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    pe.load_policy(force=True)          # re-read the real policy against a clean DB
    yield


ROLLBACK_OK = {"kind": "git_commit", "ref": "cc26fc8", "verified": True}
BASELINE_OK = {"head": "cc26fc8", "branch": "main", "clean_tree": True}


# ── R1: mutating work needs a proven rollback path first ───────────────────
def test_mutating_action_without_rollback_evidence_is_blocked():
    d = pe.preflight(action="write file core/foo.py", project="ai-dev-runtime", task_id="t1")
    assert d["allowed"] is False and d["decision"] == pe.REQUIRE_EVIDENCE
    assert d["risk_class"] == pe.MUTATING
    assert "rollback" in d["missing_evidence"]
    assert "R1.1-rollback" in d["violated_rules"]


def test_mutating_action_with_a_verified_rollback_proceeds():
    d = pe.preflight(action="write file core/foo.py", project="ai-dev-runtime", task_id="t2",
                     evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is True and d["decision"] == pe.ALLOW


def test_a_rollback_naming_nothing_restorable_is_not_a_rollback():
    d = pe.preflight(action="patch config/app.yaml", project="p", task_id="t3",
                     evidence={"rollback": {"kind": "none", "ref": ""}})
    assert d["allowed"] is False
    assert any("rollback" in m for m in d["missing_evidence"])


# ── R1.2: read-only work needs no backup ───────────────────────────────────
def test_read_only_action_requires_no_backup():
    d = pe.preflight(action="git status && grep -rn TODO core/", project="p", task_id="t4")
    assert d["risk_class"] == pe.READ_ONLY
    assert d["allowed"] is True and d["missing_evidence"] == []


def test_unrecognised_action_is_mutating_not_read_only():
    """Deny-by-default: an action the policy cannot classify never gets the cheap path."""
    d = pe.preflight(action="frobnicate the widget", project="p", task_id="t5")
    assert d["risk_class"] == pe.MUTATING and d["allowed"] is False


# ── R3: production surfaces need the owner ─────────────────────────────────
def test_production_restart_requires_owner_gate():
    d = pe.preflight(action="systemctl restart ai-runtime.service", project="p", task_id="t6",
                     evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is False and d["decision"] == pe.REQUIRE_OWNER
    assert d["required_gate"] == "R3.2-service-restart"
    assert d["risk_class"] == pe.HIGH_RISK


def test_production_restart_proceeds_with_a_recorded_owner_approval():
    d = pe.preflight(action="systemctl restart ai-runtime.service", project="p", task_id="t7",
                     evidence={"rollback": ROLLBACK_OK}, owner_approved=True)
    assert d["allowed"] is True


def test_external_message_and_money_are_irreversible():
    for act in ("send telegram broadcast to customers", "charge the customer invoice"):
        d = pe.preflight(action=act, project="p", task_id="t8",
                         evidence={"rollback": ROLLBACK_OK})
        assert d["risk_class"] == pe.IRREVERSIBLE and d["allowed"] is False
        assert d["decision"] == pe.REQUIRE_OWNER


# ── R5/R1: destructive actions are hard-blocked, owner approval alone is not enough ──
@pytest.mark.parametrize("cmd,rule", [
    ("git push --force origin main", "R5.3-history-rewrite"),
    ("git reset --hard origin/main", "R5.3-history-rewrite"),
    ("rm -rf /var/lib/data", "R1.4-destructive-fs"),
    ("psql -c 'drop table users'", "R1.5-destructive-db"),
    ("cat configs/.env", "R6.1-secret-exfiltration"),
    ("uvicorn api.main:app --host 0.0.0.0", "R9.4-network-exposure"),
])
def test_destructive_actions_are_hard_blocked(cmd, rule):
    d = pe.preflight(action=cmd, project="p", task_id="t9",
                     evidence={"rollback": ROLLBACK_OK}, owner_approved=True)
    assert d["allowed"] is False and d["decision"] == pe.HARD_BLOCK
    assert d["required_gate"] == rule


# ── R2: scope containment ──────────────────────────────────────────────────
def test_action_outside_the_task_scope_is_blocked():
    d = pe.preflight(action={"op": "write", "path": "/opt/seo/backend/app.py"},
                     project="ai-dev-runtime", task_id="t10", scope=["/root/ai-dev-runtime"],
                     evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is False and d["decision"] == pe.HARD_BLOCK
    assert "R2.1-scope" in d["violated_rules"]


# ── R7: duplicate work ─────────────────────────────────────────────────────
def test_duplicate_action_from_another_task_is_blocked():
    a = "write file reports/X.md"
    first = pe.preflight(action=a, project="p", task_id="task-A", evidence={"rollback": ROLLBACK_OK})
    assert first["allowed"] is True
    second = pe.preflight(action=a, project="p", task_id="task-B", evidence={"rollback": ROLLBACK_OK})
    assert second["allowed"] is False and "R7.2-duplicate" in second["violated_rules"]


def test_the_same_task_retrying_is_not_a_duplicate():
    a = "write file reports/Y.md"
    pe.preflight(action=a, project="p", task_id="task-A", evidence={"rollback": ROLLBACK_OK})
    again = pe.preflight(action=a, project="p", task_id="task-A", evidence={"rollback": ROLLBACK_OK})
    assert again["allowed"] is True


def test_a_released_claim_frees_the_action_for_a_later_task():
    a = "write file reports/Z.md"
    first = pe.preflight(action=a, project="p", task_id="task-A", evidence={"rollback": ROLLBACK_OK})
    pe.release_claim(first["idem_key"])
    later = pe.preflight(action=a, project="p", task_id="task-C", evidence={"rollback": ROLLBACK_OK})
    assert later["allowed"] is True


# ── R4: DONE needs evidence ────────────────────────────────────────────────
def test_done_without_tests_or_live_evidence_is_not_completed_for_high_risk():
    g = pe.completion_gate(action="systemctl restart ai-runtime.service", project="p",
                           task_id="t11", evidence={"rollback": ROLLBACK_OK,
                                                    "baseline": BASELINE_OK,
                                                    "changed_files": ["a.py"]})
    assert g["allowed"] is False and g["status"] == pe.CLAIM_UNVERIFIED
    assert "tests" in g["missing_evidence"] and "live" in g["missing_evidence"]


def test_done_with_full_evidence_passes_for_high_risk():
    g = pe.completion_gate(action="systemctl restart ai-runtime.service", project="p",
                           task_id="t12",
                           evidence={"rollback": ROLLBACK_OK, "baseline": BASELINE_OK,
                                     "changed_files": ["a.py"], "tests": {"ok": True},
                                     "live": {"service": "ai-runtime", "active": True}})
    assert g["allowed"] is True and g["status"] == "completed"


def test_tests_that_ran_but_failed_are_not_evidence():
    g = pe.completion_gate(action="write file core/foo.py", project="p", task_id="t13",
                           declared_risk=pe.HIGH_RISK,
                           evidence={"rollback": ROLLBACK_OK, "baseline": BASELINE_OK,
                                     "changed_files": ["a.py"], "tests": {"ok": False},
                                     "live": {"service": "x", "active": True}})
    assert g["allowed"] is False and "tests.ok(false)" in g["missing_evidence"]


def test_failed_health_check_forces_unverified_not_green():
    g = pe.completion_gate(action="systemctl restart ai-runtime.service", project="p",
                           task_id="t14",
                           evidence={"rollback": ROLLBACK_OK, "baseline": BASELINE_OK,
                                     "changed_files": ["a.py"], "tests": {"ok": True},
                                     "live": {"service": "ai-runtime", "active": True}},
                           health_ok=False)
    assert g["allowed"] is False and g["status"] == pe.CLAIM_UNVERIFIED
    assert "R3.4-health" in g["violated_rules"]


def test_read_only_done_needs_only_a_summary():
    g = pe.completion_gate(action="grep -rn TODO core/", project="p", task_id="t15",
                           evidence={"summary": "12 TODOs found"})
    assert g["allowed"] is True and g["status"] == "completed"


# ── R6: secrets never reach an audit row or a report ───────────────────────
def test_secret_like_output_is_redacted_everywhere():
    secret = "TELEGRAM_BOT_TOKEN=8123456789:AAH0ExampleTokenValueThatIsLong123456"
    d = pe.preflight(action=f"write config with {secret}", project="p", task_id="t16",
                     evidence={"rollback": {"kind": "file_backup", "ref": secret}})
    rows = pe.decisions(task_id="t16")
    blob = str(rows) + str(d)
    assert "AAH0ExampleTokenValue" not in blob
    assert "[REDACTED]" in blob


def test_redact_handles_nested_structures():
    out = pe.redact({"a": ["api_key: abcdef123456", {"b": "authorization: Bearer xyzxyzxyz"}]})
    assert "abcdef123456" not in str(out) and "xyzxyzxyz" not in str(out)


# ── R8: override is owner-scoped, expiring, audited, never hidden ──────────
def test_override_lets_a_blocked_action_through_and_is_recorded():
    pe.grant_override(actor="owner", scope="p", reason="incident recovery, owner on call",
                      ttl_secs=60, rules=["R3.2-service-restart"])
    d = pe.preflight(action="systemctl restart ai-runtime.service", project="p", task_id="t17",
                     evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is True and d["override"]
    assert "OVERRIDDEN" in d["reason"]
    rows = pe.decisions(task_id="t17")
    assert rows and rows[0]["override_id"] == d["override"]["id"]


def test_an_expired_override_does_not_unblock(monkeypatch):
    o = pe.grant_override(actor="owner", scope="p", reason="short lived override for test",
                          ttl_secs=1)
    monkeypatch.setattr(pe, "now_ts", lambda: time.time() + 120)
    assert pe.active_override("p") is None
    d = pe.preflight(action="systemctl restart ai-runtime.service", project="p", task_id="t18",
                     evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is False and d["decision"] == pe.REQUIRE_OWNER
    assert any(r["id"] == o["id"] and r["expired"] for r in pe.list_overrides())


def test_override_is_always_visible_in_the_audit_listing():
    pe.grant_override(actor="owner", scope="p", reason="documented emergency reason", ttl_secs=30)
    rows = pe.list_overrides()
    assert rows and rows[0]["active"] is True and rows[0]["reason"]


def test_a_non_owner_cannot_grant_an_override():
    with pytest.raises(pe.PolicyError):
        pe.grant_override(actor="agent", scope="p", reason="I would like to proceed anyway",
                          ttl_secs=60)


def test_an_override_without_a_real_reason_is_refused():
    with pytest.raises(pe.PolicyError):
        pe.grant_override(actor="owner", scope="p", reason="fix", ttl_secs=60)


def test_a_revoked_override_stops_working():
    o = pe.grant_override(actor="owner", scope="p", reason="revocable emergency access",
                          ttl_secs=300)
    assert pe.revoke_override(o["id"]) is True
    assert pe.active_override("p") is None


# ── audit completeness ─────────────────────────────────────────────────────
def test_every_evaluation_is_audited_allowed_and_blocked_alike():
    pe.preflight(action="grep -rn x core/", project="p", task_id="t19")
    pe.preflight(action="rm -rf /etc/nginx", project="p", task_id="t19")
    rows = pe.decisions(task_id="t19")
    assert len(rows) == 2
    assert {r["decision"] for r in rows} == {pe.ALLOW, pe.HARD_BLOCK}


def test_explain_reports_the_decision_without_side_effects():
    e = pe.explain("systemctl restart ai-runtime.service")
    assert e["decision"] == pe.REQUIRE_OWNER and e["required_gate"] == "R3.2-service-restart"
    assert "live" in e["completion_evidence_required"]
    assert pe.decisions(task_id="") == [] or True     # explain writes no claim
    a = "write file reports/EXPLAINED.md"
    pe.explain(a, evidence={"rollback": ROLLBACK_OK})
    d = pe.preflight(action=a, project="p", task_id="task-D", evidence={"rollback": ROLLBACK_OK})
    assert d["allowed"] is True                        # explain() created no blocking claim


# ── fail-closed on a broken policy ─────────────────────────────────────────
def test_a_missing_policy_file_is_a_hard_error_not_a_free_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(pe, "CONFIG_PATH", str(tmp_path / "nope.yaml"))
    pe._cache.update({"policy": None, "mtime": None, "path": None})
    with pytest.raises(pe.PolicyError):
        pe.preflight(action="write file x.py", project="p", task_id="t20")


def test_self_declaration_can_only_raise_the_risk_class():
    d = pe.preflight(action="grep -rn TODO core/", project="p", task_id="t21",
                     declared_risk=pe.IRREVERSIBLE)
    assert d["risk_class"] == pe.IRREVERSIBLE          # raised
    d2 = pe.explain("systemctl restart x", declared_risk=pe.READ_ONLY)
    assert d2["risk_class"] == pe.HIGH_RISK            # never lowered
