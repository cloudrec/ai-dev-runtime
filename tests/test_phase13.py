"""PHASE 13 — tests for the new autonomous-execution modules (no network)."""
import os
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_test_jobs.db"))

from core import job_store, git_write, ai_planner  # noqa: E402


def setup_module(_m):
    # fresh db
    try:
        os.remove(os.environ["RUNTIME_DB"])
    except FileNotFoundError:
        pass
    job_store.init_db()


def test_job_create_persist_get():
    j = job_store.create_job(project_path="/tmp/x", goal="g", instructions="i", autonomy_level="prepare")
    assert j["status"] == "draft" and j["autonomy_level"] == "prepare"
    got = job_store.get_job(j["id"])
    assert got and got["goal"] == "g"
    job_store.update_job(j["id"], status="queued")
    assert job_store.get_job(j["id"])["status"] == "queued"
    job_store.append_log(j["id"], "info", "hello")
    assert job_store.get_job(j["id"])["logs"][-1]["msg"] == "hello"


def test_recover_interrupted_requires_reapproval():
    j = job_store.create_job(project_path="/tmp/x", goal="g", status="editing")
    n = job_store.recover_interrupted()
    assert n >= 1
    assert job_store.get_job(j["id"])["status"] == "waiting_approval"


def test_git_slug_and_no_add_all():
    assert git_write.slug("Add a Feature!") == "add-a-feature"
    with pytest.raises(git_write.GitWriteError):
        git_write.add_paths("/tmp", [])  # refuses empty (== git add .)


def test_planner_validate_rejects_bad_paths():
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "../escape.py", "operation": "create", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": ".env", "operation": "create", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "a.py", "operation": "rm -rf", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "a.py", "operation": "create", "content": ""}]}, [])
    # allow-list enforcement
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "src/a.py", "operation": "create", "content": "x"}]}, ["utils"])
    ok = ai_planner._validate({"files": [{"path": "utils/a.py", "operation": "create", "content": "x"}]}, ["utils"])
    assert ok["files"][0]["path"] == "utils/a.py"


def test_planner_available_flag():
    assert isinstance(ai_planner.available(), bool)
