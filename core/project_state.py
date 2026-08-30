"""Durable per-project terminal/work state.

v1 limitation this removes: terminal classification was read from the visible pane, so a
finished project was resumed again as soon as its completion text scrolled out of the
capture window. Terminal must be a FACT about the project, not about what happens to be
on screen.

A terminal marker is written only from verified evidence and then stays put — across pane
scroll, across a service restart, across conversation churn — until a MATERIAL project
signal changes:

  * git HEAD moved in the project,
  * the declared report / current-task fingerprint changed,
  * the owner issued a command (explicit reopen),
  * a new task was explicitly queued,
  * the freshness deadline expired.

Pane text scrolling is deliberately NOT in that list.

Fail-closed: a corrupt or unreadable store yields "no terminal marker", i.e. the loop
keeps working the project rather than silently treating it as finished.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

TERMINAL_STATUSES = ("terminal_pass", "terminal_blocked")
# How long a terminal marker is trusted without re-verification (owner-tunable).
DEFAULT_FRESHNESS_SECS = int(os.getenv("PROJECT_STATE_FRESHNESS_SECS", str(24 * 3600)))


def _db_path() -> str:
    return os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS project_state (
        target TEXT, cwd TEXT, conversation_id TEXT,
        status TEXT, reason TEXT, evidence_fp TEXT, git_head TEXT,
        report_path TEXT, report_mtime REAL, decided_at TEXT, decided_ts REAL,
        freshness_secs REAL,
        PRIMARY KEY (target, cwd))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_state_audit (
        ts TEXT, target TEXT, cwd TEXT, action TEXT, status TEXT, reason TEXT,
        detail TEXT)""")
    conn.commit()
    return conn


def _audit(conn, target: str, cwd: str, action: str, status: str, reason: str,
           detail: Optional[dict] = None) -> None:
    try:
        conn.execute("INSERT INTO project_state_audit VALUES (?,?,?,?,?,?,?)",
                     (_now_iso(), target, cwd, action, status, reason,
                      json.dumps(detail or {})[:1000]))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def git_head(cwd: str) -> str:
    """Current HEAD of the project, or '' when it is not a repo / unreadable."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                             capture_output=True, timeout=10)
        sha = out.stdout.decode().strip()
        return sha if out.returncode == 0 and len(sha) == 40 else ""
    except Exception:  # noqa: BLE001
        return ""


def evidence_fingerprint(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def report_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        return 0.0


def record_terminal(target: str, cwd: str, *, status: str, reason: str,
                    conversation_id: str = "", evidence: str = "",
                    report_path: str = "", freshness_secs: Optional[float] = None,
                    conn=None) -> dict:
    """Persist a terminal marker. Only a verified terminal status is accepted."""
    if status not in TERMINAL_STATUSES:
        return {"recorded": False, "reason": f"not_a_terminal_status:{status}"}
    own = conn is None
    conn = conn or _db()
    try:
        head = git_head(cwd)
        rpm = report_mtime(report_path) if report_path else 0.0
        fp = evidence_fingerprint(evidence, head, f"{rpm:.0f}")
        row = (target, cwd, conversation_id, status, reason, fp, head, report_path, rpm,
               _now_iso(), time.time(),
               float(freshness_secs if freshness_secs is not None else DEFAULT_FRESHNESS_SECS))
        conn.execute("INSERT OR REPLACE INTO project_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        conn.commit()
        _audit(conn, target, cwd, "record_terminal", status, reason,
               {"git_head": head[:12], "evidence_fp": fp, "report_path": report_path})
        return {"recorded": True, "status": status, "evidence_fp": fp, "git_head": head}
    finally:
        if own:
            conn.close()


def get_state(target: str, cwd: str, conn=None) -> Optional[dict]:
    """The stored marker, or None. FAIL-CLOSED: any corruption reads as 'no marker', so
    the loop keeps working the project instead of believing it is finished."""
    own = conn is None
    try:
        conn = conn or _db()
    except Exception:  # noqa: BLE001
        return None
    try:
        r = conn.execute(
            "SELECT target,cwd,conversation_id,status,reason,evidence_fp,git_head,"
            "report_path,report_mtime,decided_at,decided_ts,freshness_secs "
            "FROM project_state WHERE target=? AND cwd=?", (target, cwd)).fetchone()
        if not r:
            return None
        d = dict(zip(("target", "cwd", "conversation_id", "status", "reason", "evidence_fp",
                      "git_head", "report_path", "report_mtime", "decided_at", "decided_ts",
                      "freshness_secs"), r))
        if d["status"] not in TERMINAL_STATUSES or not d.get("decided_ts"):
            _audit(conn, target, cwd, "corrupt_state_ignored", str(d.get("status")), "fail_closed")
            return None
        return d
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def material_change(state: dict, *, cwd: str = "", owner_command: bool = False,
                    new_queued_task: bool = False, now: Optional[float] = None) -> dict:
    """Has a MATERIAL project signal changed since the terminal marker was written?

    Pane scroll is not a signal and cannot appear here — this function never looks at
    pane text.
    """
    now = now if now is not None else time.time()
    if owner_command:
        return {"reopen": True, "reason": "owner_command"}
    if new_queued_task:
        return {"reopen": True, "reason": "new_queued_task"}
    head_now = git_head(cwd or state.get("cwd") or "")
    if state.get("git_head") and head_now and head_now != state["git_head"]:
        return {"reopen": True, "reason": "git_head_changed",
                "from": state["git_head"][:12], "to": head_now[:12]}
    rp = state.get("report_path") or ""
    if rp:
        m = report_mtime(rp)
        if m and float(state.get("report_mtime") or 0) and m > float(state["report_mtime"]):
            return {"reopen": True, "reason": "report_updated"}
    fresh = float(state.get("freshness_secs") or DEFAULT_FRESHNESS_SECS)
    if fresh > 0 and (now - float(state.get("decided_ts") or 0)) > fresh:
        return {"reopen": True, "reason": "freshness_deadline_passed"}
    return {"reopen": False, "reason": "no_material_change"}


def reopen(target: str, cwd: str, reason: str, conn=None) -> bool:
    own = conn is None
    conn = conn or _db()
    try:
        conn.execute("DELETE FROM project_state WHERE target=? AND cwd=?", (target, cwd))
        conn.commit()
        _audit(conn, target, cwd, "reopen", "", reason)
        return True
    finally:
        if own:
            conn.close()


def readout(target: str = "", conn=None) -> list:
    """CLI/API readout of stored markers (all, or one target)."""
    own = conn is None
    try:
        conn = conn or _db()
    except Exception:  # noqa: BLE001
        return []
    try:
        q = ("SELECT target,cwd,status,reason,git_head,evidence_fp,decided_at,freshness_secs "
             "FROM project_state")
        args = ()
        if target:
            q += " WHERE target=?"
            args = (target,)
        cols = ("target", "cwd", "status", "reason", "git_head", "evidence_fp", "decided_at",
                "freshness_secs")
        return [dict(zip(cols, r)) for r in conn.execute(q, args)]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def audit_trail(target: str = "", limit: int = 50, conn=None) -> list:
    own = conn is None
    try:
        conn = conn or _db()
    except Exception:  # noqa: BLE001
        return []
    try:
        q = "SELECT ts,target,cwd,action,status,reason,detail FROM project_state_audit"
        args = []
        if target:
            q += " WHERE target=?"
            args.append(target)
        q += " ORDER BY rowid DESC LIMIT ?"
        args.append(limit)
        cols = ("ts", "target", "cwd", "action", "status", "reason", "detail")
        return [dict(zip(cols, r)) for r in conn.execute(q, args)]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
