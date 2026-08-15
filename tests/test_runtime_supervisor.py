"""Bounded supervisor recovery: retry transport/environment failures, preserve
lineage and idempotency, never touch owner decisions, never duplicate jobs."""
import sqlite3

import pytest

from core import runtime_supervisor as rs

NOW = 1_700_000_000.0
_TS = "2023-11-14T22:13:20+00:00"       # == NOW as iso


def _failed(jid="f1", task=193, error="branch failed: error: Your local changes to "
            "the following files would be overwritten by checkout", **kw):
    j = {"id": jid, "task_id": task, "status": "failed", "error": error,
         "project_path": "/root/ai-dev-runtime", "goal": "Venture Radar",
         "instructions": "do it", "autonomy_level": "execute_safe",
         "approval_required": False, "auto_commit": True, "auto_push": False,
         "base_branch": "master", "finished_at": _TS, "created_at": _TS,
         "project_id": 28}
    j.update(kw)
    return j


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


class Creator:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, job, marker):
        self.calls.append((job, marker))
        if not self.ok:
            return {"ok": False, "reason": "api_unreachable:test"}
        return {"ok": True, "job": {"id": f"retry-of-{job['id']}"}}


def _noop_emit(*a, **k):
    return {"event_id": 1}


def test_classify_failure():
    assert rs.classify_failure("branch failed: ... would be overwritten by checkout ...") == "dirty_checkout"
    assert rs.classify_failure("worker crashed during 'testing': boom") == "worker_crash"
    assert rs.classify_failure("orphaned: no heartbeat for >20s during 'planning'") == "orphaned"
    assert rs.classify_failure("tests failed after repair attempts") == ""
    assert rs.classify_failure("policy deny: owner gate") == ""


def test_dirty_checkout_retries_once_with_lineage_marker(conn):
    cr = Creator()
    r = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=cr,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 60)
    assert r["retried"] and r["retry_job_id"] == "retry-of-f1"
    job, marker = cr.calls[0]
    assert "supervisor-retry of runtime job f1" in marker
    assert "dirty_checkout" in marker
    # approval preserved verbatim — never silently promoted
    assert job["approval_required"] is False
    # second pass: the claim row blocks a duplicate
    r2 = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=cr,
                              isolation_active=True, emit_fn=_noop_emit,
                              conn=conn, now=NOW + 120)
    assert not r2["retried"] and r2["reason"] == "already_retried_this_failure"
    assert len(cr.calls) == 1


def test_dirty_checkout_not_retried_without_isolation(conn):
    cr = Creator()
    r = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=cr,
                             isolation_active=False, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 60)
    assert not r["retried"]
    assert r["reason"] == "isolation_not_active_retry_would_repeat_failure"
    assert not cr.calls


def test_result_failures_are_never_retried(conn):
    cr = Creator()
    r = rs.consider_recovery(_failed(error="tests failed after repair attempts"),
                             all_jobs=[], create_fn=cr, isolation_active=True,
                             emit_fn=_noop_emit, conn=conn, now=NOW + 60)
    assert not r["retried"] and r["reason"] == "failure_class_not_retryable"


def test_active_peer_job_blocks_retry(conn):
    cr = Creator()
    peer = _failed(jid="f2", status="planning")
    r = rs.consider_recovery(_failed(), all_jobs=[_failed(), peer], create_fn=cr,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 60)
    assert not r["retried"] and r["reason"] == "task_has_active_job"


def test_newer_job_supersedes_failure(conn):
    cr = Creator()
    newer = _failed(jid="f3", status="completed",
                    created_at="2023-11-14T23:00:00+00:00")
    r = rs.consider_recovery(_failed(), all_jobs=[_failed(), newer], create_fn=cr,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 7200)
    assert not r["retried"] and r["reason"] == "newer_job_supersedes_failure"


def test_task_retry_budget_is_bounded(conn):
    cr = Creator()
    rs.consider_recovery(_failed(jid="a"), all_jobs=[_failed(jid="a")], create_fn=cr,
                         isolation_active=True, emit_fn=_noop_emit, conn=conn, now=NOW + 60)
    r = rs.consider_recovery(_failed(jid="b"), all_jobs=[_failed(jid="b")], create_fn=cr,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 120)
    assert not r["retried"] and r["reason"] == "task_retry_budget_exhausted"
    assert len(cr.calls) == 1


def test_stale_failures_are_history_not_work(conn):
    cr = Creator()
    r = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=cr,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + rs.RECENT_SECS + 1)
    assert not r["retried"] and r["reason"] == "failure_too_old"


def test_api_failure_leaves_no_claim_so_next_scan_retries(conn):
    bad = Creator(ok=False)
    r = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=bad,
                             isolation_active=True, emit_fn=_noop_emit,
                             conn=conn, now=NOW + 60)
    assert not r["retried"]
    good = Creator()
    r2 = rs.consider_recovery(_failed(), all_jobs=[_failed()], create_fn=good,
                              isolation_active=True, emit_fn=_noop_emit,
                              conn=conn, now=NOW + 120)
    assert r2["retried"]


def test_scan_only_touches_failed_jobs(conn):
    cr = Creator()
    jobs = [_failed(jid="ok1", task=100, status="completed", error=""),
            _failed(jid="wa", task=101, status="waiting_approval", error=""),
            _failed()]
    r = rs.scan(jobs=jobs, create_fn=cr, isolation_active=True,
                emit_fn=_noop_emit, conn=conn, now=NOW + 60)
    assert [x["job_id"] for x in r["retried"]] == ["f1"]
    assert len(cr.calls) == 1
