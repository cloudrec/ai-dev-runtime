"""Project specification and acceptance-criteria store.

A spec is append-only: every change is a new version with its own sha, and a task
pins the version it was cut from, so "what was asked" can never shift under a task
that is already running or under review.

Spec body (validated fail-closed, unknown keys refused like `task_contract`):
  title                str
  goal                 str            what the finished project must achieve
  acceptance_criteria  [str]          project-level, each independently checkable
  checks               [[argv...]]    commands run in the workdir; exit 0 = pass
  tasks                [task]         the initial plan (GPT may add more on review)
  scope / do_not_touch [str]          path prefixes, relative to the project root

task: {key, title, goal, acceptance_criteria: [str], checks?: [[argv]],
       scope?: [str], depends_on?: [key]}
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts


class SpecError(ValueError):
    pass


_SPEC_FIELDS = {"title": str, "goal": str, "acceptance_criteria": list,
                "checks": list, "tasks": list, "scope": list, "do_not_touch": list}
_TASK_FIELDS = {"key": str, "title": str, "goal": str, "acceptance_criteria": list,
                "checks": list, "scope": list, "depends_on": list}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_project (
    id TEXT PRIMARY KEY, name TEXT, root TEXT, executor TEXT,
    created_at TEXT, created_ts REAL
);
CREATE TABLE IF NOT EXISTS fc_spec (
    project_id TEXT, version INTEGER, body TEXT, sha TEXT, by TEXT,
    created_at TEXT, created_ts REAL,
    PRIMARY KEY (project_id, version)
)
"""


def conn_with_schema(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def _checks(v, where: str) -> list:
    if not isinstance(v, list) or not all(
            isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in v):
        raise SpecError(f"{where}.checks must be a list of argv lists")
    return v


def _strs(v, where: str, *, nonempty: bool = False) -> list:
    if not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v):
        raise SpecError(f"{where} must be a list of non-empty strings")
    if nonempty and not v:
        raise SpecError(f"{where} must not be empty")
    return v


def validate_task(t: dict, where: str = "task") -> dict:
    if not isinstance(t, dict):
        raise SpecError(f"{where} must be an object")
    unknown = set(t) - set(_TASK_FIELDS)
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {sorted(unknown)}")
    for f in ("key", "title", "goal"):
        if not isinstance(t.get(f), str) or not t[f].strip():
            raise SpecError(f"{where}.{f} is required")
    out = {"checks": [], "scope": [], "depends_on": [], **t}
    _strs(out.get("acceptance_criteria"), f"{where}.acceptance_criteria", nonempty=True)
    _checks(out["checks"], where)
    _strs(out["scope"], f"{where}.scope")
    _strs(out["depends_on"], f"{where}.depends_on")
    return out


def validate_spec(body: dict) -> dict:
    if not isinstance(body, dict):
        raise SpecError("spec must be an object")
    unknown = set(body) - set(_SPEC_FIELDS)
    if unknown:
        raise SpecError(f"unknown spec field(s): {sorted(unknown)}")
    for f in ("title", "goal"):
        if not isinstance(body.get(f), str) or not body[f].strip():
            raise SpecError(f"spec.{f} is required")
    out = {"checks": [], "tasks": [], "scope": [], "do_not_touch": [], **body}
    _strs(out.get("acceptance_criteria"), "spec.acceptance_criteria", nonempty=True)
    _checks(out["checks"], "spec")
    _strs(out["scope"], "spec.scope")
    _strs(out["do_not_touch"], "spec.do_not_touch")
    if not isinstance(out["tasks"], list):
        raise SpecError("spec.tasks must be a list")
    keys = set()
    out["tasks"] = [validate_task(t, f"spec.tasks[{i}]") for i, t in enumerate(out["tasks"])]
    for t in out["tasks"]:
        if t["key"] in keys:
            raise SpecError(f"duplicate task key {t['key']!r}")
        keys.add(t["key"])
    for t in out["tasks"]:
        missing = [d for d in t["depends_on"] if d not in keys]
        if missing:
            raise SpecError(f"task {t['key']!r} depends on unknown {missing}")
    return out


def sha(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def create_project(project_id: str, *, name: str, root: str, executor: str,
                   conn=None) -> dict:
    """Idempotent: an existing project with the same root/executor is returned as is;
    a different root or executor under the same id is refused, never overwritten."""
    conn, own = conn_with_schema(conn)
    try:
        r = conn.execute("SELECT id,name,root,executor FROM fc_project WHERE id=?",
                         (project_id,)).fetchone()
        if r:
            if (r[2], r[3]) != (root, executor):
                raise SpecError(f"project {project_id!r} exists with another root/executor")
            return {"id": r[0], "name": r[1], "root": r[2], "executor": r[3]}
        conn.execute("INSERT INTO fc_project VALUES (?,?,?,?,?,?)",
                     (project_id, name, root, executor, now_iso(), now_ts()))
        conn.commit()
        return {"id": project_id, "name": name, "root": root, "executor": executor}
    finally:
        if own:
            conn.close()


def get_project(project_id: str, conn=None) -> Optional[dict]:
    conn, own = conn_with_schema(conn)
    try:
        r = conn.execute("SELECT id,name,root,executor FROM fc_project WHERE id=?",
                         (project_id,)).fetchone()
        return dict(zip(("id", "name", "root", "executor"), r)) if r else None
    finally:
        if own:
            conn.close()


def put_spec(project_id: str, body: dict, *, by: str, conn=None) -> dict:
    """Store a new version. Re-putting the identical body returns the latest version
    instead of minting a duplicate (a retried call must not fork the spec)."""
    body = validate_spec(body)
    digest = sha(body)
    conn, own = conn_with_schema(conn)
    try:
        if not conn.execute("SELECT 1 FROM fc_project WHERE id=?", (project_id,)).fetchone():
            raise SpecError(f"no such project {project_id!r}")
        last = conn.execute("SELECT version, sha FROM fc_spec WHERE project_id=? "
                            "ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
        if last and last[1] == digest:
            return get_spec(project_id, last[0], conn=conn)
        version = (last[0] + 1) if last else 1
        conn.execute("INSERT INTO fc_spec VALUES (?,?,?,?,?,?,?)",
                     (project_id, version, json.dumps(body), digest, by, now_iso(), now_ts()))
        conn.commit()
        return get_spec(project_id, version, conn=conn)
    finally:
        if own:
            conn.close()


def get_spec(project_id: str, version: Optional[int] = None, conn=None) -> Optional[dict]:
    conn, own = conn_with_schema(conn)
    try:
        q = "SELECT version, body, sha, by, created_at FROM fc_spec WHERE project_id=?"
        args: list = [project_id]
        if version is not None:
            q += " AND version=?"
            args.append(version)
        r = conn.execute(q + " ORDER BY version DESC LIMIT 1", args).fetchone()
        if not r:
            return None
        return {"project_id": project_id, "version": r[0], "body": json.loads(r[1]),
                "sha": r[2], "by": r[3], "created_at": r[4]}
    finally:
        if own:
            conn.close()
