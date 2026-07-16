"""End-to-end executor behaviour per job kind.

Drives the real pipeline (plan -> backup -> branch -> edit -> validate -> commit)
with a stubbed planner CLI, and asserts the two results that were wrong in
production:

* an operational/content job is NOT gated on the repository test suite;
* a fallback plan reports `fallback_plan_only`, never a completed implementation.
"""
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import ai_planner, job_executor, job_kinds, job_store  # noqa: E402


def setup_module(_m):
    try:
        os.remove(os.environ["RUNTIME_DB"])
    except (FileNotFoundError, KeyError):
        pass
    job_store.init_db()


def _git(path, *args):
    subprocess.run(["git", "-C", str(path)] + list(args), check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "init")
    return r


def _planner_cli(tmp_path, plan: dict, name="fake_planner.py"):
    """Stub the planner CLI. `result` must carry the plan as a JSON *string*,
    matching the real provider envelope."""
    p = tmp_path / name
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"plan = {plan!r}\n"
        "result = json.dumps(plan)\n"
        'sys.stdout.write(json.dumps({"type": "result", "subtype": "success",'
        ' "is_error": False, "result": result}))\n'
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _failing_planner_cli(tmp_path):
    """A planner that returns non-JSON, forcing the deterministic fallback plan —
    exactly what happened for OWNER-111..120."""
    p = tmp_path / "broken_planner.py"
    p.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdout.write('not json at all')\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run(repo, monkeypatch, cli, goal, kind=None, timeout="20"):
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", timeout)
    importlib.reload(ai_planner)
    job = job_store.create_job(project_path=str(repo), goal=goal, instructions="",
                               autonomy_level="execute_safe", auto_commit=True,
                               auto_push=False, kind=kind)
    job_executor.execute(job["id"])
    return job_store.get_job(job["id"])


# --------------------------------------------------------------------------
# kind is recorded and routed
# --------------------------------------------------------------------------

def test_job_records_its_kind_at_creation(repo):
    job = job_store.create_job(project_path=str(repo), goal="Run Social content production batch",
                               instructions="")
    assert job["kind"] == job_kinds.CONTENT_PRODUCTION
    assert job["outcome"] is None


def test_explicit_kind_is_stored(repo):
    job = job_store.create_job(project_path=str(repo), goal="anything", instructions="",
                               kind=job_kinds.DEPLOYMENT)
    assert job["kind"] == job_kinds.DEPLOYMENT


# --------------------------------------------------------------------------
# the OWNER-114 regression
# --------------------------------------------------------------------------

def test_content_job_is_not_failed_by_a_broken_repo_test_suite(repo, tmp_path, monkeypatch):
    """THE regression: a content job whose plan carries `python3 -m pytest -q`
    must not fail because the repository suite is red for unrelated reasons."""
    # a pre-existing, always-failing test in the workspace (OWNER-113's leftover)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_leftover.py").write_text("def test_broken(): assert False\n")

    cli = _planner_cli(tmp_path, {
        "summary": "produce social posts",
        "files": [{"path": "posts.md", "operation": "create", "content": "# post one\n"}],
        "test_commands": ["python3 -m pytest -q"],
    })
    final = _run(repo, monkeypatch, cli, "Run Social content production batch")

    assert final["kind"] == job_kinds.CONTENT_PRODUCTION
    assert final["status"] == "completed", final.get("error")
    assert final["outcome"] == job_kinds.CONTENT_COMPLETE
    assert final["validation"]["repo_suite_used"] is False
    assert "python3 -m pytest -q" in final["validation"]["dropped_repo_suite_commands"]
    assert (repo / "posts.md").exists()


def test_code_change_job_is_still_gated_on_the_repo_suite(repo, tmp_path, monkeypatch):
    """The gate must remain for real code changes — this is not a blanket bypass."""
    cli = _planner_cli(tmp_path, {
        "summary": "add a module",
        "files": [{"path": "mod.py", "operation": "create", "content": "x = 1\n"},
                  {"path": "check.py", "operation": "create", "content": "raise SystemExit(1)\n"}],
        "test_commands": ["python3 check.py"],
    })
    final = _run(repo, monkeypatch, cli, "Implement a new module")

    assert final["kind"] == job_kinds.CODE_CHANGE
    assert final["status"] == "failed"
    assert final["outcome"] == job_kinds.FAILED
    assert "tests failed" in (final["error"] or "")


def test_failed_code_job_leaves_no_untracked_debris(repo, tmp_path, monkeypatch):
    """A failed job's created files must not survive to poison the next job."""
    cli = _planner_cli(tmp_path, {
        "summary": "add a broken module",
        "files": [{"path": "tests_leftover.py", "operation": "create", "content": "assert False\n"},
                  {"path": "check.py", "operation": "create", "content": "raise SystemExit(1)\n"}],
        "test_commands": ["python3 check.py"],
    })
    final = _run(repo, monkeypatch, cli, "Implement something broken")

    assert final["status"] == "failed"
    assert not (repo / "tests_leftover.py").exists(), \
        "failed job's created file must be removed from the shared workspace"
    assert not (repo / "check.py").exists()


def test_operational_job_with_no_code_changes_is_not_an_error(repo, tmp_path, monkeypatch):
    """No code change is a normal result for an operational job, and it must not
    fabricate a commit to look successful."""
    cli = _planner_cli(tmp_path, {
        "summary": "run the batch",
        "files": [],
        "test_commands": ["python3 -c 'print(1)'"],
    })
    final = _run(repo, monkeypatch, cli, "Run first Prospect Audit batch")

    assert final["kind"] == job_kinds.OPERATIONAL
    assert final["status"] == "completed", final.get("error")
    assert final["outcome"] == job_kinds.OPERATIONAL_COMPLETE
    assert final["changed_files"] == []
    assert not final["git_info"].get("commit"), "operational job must not fabricate a commit"


def test_operational_job_fails_when_its_own_task_command_fails(repo, tmp_path, monkeypatch):
    """Task-specific validation still really validates."""
    cli = _planner_cli(tmp_path, {
        "summary": "run the batch",
        "files": [{"path": "out.md", "operation": "create", "content": "x\n"}],
        "test_commands": ["test -s definitely_missing.md"],
    })
    final = _run(repo, monkeypatch, cli, "Run JobHunter global employer workstream")

    assert final["kind"] == job_kinds.OPERATIONAL
    assert final["status"] == "failed"
    assert final["outcome"] == job_kinds.FAILED
    assert "task validation failed" in (final["error"] or "")


def test_validation_is_explicitly_recorded_in_the_job_result(repo, tmp_path, monkeypatch):
    cli = _planner_cli(tmp_path, {
        "summary": "prepare handoff",
        "files": [{"path": "handoff.md", "operation": "create", "content": "rows\n"}],
        "test_commands": [],
    })
    final = _run(repo, monkeypatch, cli, "Prepare email handoff without sending")

    assert final["kind"] == job_kinds.DATA_HANDOFF
    assert final["outcome"] == job_kinds.DATA_HANDOFF_COMPLETE
    v = final["validation"]
    assert v["ok"] is True
    assert "handoff" in v["validation_kind"]
    assert any("handoff.md" in c["check"] for c in v["results"]), v


# --------------------------------------------------------------------------
# the OWNER-111 regression
# --------------------------------------------------------------------------

def test_fallback_plan_reports_plan_only_never_implemented(repo, tmp_path, monkeypatch):
    """OWNER-111 'Build Release Controller' committed a Markdown plan and was
    reported as completed. The plan must now report `fallback_plan_only`."""
    cli = _failing_planner_cli(tmp_path)
    final = _run(repo, monkeypatch, cli, "Build Release Controller")

    assert final["status"] == "completed"
    assert final["outcome"] == job_kinds.FALLBACK_PLAN_ONLY
    assert not job_kinds.is_implementation(final["outcome"])
    assert not job_kinds.is_releasable(final["outcome"])
    # the plan document itself is preserved as a plan
    assert any("fallback" in c["path"] for c in final["changed_files"]), final["changed_files"]
