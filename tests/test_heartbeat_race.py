"""recover_interrupted() self-clobber race: a job's own generated test step
can spin up a nested FastAPI TestClient (e.g. `from api.main import app`)
against the SAME production runtime_jobs.db. That triggers the startup event
-> recover_interrupted() again, in a different process, while the job is
still genuinely running in the original process/thread. The old sweep had no
way to tell "orphaned by a crash" from "actively running elsewhere" and
clobbered the live job's status mid-flight.

Fix: job_executor pulses job_store.touch_heartbeat() every few seconds for
the life of a job; recover_interrupted() only reaps jobs whose heartbeat is
stale (or absent), regardless of which process runs the sweep."""
import os
import stat
import tempfile
import time

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_test_jobs.db"))

from core import job_executor, job_store  # noqa: E402


def setup_module(_m):
    # conftest.py points RUNTIME_DB at ONE shared temp file for the whole pytest
    # session. Removing it here raced other test modules' still-live background
    # threads (job_executor's heartbeat, or an unjoined dispatch thread) writing
    # to it mid-run -> sqlite3.OperationalError: attempt to write a readonly
    # database in unrelated files. Clearing the ROWS instead leaves the file
    # (and any other module's open connection) intact, while still giving this
    # module the same "starts empty" guarantee the old os.remove() gave it —
    # this file's exact-count assertions (recover_interrupted() == 0/1) need
    # a table with nothing left over from an earlier module.
    job_store.init_db()
    with job_store._LOCK, job_store._conn() as c:
        c.execute("DELETE FROM jobs")


def _fake_cli(tmp_path, body):
    p = tmp_path / "fake_claude.py"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_touch_heartbeat_then_recover_interrupted_leaves_job_alone():
    j = job_store.create_job(project_path="/tmp/x", goal="g", status="testing")
    job_store.touch_heartbeat(j["id"])
    n = job_store.recover_interrupted()
    assert n == 0
    assert job_store.get_job(j["id"])["status"] == "testing"


def test_stale_heartbeat_is_still_reaped():
    j = job_store.create_job(project_path="/tmp/x", goal="g", status="testing")
    with job_store._LOCK, job_store._conn() as c:
        c.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?",
                  ("2000-01-01T00:00:00+00:00", j["id"]))
    n = job_store.recover_interrupted()
    assert n == 1
    got = job_store.get_job(j["id"])
    assert got["status"] == "waiting_approval"
    assert "re-approval required" in got["error"]


def test_no_heartbeat_at_all_is_reaped_like_before():
    # unchanged legacy behavior: a job that never got a heartbeat (e.g. crash
    # right after the status write, before the pulse thread ever ticked) is
    # still treated as orphaned, not silently left running forever.
    j = job_store.create_job(project_path="/tmp/x", goal="g", status="editing")
    n = job_store.recover_interrupted()
    assert n >= 1
    assert job_store.get_job(j["id"])["status"] == "waiting_approval"


def test_execute_pulses_heartbeat_and_survives_concurrent_recover_sweep(tmp_path, monkeypatch):
    """Reproduces the actual incident: recover_interrupted() runs (as if from
    a nested process) WHILE the job is genuinely still mid-flight. With the
    heartbeat pulsing, the sweep must find nothing to reap and the job must
    go on to finish normally."""
    monkeypatch.setenv("RUNTIME_HEARTBEAT_INTERVAL_SECS", "1")
    monkeypatch.setenv("RUNTIME_HEARTBEAT_STALE_SECS", "5")
    import importlib
    importlib.reload(job_store)
    importlib.reload(job_executor)

    cli = _fake_cli(tmp_path, (
        "import sys, time, json\n"
        "time.sleep(3)\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "    'result': json.dumps({'summary': 'ok', 'files': "
        "[{'path': 'a.txt', 'operation': 'create', 'content': 'x'}]})}))\n"
    ))
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", "15")
    from core import ai_planner
    importlib.reload(ai_planner)

    job = job_store.create_job(project_path=str(tmp_path), goal="g", instructions="i",
                               autonomy_level="suggest")
    job_executor.execute_async(job["id"])

    deadline = time.monotonic() + 5
    while job_store.get_job(job["id"])["status"] != "planning" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert job_store.get_job(job["id"])["status"] == "planning"

    # simulate the nested-process sweep firing mid-flight
    n = job_store.recover_interrupted()
    assert n == 0, "live job must not be reaped just because it's mid-execution"
    assert job_store.get_job(job["id"])["status"] == "planning"

    deadline = time.monotonic() + 10
    while job_store.get_job(job["id"])["status"] not in (
            "completed", "failed", "blocked", "fallback_plan_only") \
            and time.monotonic() < deadline:
        time.sleep(0.1)
    final = job_store.get_job(job["id"])
    # This job runs at autonomy `suggest`, so it legitimately stops at a plan and
    # ends in the plan-only terminal status. What this test pins is the
    # heartbeat: the job survived the concurrent sweep and finished on its own
    # terms rather than being reaped with an error.
    assert final["status"] == "fallback_plan_only"
    assert final["error"] is None
