"""The constitution on the EXECUTION PATH, not in a document.

`tests/test_owner_os_policy.py` proves the engine decides correctly. This file proves the
decisions are actually wired into the runtime job pipeline: a prohibited job stops before
it edits anything, an owner-gated job stops without an approval, and a job that cannot
show evidence does not get to call itself `completed`.
"""
import importlib
import json
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import ai_planner, job_executor, job_store, policy_engine  # noqa: E402


def setup_module(_m):
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


@pytest.fixture(autouse=True)
def _isolated_cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    policy_engine.load_policy(force=True)
    yield


def _planner_cli(tmp_path, plan: dict, name="policy_planner.py"):
    p = tmp_path / name
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"plan = {plan!r}\n"
        "result = json.dumps(plan)\n"
        'sys.stdout.write(json.dumps({"type": "result", "subtype": "success",'
        ' "is_error": False, "result": result}))\n')
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run(repo, monkeypatch, tmp_path, goal, plan=None, **job_kw):
    plan = plan or {"summary": "safe change",
                    "files": [{"path": "note.md", "operation": "create", "content": "x\n"}],
                    "test_commands": []}
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", _planner_cli(tmp_path, plan))
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", "20")
    importlib.reload(ai_planner)
    job = job_store.create_job(project_path=str(repo), goal=goal, instructions="",
                               autonomy_level="execute_safe", auto_commit=True,
                               auto_push=False, **job_kw)
    job_executor.execute(job["id"])
    return job_store.get_job(job["id"])


# ── preflight actually stops the pipeline ──────────────────────────────────
def test_a_prohibited_job_is_blocked_before_it_edits_anything(repo, tmp_path, monkeypatch):
    final = _run(repo, monkeypatch, tmp_path,
                 "drop table customers from the production database")
    assert final["status"] == "blocked"
    assert "HARD_BLOCK" in (final["error"] or "")
    assert not (repo / "note.md").exists(), "a blocked job must not have edited the workspace"


def test_an_owner_gated_job_stops_without_an_approval(repo, tmp_path, monkeypatch):
    final = _run(repo, monkeypatch, tmp_path,
                 "systemctl restart the api service", approval_required=True)
    assert final["status"] == "blocked"
    assert "REQUIRE_OWNER" in (final["error"] or "")


def test_an_approved_owner_gated_job_passes_preflight_but_still_owes_live_evidence(
        repo, tmp_path, monkeypatch):
    """With the approval recorded the work RUNS — and then cannot call itself done until
    the service is proven live. Approval buys permission to act, never a green result."""
    final = _run(repo, monkeypatch, tmp_path,
                 "systemctl restart the api service", approval_required=False)
    assert (repo / "note.md").exists(), "preflight allowed the work to proceed"
    rows = policy_engine.decisions(task_id=final["id"])
    pre = [r for r in rows if r["phase"] == "preflight"]
    assert pre and pre[0]["decision"] == policy_engine.ALLOW
    assert final["status"] == "blocked"
    assert "completion gate" in (final["error"] or "") and "live" in (final["error"] or "")


def test_an_ordinary_change_still_completes(repo, tmp_path, monkeypatch):
    """Non-regression: the policy layer must not break a normal, safe, evidenced job."""
    final = _run(repo, monkeypatch, tmp_path, "add a short note file to the repo")
    assert final["status"] == "completed", final.get("error")
    assert (repo / "note.md").exists()


# ── the decision is durable and inspectable ────────────────────────────────
def test_every_pipeline_run_leaves_an_audit_row(repo, tmp_path, monkeypatch):
    final = _run(repo, monkeypatch, tmp_path, "add a short note file to the repo")
    rows = policy_engine.decisions(task_id=final["id"])
    phases = {r["phase"] for r in rows}
    assert "preflight" in phases and "completion" in phases
    assert all(r["decision"] == policy_engine.ALLOW for r in rows), rows


def test_a_blocked_run_records_the_violated_rule(repo, tmp_path, monkeypatch):
    final = _run(repo, monkeypatch, tmp_path, "rm -rf /var/lib/postgres data cleanup")
    rows = policy_engine.decisions(task_id=final["id"])
    assert rows and rows[0]["decision"] == policy_engine.HARD_BLOCK
    assert any("R1.4" in r or "R1.5" in r for r in rows[0]["rules"] + [rows[0]["reason"]])


# ── completion gate on the real _finish path ───────────────────────────────
def test_completion_without_a_recorded_rollback_is_not_completed(monkeypatch):
    """A job that reaches `_finish(completed)` with no rollback artifact is recorded
    `blocked`, not green — the false-DONE this gate exists to prevent."""
    job = job_store.create_job(project_path="/root/ai-dev-runtime",
                               goal="update the deployment manifest", instructions="",
                               autonomy_level="execute_safe")
    job_executor._finish(job["id"], "completed", outcome="implemented")
    final = job_store.get_job(job["id"])
    assert final["status"] == "blocked"
    assert "completion gate" in (final["error"] or "")


def test_completion_with_a_recorded_rollback_is_allowed(monkeypatch):
    job = job_store.create_job(project_path="/root/ai-dev-runtime",
                               goal="update the deployment manifest", instructions="",
                               autonomy_level="execute_safe")
    job_store.update_job(job["id"], artifacts=[{"backup_id": "b1", "rollback": {
        "kind": "file_backup", "ref": "b1", "verified": True}}],
        changed_files=[{"path": "m.yaml", "operation": "update"}],
        git_info={"branch": "work", "commit": "abc1234"})
    job_executor._finish(job["id"], "completed", outcome="implemented")
    final = job_store.get_job(job["id"])
    assert final["status"] == "completed", final.get("error")


def test_enforcement_can_be_disabled_only_explicitly(repo, tmp_path, monkeypatch):
    """The kill switch exists for diagnosing the policy layer itself; it is OFF by
    default, so an agent cannot reach the unenforced path by doing nothing."""
    assert job_executor.POLICY_ENFORCE is True
    monkeypatch.setattr(job_executor, "POLICY_ENFORCE", False)
    final = _run(repo, monkeypatch, tmp_path, "drop table customers from production")
    assert final["status"] != "blocked"
