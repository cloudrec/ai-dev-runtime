"""Runtime job lifecycle -> Owner OS event pipeline (the bridge that was missing
when job 888f5266 / task OWNER-193 failed silently on 2026-08-15).

Covers: durable event per transition, correct wake-relevant mapping for failed /
waiting_approval / completed, notification enqueue for owner-relevant events,
event dedupe (no duplicate wake sources), and project route derivation.
"""
import sqlite3

import pytest

from core import job_store, runtime_events
from core.control_plane import store as cp_store


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    job_store.init_db()
    yield str(tmp_path / "cp.db")


def _events(db, source="runtime_jobs"):
    c = sqlite3.connect(db)
    try:
        return c.execute(
            "SELECT type, project_id, severity, owner_action_required, dedup_key "
            "FROM event WHERE source=? ORDER BY id", (source,)).fetchall()
    finally:
        c.close()


def _notifications(db):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT dedup_key, state FROM notification ORDER BY id").fetchall()
    finally:
        c.close()


def test_route_key_for_control_repo_and_projects():
    assert runtime_events.route_key_for({"project_path": "/root/ai-dev-runtime"}) == "owner-os"
    assert runtime_events.route_key_for({"project_path": "/opt/seo"}) == "seo"
    assert runtime_events.route_key_for({"project_path": "/opt/seo/"}) == "seo"
    assert runtime_events.route_key_for({"project_path": ""}) == ""


def test_lifecycle_transitions_emit_durable_events(cp):
    job = job_store.create_job(project_path="/root/ai-dev-runtime", task_id=193,
                               goal="Venture Radar", status="queued",
                               approval_required=0, autonomy_level="execute_safe")
    job_store.update_job(job["id"], status="planning")
    job_store.update_job(job["id"], status="branching")
    job_store.update_job(job["id"], status="failed",
                         error="branch failed: error: Your local changes ... "
                               "would be overwritten by checkout")
    rows = _events(cp)
    types = [r[0] for r in rows]
    assert types == ["runtime_job_state", "runtime_job_state", "runtime_job_state",
                     "task_failed"]
    failed = rows[-1]
    assert failed[1] == "owner-os"
    assert failed[2] == "high" and failed[3] == 1
    # the failure enqueued an owner notification; routine transitions did not
    notifs = _notifications(cp)
    assert len(notifs) == 1
    assert notifs[0][0] == f"runtimejob:{job['id']}:failed"


def test_waiting_approval_is_owner_decision(cp):
    job = job_store.create_job(project_path="/opt/seo", task_id=200,
                               goal="SEO validation", status="waiting_approval")
    rows = _events(cp)
    assert rows[-1][0] == "owner_decision_required"
    assert rows[-1][1] == "seo"
    assert rows[-1][3] == 1
    # approving moves it on without re-announcing the decision
    job_store.update_job(job["id"], status="queued")
    assert _events(cp)[-1][0] == "runtime_job_state"


def test_completed_emits_completion_not_owner_action(cp):
    job = job_store.create_job(project_path="/opt/seo", task_id=200,
                               goal="ok job", status="queued", approval_required=0)
    job_store.update_job(job["id"], status="completed")
    rows = _events(cp)
    assert rows[-1][0] == "task_completed"
    assert rows[-1][2] == "info" and rows[-1][3] == 0


def test_no_duplicate_event_for_replayed_transition(cp):
    job = job_store.create_job(project_path="/root/ai-dev-runtime", task_id=193,
                               goal="dup check", status="queued", approval_required=0)
    job_store.update_job(job["id"], status="failed", error="boom")
    # replay: same terminal status written again (idempotent _finish, restarts)
    job_store.update_job(job["id"], error="boom again")          # no status change
    job_store.update_job(job["id"], status="failed", error="boom")
    failed_rows = [r for r in _events(cp) if r[0] == "task_failed"]
    assert len(failed_rows) == 1
    assert len(_notifications(cp)) == 1


def test_reap_orphaned_emits_failed_event(cp):
    job = job_store.create_job(project_path="/root/ai-dev-runtime", task_id=192,
                               goal="Agent Fabric v1", status="queued",
                               approval_required=0)
    # worker died mid-planning: status non-terminal, heartbeat never written
    job_store.update_job(job["id"], status="planning")
    n = job_store.reap_orphaned()
    assert n >= 1
    rows = _events(cp)
    assert rows[-1][0] == "task_failed"
    got = job_store.get_job(job["id"])
    assert got["status"] == "failed" and "orphaned" in got["error"]


def test_pytest_without_sandbox_never_writes_live_db(monkeypatch):
    """2026-08-15 leak: a worktree repo-suite run with an old conftest (no
    CONTROL_PLANE_DB pin) imported the live hooked job_store and wrote 126
    debris events into production. Inside pytest with no sandboxed control
    plane DB, emission must refuse."""
    monkeypatch.delenv("CONTROL_PLANE_DB", raising=False)
    assert runtime_events._pytest_without_sandbox()  # PYTEST_CURRENT_TEST is set

    called = []
    monkeypatch.setattr(runtime_events, "emit_transition",
                        lambda *a, **k: called.append(1))
    runtime_events.safe_emit_transition({"id": "x", "project_path": "/tmp/r"}, "failed")
    assert not called
    # with the sandbox pinned, emission proceeds
    monkeypatch.setenv("CONTROL_PLANE_DB", "/tmp/sandbox.db")
    runtime_events.safe_emit_transition({"id": "x", "project_path": "/tmp/r"}, "failed")
    assert called


def test_emission_failure_never_breaks_job_write(cp, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("control plane down")
    monkeypatch.setattr(runtime_events, "emit_transition", _boom)
    job = job_store.create_job(project_path="/opt/seo", task_id=1,
                               goal="resilience", status="queued", approval_required=0)
    out = job_store.update_job(job["id"], status="failed", error="x")
    assert out["status"] == "failed"
