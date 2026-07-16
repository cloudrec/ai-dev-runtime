"""Untracked workspace handling and job outcome reporting.

Regression cover for the cascade behind OWNER-113..120:

OWNER-113 failed its tests. `BackupEngine.rollback()` restores archived files by
extracting a tar, which cannot delete files that did not exist when the snapshot
was taken — so the broken module and its failing test stayed in the shared
workspace as untracked files. Every later job then ran `python3 -m pytest -q` as
its gate, tripped over OWNER-113's leftover defect, and reported
"tests failed after repair attempts" for work it had nothing to do with.
"""
import os
import sys
import tempfile

sys.path.insert(0, "/root/ai-dev-runtime")

from core import job_executor, job_kinds  # noqa: E402


# --------------------------------------------------------------------------
# removal of files a failed job created
# --------------------------------------------------------------------------

def test_created_files_are_removed_so_they_cannot_poison_later_jobs(tmp_path):
    broken = tmp_path / "tests"
    broken.mkdir()
    (broken / "test_broken.py").write_text("def test_x(): assert False\n")
    (tmp_path / "core_mod.py").write_text("raise SystemExit\n")

    changed = [
        {"path": "tests/test_broken.py", "operation": "create", "before": "absent", "after": "h1"},
        {"path": "core_mod.py", "operation": "create", "before": "absent", "after": "h2"},
    ]
    removed = job_executor._remove_created_paths(str(tmp_path), changed)

    assert sorted(removed) == ["core_mod.py", "tests/test_broken.py"]
    assert not (broken / "test_broken.py").exists()
    assert not (tmp_path / "core_mod.py").exists()


def test_preexisting_files_are_never_removed(tmp_path):
    """A file that existed before the job is restored from the backup archive,
    never deleted by the hygiene sweep."""
    (tmp_path / "existing.py").write_text("important\n")
    changed = [{"path": "existing.py", "operation": "replace", "before": "abc123", "after": "def456"}]

    removed = job_executor._remove_created_paths(str(tmp_path), changed)

    assert removed == []
    assert (tmp_path / "existing.py").read_text() == "important\n"


def test_hygiene_sweep_cannot_escape_the_project_directory(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    project = tmp_path / "project"
    project.mkdir()

    changed = [{"path": "../outside.txt", "operation": "create", "before": "absent", "after": "h"}]
    removed = job_executor._remove_created_paths(str(project), changed)

    assert removed == []
    assert outside.exists(), "hygiene sweep must never delete outside the project"


def test_deleted_paths_are_not_resurrected_or_removed(tmp_path):
    changed = [{"path": "gone.py", "operation": "delete", "before": "abc", "after": "absent"}]
    assert job_executor._remove_created_paths(str(tmp_path), changed) == []


def test_missing_file_is_tolerated(tmp_path):
    changed = [{"path": "never_written.py", "operation": "create", "before": "absent", "after": "h"}]
    assert job_executor._remove_created_paths(str(tmp_path), changed) == []


def test_the_exact_owner_113_leftovers_would_be_cleaned(tmp_path):
    """The two files that actually poisoned OWNER-114..120."""
    (tmp_path / "core").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "core" / "prospect_audit_batch.py").write_text("# broken impl\n")
    (tmp_path / "tests" / "test_prospect_audit_batch.py").write_text("def test_bad(): assert False\n")

    changed = [
        {"path": "core/prospect_audit_batch.py", "operation": "create", "before": "absent", "after": "a"},
        {"path": "tests/test_prospect_audit_batch.py", "operation": "create", "before": "absent", "after": "b"},
    ]
    removed = job_executor._remove_created_paths(str(tmp_path), changed)

    assert len(removed) == 2
    assert not (tmp_path / "tests" / "test_prospect_audit_batch.py").exists(), \
        "a failed job's failing test must not survive to gate the next job"


# --------------------------------------------------------------------------
# outcome recorded on the job
# --------------------------------------------------------------------------

def test_failed_status_always_carries_a_failed_outcome(monkeypatch):
    """No terminal state may be outcome-less: that ambiguity is the bug."""
    captured = {}

    def _fake_update(job_id, **fields):
        captured.update(fields)
        return None

    monkeypatch.setattr(job_executor.job_store, "update_job", _fake_update)
    job_executor._finish("job-1", "failed", error="boom")

    assert captured["outcome"] == job_kinds.FAILED
    assert captured["status"] == "failed"


def test_explicit_outcome_is_not_overwritten(monkeypatch):
    captured = {}
    monkeypatch.setattr(job_executor.job_store, "update_job",
                        lambda job_id, **f: captured.update(f) or None)
    job_executor._finish("job-1", "completed", outcome=job_kinds.FALLBACK_PLAN_ONLY)
    assert captured["outcome"] == job_kinds.FALLBACK_PLAN_ONLY
