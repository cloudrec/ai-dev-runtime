"""Runtime watchdog — stall detection fixtures from the 2026-08-15 incident.

Jobs 54a8a047 (task OWNER-192, Agent Fabric) and 43c0888c (task OWNER-200, SEO
GEO/AEO) sat in `planning` while the owner's OS showed worker.pid=null and
heartbeat_at=null — execution is delegated, there IS no local pid, and nothing
watched the job store. These tests pin: stall only on evidence, exactly one
emission per episode, re-arm on life, restart persistence, and the positive
controls (live progress, true waiting_approval) that must never alert.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core import runtime_watchdog as rw


def _iso(now, ago_secs):
    return (datetime.fromtimestamp(now, timezone.utc)
            - timedelta(seconds=ago_secs)).isoformat()


NOW = 1_700_000_000.0


def _job(status="planning", hb_ago=None, up_ago=30, jid="job-74", task=192,
         path="/root/ai-dev-runtime"):
    return {"id": jid, "task_id": task, "project_path": path, "status": status,
            "goal": "Agent Fabric v1",
            "heartbeat_at": None if hb_ago is None else _iso(NOW, hb_ago),
            "updated_at": None if up_ago is None else _iso(NOW, up_ago)}


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "cp.db"))
    yield c
    c.close()


class Emitter:
    def __init__(self):
        self.events = []

    def __call__(self, source, etype, **kw):
        self.events.append({"source": source, "type": etype, **kw})
        return {"event_id": len(self.events)}


# ── the pure verdict ────────────────────────────────────────────────────────

def test_planning_with_no_heartbeat_and_stale_updates_is_stalled():
    v = rw.stall_evidence(_job(hb_ago=None, up_ago=600), NOW)
    assert v and v["reason"] == "no_heartbeat_in_execution_stage"


def test_planning_with_fresh_heartbeat_is_not_stalled():
    # positive control: a live worker pulses every ~5s
    assert rw.stall_evidence(_job(hb_ago=6, up_ago=600), NOW) is None


def test_planning_with_recent_update_is_not_stalled():
    # updated_at moves on every log line; recent movement is life
    assert rw.stall_evidence(_job(hb_ago=None, up_ago=30), NOW) is None


def test_waiting_approval_is_never_a_stall():
    # a true owner decision — announced once by the lifecycle bridge, not here
    assert rw.stall_evidence(_job(status="waiting_approval", hb_ago=None,
                                  up_ago=90_000), NOW) is None


def test_queued_never_picked_up_is_stalled():
    v = rw.stall_evidence(_job(status="queued", hb_ago=None, up_ago=700), NOW)
    assert v and v["reason"] == "queued_never_picked_up"


def test_queued_recent_is_not_stalled():
    assert rw.stall_evidence(_job(status="queued", hb_ago=None, up_ago=60), NOW) is None


# ── the scan: dedupe, re-arm, persistence ───────────────────────────────────

def test_scan_emits_once_then_dedupes(conn):
    em = Emitter()
    jobs = [_job(hb_ago=None, up_ago=600)]
    r1 = rw.scan(jobs=jobs, emit_fn=em, conn=conn, now=NOW)
    assert len(r1["emitted"]) == 1
    ev = em.events[0]
    assert ev["type"] == "runtime_job_stalled"
    assert ev["severity"] == "high" and ev["owner_action_required"]
    assert ev["project_id"] == "owner-os"
    assert ev["dedup_key"] == "runtimejob:job-74:stalled:planning"
    r2 = rw.scan(jobs=jobs, emit_fn=em, conn=conn, now=NOW + 30)
    assert not r2["emitted"]
    assert any(s["why"] == "already_notified" for s in r2["skipped"])


def test_resumed_heartbeat_rearms_then_new_stall_emits_again(conn):
    em = Emitter()
    rw.scan(jobs=[_job(hb_ago=None, up_ago=600)], emit_fn=em, conn=conn, now=NOW)
    assert len(em.events) == 1
    # worker came back: fresh heartbeat -> no stall, episode consumed
    alive = dict(_job(), heartbeat_at=_iso(NOW + 60, 3), updated_at=_iso(NOW + 60, 3))
    r = rw.scan(jobs=[alive], emit_fn=em, conn=conn, now=NOW + 60)
    assert not r["emitted"]
    # it dies again later: a NEW episode announces
    rw.scan(jobs=[_job(hb_ago=500, up_ago=500)], emit_fn=em, conn=conn, now=NOW + 1200)
    assert len(em.events) == 2


def test_state_survives_restart(tmp_path):
    """The dedupe memory is the DB, not the process."""
    db = str(tmp_path / "cp.db")
    em = Emitter()
    c1 = sqlite3.connect(db)
    rw.scan(jobs=[_job(hb_ago=None, up_ago=600)], emit_fn=em, conn=c1, now=NOW)
    c1.commit()
    c1.close()
    c2 = sqlite3.connect(db)
    r = rw.scan(jobs=[_job(hb_ago=None, up_ago=630)], emit_fn=em, conn=c2, now=NOW + 30)
    c2.close()
    assert not r["emitted"]
    assert len(em.events) == 1


def test_reminder_after_interval(conn, monkeypatch):
    em = Emitter()
    rw.scan(jobs=[_job(hb_ago=None, up_ago=600)], emit_fn=em, conn=conn, now=NOW)
    later = NOW + rw.REMINDER_SECS + 1
    stale = dict(_job(), updated_at=_iso(later, 600 + rw.REMINDER_SECS))
    r = rw.scan(jobs=[stale], emit_fn=em, conn=conn, now=later)
    assert len(r["emitted"]) == 1 and len(em.events) == 2


def test_seo_job_routes_to_seo_key(conn):
    em = Emitter()
    rw.scan(jobs=[_job(jid="job-76", task=200, path="/opt/seo",
                       hb_ago=None, up_ago=600)], emit_fn=em, conn=conn, now=NOW)
    assert em.events[0]["project_id"] == "seo"
