"""Telegram notification content for the outcome model.

A completion message must let the owner tell an implementation from a plan
without opening the repo, and must carry the fields needed to act: job id,
title, outcome, branch, commit, test/validation status, short reason, next step.
"""
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import notify_format as nf  # noqa: E402


def _payload(**kw):
    base = {
        "job_id": "7345c8e1-98ed-4ca3-b327-a74f960d428b",
        "task_title": "Build Release Controller",
        "outcome": "implemented",
        "branch": "ai-runtime/111-build-release-controller",
        "commit": "5e3ec9e1234567890abc",
        "tests_result": "170 passed",
        "validation": "repository test suite",
        "next_action": "review and release",
    }
    base.update(kw)
    return base


def test_completed_message_carries_every_required_field():
    msg = nf.render_completed(_payload(reason="n/a"))
    assert "7345c8e1-98ed-4ca3-b327-a74f960d428b" in msg   # job id
    assert "Build Release Controller" in msg               # task title
    assert "Outcome: implemented" in msg                   # outcome
    assert "ai-runtime/111-build-release-controller" in msg  # branch
    assert "5e3ec9e12345" in msg                           # commit
    assert "170 passed" in msg                             # test status
    assert "repository test suite" in msg                  # validation status
    assert "Reason: n/a" in msg                            # short reason
    assert "review and release" in msg                     # next step


def test_fallback_plan_only_is_never_announced_as_completed_implementation():
    """OWNER-111 was announced as completed while only a Markdown plan existed."""
    msg = nf.render_completed(_payload(outcome="fallback_plan_only",
                                       reason="planner timed out",
                                       next_action="requeue for real implementation"))
    assert "PLAN ONLY" in msg
    assert "NOT implemented" in msg
    assert "This task is NOT done" in msg
    assert not msg.startswith("✅")


def test_implemented_is_announced_as_completed():
    msg = nf.render_completed(_payload())
    assert msg.startswith("✅")
    assert "PLAN ONLY" not in msg
    assert "NOT done" not in msg


@pytest.mark.parametrize("outcome,marker", [
    ("operational_complete", "✅"),
    ("content_complete", "✅"),
    ("deployment_prepared", "deployment prepared"),
    ("data_handoff_complete", "handoff prepared"),
    ("context_restored", "context restored"),
    ("failed", "❌"),
])
def test_each_outcome_has_its_own_headline(outcome, marker):
    msg = nf.render_completed(_payload(outcome=outcome))
    assert marker in msg
    assert f"Outcome: {outcome}" in msg


def test_failure_reason_falls_back_to_error_field():
    msg = nf.render_completed(_payload(outcome="failed", error="tests failed after repair attempts",
                                       reason=None))
    assert "tests failed after repair attempts" in msg


def test_unknown_outcome_does_not_crash_and_omits_missing_fields():
    msg = nf.render_completed({"task_title": "Something", "outcome": "weird_new_outcome"})
    assert "Something" in msg
    assert "None" not in msg


def test_missing_outcome_keeps_previous_wording():
    """Backward compatibility: an event with no outcome still renders."""
    msg = nf.render_completed({"job_id": "j1", "task_title": "Legacy job"})
    assert msg.startswith("✅ Runtime job completed")
    assert "None" not in msg


def test_render_event_routes_completed_and_preserves_other_events():
    completed = nf.render_event({"type": "runtime.job.completed",
                                 "payload": _payload(outcome="fallback_plan_only")})
    assert "PLAN ONLY" in completed
    warning = nf.render_event({"type": "runtime.warning", "text": "disk almost full"})
    assert warning == "disk almost full"
