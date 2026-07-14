"""POST-REPAIR E2E CANARY — durable BusEvent + OwnerTask persistence.

Mirrors core/job_store.py's SQLite pattern so a GitHub issue ingestion can be
traced end to end: BusEvent (raw inbound event) -> OwnerTask (claimed unit of
work) -> Runtime job (core/job_store.py). No network calls, no LLM calls.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

_DB = os.getenv("RUNTIME_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime_jobs.db"))
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS bus_events (
            id TEXT PRIMARY KEY,
            source TEXT, event_type TEXT, repo TEXT, issue_number INTEGER,
            payload TEXT, created_at TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS owner_tasks (
            id TEXT PRIMARY KEY,
            bus_event_id TEXT, repo TEXT, issue_number INTEGER, title TEXT,
            status TEXT DEFAULT 'claimed', runtime_job_id TEXT,
            created_at TEXT, updated_at TEXT
        )""")


def create_bus_event(source: str, event_type: str, repo: str, issue_number: int, payload: dict | None = None) -> dict:
    event_id = str(uuid.uuid4())
    now = _now()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO bus_events (id, source, event_type, repo, issue_number, payload, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (event_id, source, event_type, repo, issue_number, json.dumps(payload or {}), now),
        )
    return get_bus_event(event_id)


def get_bus_event(event_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM bus_events WHERE id=?", (event_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
    return d


def create_owner_task(bus_event_id: str, repo: str, issue_number: int, title: str) -> dict:
    task_id = str(uuid.uuid4())
    now = _now()
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO owner_tasks (id, bus_event_id, repo, issue_number, title, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, bus_event_id, repo, issue_number, title, "claimed", now, now),
        )
    return get_owner_task(task_id)


def get_owner_task(task_id: str) -> dict | None:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM owner_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def update_owner_task(task_id: str, **fields) -> dict | None:
    if not fields:
        return get_owner_task(task_id)
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(task_id)
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE owner_tasks SET {','.join(sets)} WHERE id=?", vals)
    return get_owner_task(task_id)
