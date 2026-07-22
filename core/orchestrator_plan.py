"""Persistent goal → plan → task-queue orchestrator over EXISTING agents.

Owns a DURABLE owner goal, an ordered PLAN of project tasks (priority +
dependencies), per-task assignment to an EXISTING agent, completion state, and the
next-action. Each tick it: detects completed dispatched tasks, records completion
exactly once, and dispatches the next dependency-satisfied task to its assigned
existing agent — never creating a duplicate agent and never inventing work after a
task/project is complete. With no goal it reports ORCHESTRATOR_IDLE_NO_GOAL.

Everything is stored in the shared agent_control.db so it survives a service
restart; the prompt supervisor, context rotation, safe push/commit and service-ops
policies are HELPERS invoked around this — not the orchestrator itself.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional

TASK_STATES = ("pending", "dispatched", "in_progress", "completed",
               "waiting_external", "failed", "blocked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts() -> float:
    return time.time()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_goal (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, status TEXT,
        created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_task (
        id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER, project TEXT, agent TEXT,
        title TEXT, task_text TEXT, order_index INTEGER, priority INTEGER,
        depends_on TEXT, status TEXT, handoff_path TEXT, approved_scope TEXT,
        completion_marker TEXT, next_action TEXT, note TEXT,
        dispatched_at TEXT, completed_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orchestrator_state (
        key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
    conn.commit()
    return conn


# ── goal ─────────────────────────────────────────────────────────────────────
def set_goal(text: str) -> dict:
    """Set THE durable owner goal (one active at a time). Re-uses the active row if
    the text is unchanged, so it is idempotent."""
    conn = _db()
    try:
        row = conn.execute("SELECT id,text FROM orchestrator_goal WHERE status='active' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[1] == text:
            return {"id": row[0], "text": text, "status": "active", "created": False}
        if row:
            conn.execute("UPDATE orchestrator_goal SET status='superseded', updated_at=? WHERE id=?",
                         (_now(), row[0]))
        cur = conn.execute("INSERT INTO orchestrator_goal(text,status,created_at,updated_at) "
                           "VALUES(?,?,?,?)", (text, "active", _now(), _now()))
        conn.commit()
        return {"id": cur.lastrowid, "text": text, "status": "active", "created": True}
    finally:
        conn.close()


def get_active_goal() -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute("SELECT id,text,status,created_at,updated_at FROM orchestrator_goal "
                           "WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        return dict(zip(("id", "text", "status", "created_at", "updated_at"), row))
    finally:
        conn.close()


def complete_goal(goal_id: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE orchestrator_goal SET status='complete', updated_at=? WHERE id=?",
                     (_now(), goal_id))
        conn.commit()
    finally:
        conn.close()


# ── tasks ────────────────────────────────────────────────────────────────────
_TASK_COLS = ("id", "goal_id", "project", "agent", "title", "task_text", "order_index",
              "priority", "depends_on", "status", "handoff_path", "approved_scope",
              "completion_marker", "next_action", "note", "dispatched_at", "completed_at", "updated_at")


def add_task(goal_id: int, project: str, title: str, task_text: str, *, agent: str = "",
             order_index: int = 0, priority: int = 0, depends_on: Optional[list] = None,
             status: str = "pending", handoff_path: str = "", approved_scope: str = "",
             completion_marker: str = "", note: str = "") -> int:
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO orchestrator_task(goal_id,project,agent,title,task_text,order_index,"
            "priority,depends_on,status,handoff_path,approved_scope,completion_marker,note,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (goal_id, project, agent, title, task_text, order_index, priority,
             json.dumps(depends_on or []), status, handoff_path, approved_scope,
             completion_marker, note, _now()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_tasks(goal_id: int) -> list:
    conn = _db()
    try:
        rows = conn.execute(f"SELECT {','.join(_TASK_COLS)} FROM orchestrator_task WHERE goal_id=? "
                           "ORDER BY order_index, priority DESC, id", (goal_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(_TASK_COLS, r))
            d["depends_on"] = json.loads(d["depends_on"]) if d["depends_on"] else []
            out.append(d)
        return out
    finally:
        conn.close()


def _update_task(task_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    conn = _db()
    try:
        sets = ",".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE orchestrator_task SET {sets} WHERE id=?",
                     (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def mark_dispatched(task_id: int) -> None:
    _update_task(task_id, status="dispatched", dispatched_at=_now())


def mark_completed(task_id: int, next_action: str = "") -> None:
    _update_task(task_id, status="completed", completed_at=_now(), next_action=next_action)


def mark_status(task_id: int, status: str, note: str = "") -> None:
    _update_task(task_id, status=status, note=note)


def _state_get(key: str) -> Optional[str]:
    conn = _db()
    try:
        row = conn.execute("SELECT value FROM orchestrator_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _state_set(**kv) -> None:
    conn = _db()
    try:
        for k, v in kv.items():
            conn.execute("INSERT INTO orchestrator_state(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (k, str(v), _now()))
        conn.commit()
    finally:
        conn.close()


def _deps_satisfied(task: dict, by_id: dict) -> bool:
    for dep in task.get("depends_on") or []:
        d = by_id.get(dep)
        if not d or d["status"] not in ("completed", "waiting_external"):
            return False
    return True


def default_completion(task: dict) -> bool:
    """A dispatched task is complete when its completion_marker file exists (a
    controlled, real, file-based signal the assigned agent produces)."""
    m = task.get("completion_marker")
    return bool(m) and os.path.exists(m)


def tick(*, dispatch: bool = True,
         agent_available: Optional[Callable[[str], bool]] = None,
         send: Optional[Callable[[str, str], dict]] = None,
         is_complete: Optional[Callable[[dict], bool]] = None,
         interval_secs: int = 45) -> dict:
    """One orchestration cycle. Detect completions, then dispatch the next
    dependency-satisfied task to its assigned EXISTING agent. Returns the full,
    honest orchestrator state."""
    is_complete = is_complete or default_completion
    now = _now()
    goal = get_active_goal()
    _state_set(last_tick=now, next_tick_epoch=_ts() + interval_secs)
    if not goal:
        return {"state": "ORCHESTRATOR_IDLE_NO_GOAL", "goal": None, "tasks": [],
                "last_tick": now, "reason": "no durable owner goal is set"}

    tasks = list_tasks(goal["id"])
    by_id = {t["id"]: t for t in tasks}
    completions, dispatches, blockers, idle_reasons = [], [], [], []

    # 1) detect completion of dispatched/in-progress tasks (never re-invent work).
    for t in tasks:
        if t["status"] in ("dispatched", "in_progress") and is_complete(t):
            mark_completed(t["id"], next_action=t.get("next_action") or "")
            t["status"] = "completed"
            completions.append({"task_id": t["id"], "title": t["title"], "project": t["project"]})
            _state_set(last_completion=f"{now} task#{t['id']} {t['title']}")

    # 2) dispatch the next dependency-satisfied pending task to its assigned agent.
    #    Evaluate ALL pending (so blockers/idle-reasons are honest) but dispatch at
    #    most ONE per tick (sequential, avoids overloading an agent).
    dispatched_one = False
    for t in tasks:
        if t["status"] != "pending":
            continue
        if not _deps_satisfied(t, by_id):
            blockers.append({"task_id": t["id"], "title": t["title"],
                             "reason": "waiting on dependencies", "depends_on": t["depends_on"]})
            continue
        agent = t.get("agent")
        if not agent:
            blockers.append({"task_id": t["id"], "title": t["title"], "reason": "no agent assigned"})
            continue
        if agent_available and not agent_available(agent):
            idle_reasons.append({"agent": agent, "task_id": t["id"],
                                 "reason": "assigned agent busy / not at a boundary"})
            continue
        if dispatch and not dispatched_one and send:
            res = send(agent, t["task_text"])
            if res is not None:
                mark_dispatched(t["id"])
                t["status"] = "dispatched"
                dispatched_one = True
                dispatches.append({"task_id": t["id"], "title": t["title"], "agent": agent})
                _state_set(last_dispatch=f"{now} task#{t['id']} -> {agent}")
        elif dispatched_one:
            idle_reasons.append({"agent": agent, "task_id": t["id"],
                                 "reason": "queued behind this tick's dispatch (one per tick)"})

    tasks = list_tasks(goal["id"])                       # refresh after mutations
    pending = [t for t in tasks if t["status"] in ("pending", "dispatched", "in_progress", "blocked")]
    waiting_ext = [t for t in tasks if t["status"] == "waiting_external"]
    done = [t for t in tasks if t["status"] == "completed"]
    if not pending:
        state = "ORCHESTRATOR_ALL_DISPATCHED_WAITING" if waiting_ext and not done else \
                ("GOAL_COMPLETE" if not waiting_ext else "GOAL_COMPLETE_WAITING_EXTERNAL")
        if state.startswith("GOAL_COMPLETE"):
            complete_goal(goal["id"])
    else:
        state = "ORCHESTRATING"
    return {
        "state": state, "goal": goal,
        "queue_size": len(pending),
        "assignments": [{"task_id": t["id"], "agent": t["agent"], "title": t["title"],
                         "project": t["project"], "status": t["status"],
                         "handoff_path": t["handoff_path"]} for t in tasks],
        "tasks": tasks, "completions": completions, "dispatches": dispatches,
        "blockers": blockers, "idle_reasons": idle_reasons,
        "work_remaining": [t["title"] for t in pending],
        "waiting_external": [t["title"] for t in waiting_ext],
        "completed": [t["title"] for t in done],
        "last_tick": now, "last_dispatch": _state_get("last_dispatch"),
        "last_completion": _state_get("last_completion"),
        "next_tick_epoch": float(_state_get("next_tick_epoch") or 0),
    }


def status() -> dict:
    """Read-only orchestrator plan state for Owner OS mission control (no mutation)."""
    goal = get_active_goal()
    if not goal:
        return {"state": "ORCHESTRATOR_IDLE_NO_GOAL", "goal": None, "queue_size": 0,
                "last_tick": _state_get("last_tick")}
    tasks = list_tasks(goal["id"])
    pending = [t for t in tasks if t["status"] in ("pending", "dispatched", "in_progress", "blocked")]
    return {"state": "ORCHESTRATING" if pending else "GOAL_COMPLETE", "goal": goal,
            "queue_size": len(pending),
            "assignments": [{"task_id": t["id"], "agent": t["agent"], "title": t["title"],
                             "project": t["project"], "status": t["status"]} for t in tasks],
            "work_remaining": [t["title"] for t in pending],
            "waiting_external": [t["title"] for t in tasks if t["status"] == "waiting_external"],
            "completed": [t["title"] for t in tasks if t["status"] == "completed"],
            "last_tick": _state_get("last_tick"), "next_tick_epoch": float(_state_get("next_tick_epoch") or 0),
            "last_dispatch": _state_get("last_dispatch"), "last_completion": _state_get("last_completion")}
