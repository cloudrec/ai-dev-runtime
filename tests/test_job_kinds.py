"""Job kind routing, task-specific validation, and outcome semantics."""
import os
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import job_kinds, job_validation  # noqa: E402


# --------------------------------------------------------------------------
# kind routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("goal,expected", [
    ("Fix operational job test gating", job_kinds.CODE_CHANGE),
    ("Build Release Controller", job_kinds.CODE_CHANGE),
    ("Run first Prospect Audit batch", job_kinds.OPERATIONAL),
    ("Run JobHunter global employer workstream", job_kinds.OPERATIONAL),
    ("Run Social content production batch", job_kinds.CONTENT_PRODUCTION),
    ("Prepare Safe Guard live deployment now", job_kinds.DEPLOYMENT),
    ("Prepare Safe Guard demo VPS deployment", job_kinds.DEPLOYMENT),
    ("Prepare email handoff without sending", job_kinds.DATA_HANDOFF),
    ("Restore protected project agent contexts", job_kinds.CONTEXT_RESTORE),
])
def test_classify_real_owner_tasks(goal, expected):
    """The exact goals of OWNER-113..120 must route to their real kinds."""
    assert job_kinds.classify(goal) == expected


def test_explicit_kind_always_wins_over_text():
    assert job_kinds.classify("Run a content batch", explicit=job_kinds.CODE_CHANGE) == job_kinds.CODE_CHANGE


def test_invalid_explicit_kind_falls_back_to_classification():
    assert job_kinds.classify("Run Social content production batch", explicit="nonsense") == \
        job_kinds.CONTENT_PRODUCTION


def test_unknown_text_defaults_to_strictest_kind():
    """An unrecognised job must never be under-validated."""
    assert job_kinds.classify("zzz qqq") == job_kinds.CODE_CHANGE


def test_only_code_change_requires_repo_tests():
    assert job_kinds.requires_repo_tests(job_kinds.CODE_CHANGE)
    for kind in (job_kinds.OPERATIONAL, job_kinds.CONTENT_PRODUCTION, job_kinds.DEPLOYMENT,
                 job_kinds.DATA_HANDOFF, job_kinds.CONTEXT_RESTORE):
        assert not job_kinds.requires_repo_tests(kind), kind


def test_no_code_changes_is_not_an_error_for_non_code_kinds():
    assert job_kinds.requires_code_changes(job_kinds.CODE_CHANGE)
    for kind in (job_kinds.OPERATIONAL, job_kinds.CONTENT_PRODUCTION):
        assert not job_kinds.requires_code_changes(kind)


def test_no_kind_may_create_an_empty_commit():
    for kind in job_kinds.KINDS:
        assert job_kinds.allows_empty_commit(kind) is False


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------

def test_fallback_plan_only_is_never_an_implementation():
    assert not job_kinds.is_implementation(job_kinds.FALLBACK_PLAN_ONLY)
    assert not job_kinds.is_releasable(job_kinds.FALLBACK_PLAN_ONLY)


def test_fallback_plan_only_summary_says_not_implemented():
    assert "NOT implemented" in job_kinds.summarize(job_kinds.CODE_CHANGE, job_kinds.FALLBACK_PLAN_ONLY)


def test_only_implemented_is_releasable():
    for outcome in job_kinds.OUTCOMES:
        if outcome == job_kinds.IMPLEMENTED:
            assert job_kinds.is_releasable(outcome)
        else:
            assert not job_kinds.is_releasable(outcome), outcome


def test_success_outcome_per_kind():
    assert job_kinds.success_outcome_for(job_kinds.CODE_CHANGE) == job_kinds.IMPLEMENTED
    assert job_kinds.success_outcome_for(job_kinds.OPERATIONAL) == job_kinds.OPERATIONAL_COMPLETE
    assert job_kinds.success_outcome_for(job_kinds.CONTENT_PRODUCTION) == job_kinds.CONTENT_COMPLETE
    assert job_kinds.success_outcome_for(job_kinds.DEPLOYMENT) == job_kinds.DEPLOYMENT_PREPARED
    assert job_kinds.success_outcome_for(job_kinds.DATA_HANDOFF) == job_kinds.DATA_HANDOFF_COMPLETE
    assert job_kinds.success_outcome_for(job_kinds.CONTEXT_RESTORE) == job_kinds.CONTEXT_RESTORED


# --------------------------------------------------------------------------
# task-specific validation
# --------------------------------------------------------------------------

def test_repo_suite_commands_are_stripped_for_non_code_jobs():
    """The exact regression behind OWNER-114..120: a content job gated on the
    repository suite fails on defects it did not introduce."""
    cmds = ["python3 -m pytest -q", "test -s reports/out.md"]
    assert job_validation.strip_repo_suite_commands(cmds) == ["test -s reports/out.md"]


@pytest.mark.parametrize("cmd", [
    "python3 -m pytest -q", "pytest tests/", "python3 -m unittest discover", "tox -e py312",
])
def test_repo_suite_markers_detected(cmd):
    assert job_validation.is_repo_suite_command(cmd)


def test_task_command_is_not_mistaken_for_repo_suite():
    assert not job_validation.is_repo_suite_command("test -s reports/batch.md && echo OK")


def test_validation_passes_when_artifacts_exist(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "batch.md").write_text("content produced")
    changed = [{"path": "reports/batch.md", "operation": "create", "before": "absent", "after": "x"}]
    result = job_validation.validate(job_kinds.CONTENT_PRODUCTION, str(tmp_path), {}, changed)
    assert result["ok"]
    assert result["repo_suite_used"] is False
    assert "content" in result["validation_kind"]


def test_validation_fails_when_claimed_artifact_is_missing(tmp_path):
    changed = [{"path": "reports/missing.md", "operation": "create", "before": "absent", "after": "x"}]
    result = job_validation.validate(job_kinds.OPERATIONAL, str(tmp_path), {}, changed)
    assert not result["ok"]


def test_validation_fails_when_artifact_is_empty(tmp_path):
    (tmp_path / "empty.md").write_text("")
    changed = [{"path": "empty.md", "operation": "create", "before": "absent", "after": "x"}]
    result = job_validation.validate(job_kinds.OPERATIONAL, str(tmp_path), {}, changed)
    assert not result["ok"]


def test_validation_fails_when_job_produced_nothing(tmp_path):
    result = job_validation.validate(job_kinds.OPERATIONAL, str(tmp_path), {}, [])
    assert not result["ok"]
    assert any("at least one artifact" in c["check"] for c in result["results"])


def test_repo_suite_failure_cannot_fail_a_content_job(tmp_path):
    """Even when the plan carries `python3 -m pytest -q`, a content job is not
    gated on it — the command is dropped and recorded as dropped."""
    (tmp_path / "post.md").write_text("a social post")
    changed = [{"path": "post.md", "operation": "create", "before": "absent", "after": "x"}]

    def _never_called(project_path, commands):
        raise AssertionError(f"repo suite must not run for a content job: {commands}")

    result = job_validation.validate(job_kinds.CONTENT_PRODUCTION, str(tmp_path),
                                     {"test_commands": ["python3 -m pytest -q"]}, changed,
                                     run_commands=_never_called)
    assert result["ok"]
    assert result["dropped_repo_suite_commands"] == ["python3 -m pytest -q"]


def test_task_specific_command_is_run_and_can_fail(tmp_path):
    (tmp_path / "post.md").write_text("x")
    changed = [{"path": "post.md", "operation": "create", "before": "absent", "after": "x"}]

    def _runner(project_path, commands):
        return {"ok": False, "results": [{"cmd": commands[0], "passed": False, "output": "boom"}]}

    result = job_validation.validate(job_kinds.OPERATIONAL, str(tmp_path),
                                     {"test_commands": ["test -s nope.md"]}, changed,
                                     run_commands=_runner)
    assert not result["ok"]
