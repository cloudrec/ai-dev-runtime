"""What a new ChatGPT chat needs to carry on as tech lead, in one call.

A ChatGPT conversation has a length cap. The work does not live in the chat: specs, tasks,
reviews and commits are all here. `brief()` returns that state, and the tech lead's own
notes (`add_note`) carry what only the chat knew: plans, architecture choices, what was
agreed with the owner. A chat that replaces a full one starts by reading this.
"""
from __future__ import annotations

import time
from typing import Optional

from core.fullcycle import spec as specs

NOTE_TOPIC_MAX = 80
NOTE_TEXT_MAX = 4000
NOTES_KEPT = 500
BRIEF_NOTES = 60
DONE_SHOWN = 8

_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_techlead_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, by TEXT, topic TEXT, text TEXT
)"""


class BriefError(Exception):
    pass


def _conn(conn=None):
    from core.fullcycle import engine as fe
    conn, own = fe.connect(conn)
    conn.execute(_NOTES_SCHEMA)
    return conn, own


def add_note(topic: str, text: str, *, by: str, conn=None, now: Optional[float] = None) -> dict:
    topic, text = (topic or "").strip(), (text or "").strip()
    if not topic or len(topic) > NOTE_TOPIC_MAX:
        raise BriefError(f"topic: 1-{NOTE_TOPIC_MAX} characters")
    if not text or len(text) > NOTE_TEXT_MAX:
        raise BriefError(f"text: 1-{NOTE_TEXT_MAX} characters")
    if not by.startswith("api:"):
        raise BriefError("notes need an authenticated client")
    conn, own = _conn(conn)
    try:
        cur = conn.execute("INSERT INTO fc_techlead_note (ts, by, topic, text) VALUES (?,?,?,?)",
                           (now if now is not None else time.time(), by, topic, text))
        conn.execute("DELETE FROM fc_techlead_note WHERE id <= ("
                     "SELECT id FROM fc_techlead_note ORDER BY id DESC LIMIT 1 OFFSET ?)",
                     (NOTES_KEPT,))
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        if own:
            conn.close()


def notes(limit: int = BRIEF_NOTES, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        rows = conn.execute("SELECT id, ts, topic, text FROM fc_techlead_note ORDER BY id DESC "
                            "LIMIT ?", (max(1, min(int(limit), NOTES_KEPT)),)).fetchall()
    finally:
        if own:
            conn.close()
    return [{"id": r[0], "at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(r[1] or 0)),
             "topic": r[2], "text": r[3]} for r in reversed(rows)]


def _project(pid: str, conn) -> dict:
    from core.fullcycle import engine as fe
    from core.fullcycle import intake
    st = fe.project_status(pid, conn=conn)
    s = specs.get_spec(pid, conn=conn) or {}
    body = s.get("body") or {}
    src = intake.source_of(pid, conn=conn)
    out = {"project": pid, "title": (st.get("project") or {}).get("name"),
           "done": st.get("done"), "spec_version": st.get("spec_version"),
           "goal": (body.get("goal") or "")[:600] if isinstance(body, dict) else "",
           "tasks": [{"key": t["key"], "state": t["state"], "attempt": t["attempt"],
                      **({"last_reason": t["last_reason"][:160]} if t.get("last_reason") else {})}
                     for t in st.get("tasks") or []],
           "pending_reviews": [{"review_id": r.get("review_id") or r.get("id"),
                                "kind": r.get("kind"),
                                "task": (r.get("payload") or {}).get("task") or r.get("task_id")}
                               for r in st.get("pending_reviews") or []]}
    if src:
        out.update(repo=src["repo"], branch=src["branch"], base_sha=src["base_sha"][:12])
    return out


def brief(conn=None) -> dict:
    """Every open project in full, the last few finished ones, and the notes."""
    conn, own = _conn(conn)
    try:
        rows = conn.execute("SELECT id FROM fc_project ORDER BY rowid").fetchall()
        done = {r[0]: r[1] for r in conn.execute("SELECT project_id, ts FROM fc_project_done")}
        open_ids = [r[0] for r in rows if r[0] not in done]
        recent = sorted((p for p in done), key=lambda p: done[p] or 0, reverse=True)[:DONE_SHOWN]
        out = {"open_projects": [], "recently_finished": [], "notes": notes(conn=conn),
               "how_to_continue": (
                   "You are the Owner OS tech lead. This is the state kept by Owner OS, not by "
                   "any chat. Pending reviews wait for fullcycle_review_decision; open "
                   "projects continue on their own. Record plans and decisions with "
                   "techlead_note_add so the next chat has them too.")}
        for pid in open_ids:
            try:
                out["open_projects"].append(_project(pid, conn))
            except Exception as e:  # noqa: BLE001 — one broken project must not hide the rest
                out["open_projects"].append({"project": pid, "error": str(e)[:160]})
        for pid in recent:
            try:
                p = _project(pid, conn)
                out["recently_finished"].append({k: p[k] for k in ("project", "title", "tasks")
                                                 if k in p} | ({"repo": p["repo"],
                                                                "branch": p["branch"]}
                                                               if "repo" in p else {}))
            except Exception as e:  # noqa: BLE001
                out["recently_finished"].append({"project": pid, "error": str(e)[:160]})
        return out
    finally:
        if own:
            conn.close()
