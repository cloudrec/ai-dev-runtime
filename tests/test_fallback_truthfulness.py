"""A fallback PLAN must never be reported as a completed implementation.

Jobs 59/60/61 each produced only a deterministic fallback Markdown plan and
still finished with `status=completed`. The outcome field already said
`fallback_plan_only`, but every consumer filtered on `status`, so the lie
survived. These tests pin the coupling between the two fields.
"""
from __future__ import annotations

import pytest

from core import job_kinds, notify_format as nf


# ── status/outcome coupling ─────────────────────────────────────────────────
def test_plan_only_outcome_can_never_be_completed():
    assert job_kinds.terminal_status_for(job_kinds.FALLBACK_PLAN_ONLY, "completed") == \
        job_kinds.STATUS_FALLBACK_PLAN_ONLY


@pytest.mark.parametrize("status", ["failed", "blocked", "fallback_plan_only"])
def test_plan_only_may_end_in_the_allowed_terminal_states(status):
    assert job_kinds.terminal_status_for(job_kinds.FALLBACK_PLAN_ONLY, status) == status


def test_real_implementation_still_completes():
    assert job_kinds.terminal_status_for(job_kinds.IMPLEMENTED, "completed") == "completed"
    assert job_kinds.terminal_status_for(job_kinds.OPERATIONAL_COMPLETE, "completed") == "completed"


def test_truthful_terminal_detects_the_old_bug():
    # Exactly the shape jobs 59/60/61 were stored in.
    assert job_kinds.is_truthful_terminal("completed", job_kinds.FALLBACK_PLAN_ONLY) is False
    assert job_kinds.is_truthful_terminal("fallback_plan_only", job_kinds.FALLBACK_PLAN_ONLY) is True
    assert job_kinds.is_truthful_terminal("completed", job_kinds.IMPLEMENTED) is True


def test_plan_only_is_not_releasable_or_an_implementation():
    assert job_kinds.is_releasable(job_kinds.FALLBACK_PLAN_ONLY) is False
    assert job_kinds.is_implementation(job_kinds.FALLBACK_PLAN_ONLY) is False


def test_fallback_status_is_a_known_terminal_status():
    from core import job_executor, job_store
    assert job_kinds.STATUS_FALLBACK_PLAN_ONLY in job_store.STATUSES
    assert job_kinds.STATUS_FALLBACK_PLAN_ONLY in job_executor._TERMINAL_STATUSES


# ── executor writes the truthful status ─────────────────────────────────────
def test_finish_downgrades_a_plan_only_completed_call(monkeypatch):
    """_finish is the chokepoint: even a direct "completed" call is corrected."""
    from core import job_executor

    recorded = {}
    monkeypatch.setattr(job_executor.job_store, "update_job",
                        lambda job_id, **kw: recorded.update(kw) or {"id": job_id, **kw})
    monkeypatch.setattr(job_executor, "_write_report", lambda job: None)

    job_executor._finish("job-1", "completed", outcome=job_kinds.FALLBACK_PLAN_ONLY)
    assert recorded["status"] == "fallback_plan_only"
    assert recorded["outcome"] == job_kinds.FALLBACK_PLAN_ONLY


def test_finish_leaves_a_real_implementation_completed(monkeypatch, tmp_path):
    from core import job_executor

    recorded = {}
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(job_executor.job_store, "update_job",
                        lambda job_id, **kw: recorded.update(kw) or {"id": job_id, **kw})
    # The constitution's completion gate reads the job's recorded evidence, so this
    # fully-mocked store must present a job that HAS a rollback path — otherwise the
    # correct answer is `blocked`, which is a different invariant than the one here.
    monkeypatch.setattr(job_executor.job_store, "get_job", lambda job_id: {
        "id": job_id, "goal": "implement the widget", "instructions": "",
        "project_path": str(tmp_path), "changed_files": [{"path": "w.py"}],
        "git_info": {"branch": "work", "commit": "abc1234"},
        "artifacts": [{"rollback": {"kind": "file_backup", "ref": "bk-1", "verified": True}}]})
    monkeypatch.setattr(job_executor, "_write_report", lambda job: None)

    job_executor._finish("job-2", "completed", outcome=job_kinds.IMPLEMENTED)
    assert recorded["status"] == "completed"


def test_finish_refuses_completed_when_the_job_record_cannot_be_read(monkeypatch, tmp_path):
    """Fail-closed: no readable job means no evidence, and no evidence is not a
    completion. A gate that passed here would be bypassable by losing the record."""
    from core import job_executor

    recorded = {}
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp2.db"))
    monkeypatch.setattr(job_executor.job_store, "update_job",
                        lambda job_id, **kw: recorded.update(kw) or {"id": job_id, **kw})
    monkeypatch.setattr(job_executor.job_store, "get_job", lambda job_id: None)
    monkeypatch.setattr(job_executor, "_write_report", lambda job: None)

    job_executor._finish("job-3", "completed", outcome=job_kinds.IMPLEMENTED)
    assert recorded["status"] == "blocked"
    assert "completion gate" in (recorded.get("error") or "")


# ── notifications ───────────────────────────────────────────────────────────
def test_plan_only_never_gets_the_completed_event_type():
    assert nf.event_type_for(job_kinds.FALLBACK_PLAN_ONLY) == nf.FALLBACK_PLAN_ONLY_EVENT
    assert nf.event_type_for(job_kinds.FALLBACK_PLAN_ONLY) != nf.COMPLETED_EVENT


def test_real_outcomes_get_the_completed_event_type():
    assert nf.event_type_for(job_kinds.IMPLEMENTED) == nf.COMPLETED_EVENT
    assert nf.event_type_for(job_kinds.OPERATIONAL_COMPLETE) == nf.COMPLETED_EVENT


def test_plan_only_message_says_not_implemented():
    text = nf.render_completed({"job_id": 59, "task_title": "Build the control plane",
                                "outcome": "fallback_plan_only"})
    assert "PLAN ONLY" in text
    assert "NOT implemented" in text
    assert "❌ Implementation NOT completed" in text
    assert "⚠️ Fallback plan only" in text


def test_mislabelled_completed_event_is_rendered_truthfully():
    """An emitter that still sends runtime.job.completed for a plan must not be
    able to launder it into a success message."""
    text = nf.render_event({"type": "runtime.job.completed",
                            "payload": {"job_id": 60, "outcome": "fallback_plan_only"}})
    assert "PLAN ONLY" in text
    assert "✅ Implementation completed" not in text


def test_fallback_event_type_renders():
    text = nf.render_event({"type": nf.FALLBACK_PLAN_ONLY_EVENT,
                            "payload": {"job_id": 61, "outcome": "fallback_plan_only"}})
    assert "PLAN ONLY" in text


# ── the five facts are reported independently ───────────────────────────────
def test_facts_distinguish_implementation_tests_branch_and_release():
    lines = nf.render_facts({"outcome": "implemented", "tests_passed": True,
                             "branch": "feat/x", "released": True})
    assert lines == ["✅ Implementation completed", "✅ Tests passed",
                     "✅ Branch created: feat/x", "✅ Released to production"]


def test_implementation_does_not_imply_tests_or_release():
    lines = nf.render_facts({"outcome": "implemented", "tests_passed": False, "released": False})
    assert "✅ Implementation completed" in lines
    assert "❌ Tests not passed" in lines
    assert "❌ Not released to production" in lines
    assert "❌ No branch created" in lines


def test_unknown_facts_are_marked_unknown_not_assumed_true():
    lines = nf.render_facts({"outcome": "implemented"})
    assert any("Tests not passed (unknown)" in line for line in lines)
    assert any("Not released to production (unknown)" in line for line in lines)


def test_plan_only_facts_report_no_implementation():
    lines = nf.render_facts({"outcome": "fallback_plan_only", "branch": "feat/x"})
    assert "❌ Implementation NOT completed" in lines
    assert "⚠️ Fallback plan only — a document, not an implementation" in lines
