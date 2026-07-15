"""PHASE 13 — persistent job store (SQLite). Jobs survive service restart."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

_DB = os.getenv("RUNTIME_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime_jobs.db"))
_LOCK = threading.RLock()
# a job with a heartbeat newer than this is still being actively executed by
# *some* process (this one or another) and must not be reaped as orphaned —
# see recover_interrupted().
_HEARTBEAT_STALE_SECS = int(os.getenv("RUNTIME_HEARTBEAT_STALE_SECS", "20"))

# statuses (spec §3)
STATUSES = ["draft", "waiting_approval", "queued", "planning", "backing_up", "branching",
            "editing", "validating", "testing", "committing", "pushing", "deploying",
            "completed", "failed", "cancelled", "rolled_back", "blocked"]
_INTERRUPTIBLE = {"planning", "backing_up", "branching", "editing", "validating",
                  "testing", "committing", "pushing", "deploying"}
_JSON_FIELDS = {"constraints", "allowed_paths", "forbidden_paths", "plan", "changed_files",
                "validation", "tests", "git_info", "logs", "artifacts"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_id INTEGER, project_path TEXT, task_id INTEGER,
            goal TEXT, instructions TEXT,
            constraints TEXT, allowed_paths TEXT, forbidden_paths TEXT,
            autonomy_level TEXT DEFAULT 'prepare',
            approval_required INTEGER DEFAULT 1,
            auto_commit INTEGER DEFAULT 1, auto_push INTEGER DEFAULT 0, auto_deploy INTEGER DEFAULT 0,
            target_branch TEXT, base_branch TEXT,
            status TEXT DEFAULT 'draft', risk_level TEXT DEFAULT 'medium', dangerous INTEGER DEFAULT 0,
            plan TEXT, changed_files TEXT, validation TEXT, tests TEXT, git_info TEXT,
            logs TEXT, error TEXT, artifacts TEXT,
            created_at TEXT, started_at TEXT, finished_at TEXT, updated_at TEXT,
            heartbeat_at TEXT
        )""")
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT")
        except sqlite3.OperationalError:
            pass  # already migrated


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f in _JSON_FIELDS:
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                d[f] = None
        elif f in ("constraints", "allowed_paths", "forbidden_paths", "changed_files", "logs", "artifacts"):
            d[f] = []
        else:
            d[f] = {} if f in ("plan", "validation", "tests", "git_info") else None
    for b in ("approval_required", "auto_commit", "auto_push", "auto_deploy", "dangerous"):
        d[b] = bool(d.get(b))
    return d


def create_job(**kw) -> dict:
    job_id = str(uuid.uuid4())
    now = _now()
    fields = {
        "id": job_id, "project_id": kw.get("project_id"), "project_path": kw.get("project_path"),
        "task_id": kw.get("task_id"), "goal": kw.get("goal"), "instructions": kw.get("instructions"),
        "constraints": json.dumps(kw.get("constraints") or []),
        "allowed_paths": json.dumps(kw.get("allowed_paths") or []),
        "forbidden_paths": json.dumps(kw.get("forbidden_paths") or []),
        "autonomy_level": kw.get("autonomy_level", "prepare"),
        "approval_required": int(kw.get("approval_required", 1)),
        "auto_commit": int(kw.get("auto_commit", 1)), "auto_push": int(kw.get("auto_push", 0)),
        "auto_deploy": int(kw.get("auto_deploy", 0)),
        "target_branch": kw.get("target_branch"), "base_branch": kw.get("base_branch"),
        "status": kw.get("status", "draft"), "risk_level": kw.get("risk_level", "medium"),
        "dangerous": int(kw.get("dangerous", 0)),
        "plan": None, "changed_files": json.dumps([]), "validation": None, "tests": None,
        "git_info": None, "logs": json.dumps([]), "error": None, "artifacts": json.dumps([]),
        "created_at": now, "started_at": None, "finished_at": None, "updated_at": now,
    }
    cols = ",".join(fields.keys())
    ph = ",".join("?" * len(fields))
    with _LOCK, _conn() as c:
        c.execute(f"INSERT INTO jobs ({cols}) VALUES ({ph})", list(fields.values()))
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_dict(r) if r else None


def list_jobs(limit: int = 100, status: str | None = None) -> list[dict]:
    with _LOCK, _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_job(job_id: str, **fields) -> dict | None:
    if not fields:
        return get_job(job_id)
    sets, vals = [], []
    for k, v in fields.items():
        if k in _JSON_FIELDS:
            v = json.dumps(v)
        elif k in ("approval_required", "auto_commit", "auto_push", "auto_deploy", "dangerous"):
            v = int(bool(v))
        sets.append(f"{k}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(job_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", vals)
    return get_job(job_id)


def append_log(job_id: str, level: str, msg: str) -> None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT logs FROM jobs WHERE id=?", (job_id,)).fetchone()
        logs = json.loads(r["logs"]) if r and r["logs"] else []
        logs.append({"ts": _now(), "level": level, "msg": str(msg)[:1000]})
        c.execute("UPDATE jobs SET logs=?, updated_at=? WHERE id=?", (json.dumps(logs[-500:]), _now(), job_id))


def touch_heartbeat(job_id: str) -> None:
    """Called every few seconds by the executor thread for as long as a job is
    actively running, so recover_interrupted() (which can fire from *any*
    process that boots this same app, e.g. a planner-generated test spinning
    up its own FastAPI TestClient against the shared DB) can tell a live job
    apart from one truly orphaned by a crash."""
    with _LOCK, _conn() as c:
        c.execute("UPDATE jobs SET heartbeat_at=? WHERE id=?", (_now(), job_id))


def recover_interrupted() -> int:
    """On boot: any job left mid-execution with no recent heartbeat is truly
    orphaned (its process crashed or was killed) -> require re-approval, never
    silently resume a destructive step (spec §4). A job whose heartbeat is
    still fresh is left alone — it is still being actively executed somewhere,
    not orphaned, even if this sweep runs in a different process than the one
    driving it."""
    n = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_HEARTBEAT_STALE_SECS)).isoformat()
    with _LOCK, _conn() as c:
        rows = c.execute(
            ("SELECT id, status FROM jobs WHERE status IN (%s) "
             "AND (heartbeat_at IS NULL OR heartbeat_at < ?)") %
            ",".join("?" * len(_INTERRUPTIBLE)),
            tuple(_INTERRUPTIBLE) + (cutoff,)).fetchall()
        for r in rows:
            c.execute("UPDATE jobs SET status='waiting_approval', error=?, updated_at=? WHERE id=?",
                      (f"interrupted during '{r['status']}' by service restart — re-approval required", _now(), r["id"]))
            n += 1
    return n
