"""Was a new task SUBMITTED to this agent after a given moment? Read-only, bounded.

agent_watch parks a stop that its own stage_done already announced. A task given to the
agent after that stage makes the stop news again. API deliveries say so themselves
(`note_task_delivered`); a task typed straight into tmux leaves only one durable trace:
the session transcript, where Claude Code writes a `user` entry with
`origin.kind == "human"` exactly when a prompt is submitted (never for a draft, the
suggested prompt, a tool result, a task notification, a compaction summary or a meta
entry).

Three answers, never more confident than the evidence:
  task     a prompt after `since` that no automated sender typed (ts = the newest one);
  none     the transcript after `since` was read completely and holds no such prompt;
  unknown  the evidence is not available: no session for the pane, no transcript, more
           than SCAN_BUDGET_BYTES written since, or a prompt that an unfingerprinted
           delivery may have typed. The caller announces the stop once and does not
           re-wake it; it never parks it silently and never guesses a task.

Attribution is by CONTENT, not by clock. Every agent_control delivery stores the sha256 of
the exact text it typed (`delivery_provenance`). Claude Code records a long paste wrapped
in a `<pasted_content id=…>` block, so the prompt is hashed as typed and unwrapped, in
memory only; the text is never stored, logged or returned. A prompt whose hash matches a
delivery belongs to that delivery's caller (API: a task; anyone else: an internal nudge,
#338). A human prompt typed seconds after a nudge no longer collides with it.

Identity: the pane's Claude pid -> the runtime's session list (core.native_sessions), else
Claude Code's own per-pid record (`~/.claude/sessions/<pid>.json`), else the identity this
module last resolved for the same pid, if that transcript was written after `since`. The
resolved identity and a scan checkpoint (byte offset + its timestamp) are kept in
agent_watch_state, so a restart resumes without re-reading and a /clear (a new session id)
resets them.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
from typing import Optional

SCAN_BUDGET_BYTES = int(os.getenv("TASK_EVIDENCE_SCAN_BUDGET_BYTES", str(16 * 1024 * 1024)))
PROBE_BYTES = 256 * 1024
ATTRIBUTION_WINDOW_SECS = int(os.getenv("TASK_EVIDENCE_ATTRIBUTION_WINDOW_SECS", "600"))
UNFINGERPRINTED_WINDOW_SECS = int(os.getenv("AGENT_WATCH_DELIVERY_MATCH_SECS", "90"))

_TS_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]{10,40})"')
_WRAP_RE = re.compile(r'^\s*<pasted_content id="([^"]+)">\n?(.*?)\n?</pasted_content id="\1">\s*$',
                      re.S)

STATE_COLUMNS = (("tr_pid", "INTEGER"), ("tr_sid", "TEXT"), ("tr_offset", "INTEGER"),
                 ("tr_offset_ts", "REAL"))


def _epoch(value) -> Optional[float]:
    try:
        v = value.decode() if isinstance(value, bytes) else str(value)
        return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _fingerprints(text: str) -> set:
    forms = {text, text.strip()}
    m = _WRAP_RE.match(text)
    if m:
        forms |= {m.group(2), m.group(2).strip()}
    return {hashlib.sha256(f.encode("utf-8")).hexdigest() for f in forms}


# ── identity ─────────────────────────────────────────────────────────────────────────────
def _pidfile_session(pid: int) -> str:
    try:
        from core import agent_privilege as _ap
        path = os.path.join(_ap.claude_home(), ".claude", "sessions", f"{int(pid)}.json")
        with _ap.open_regular(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return str(d.get("sessionId") or "") if int(d.get("pid") or 0) == int(pid) else ""
    except Exception:  # noqa: BLE001 — no record: no opinion
        return ""


def _transcript(cwd: str, sid: str) -> str:
    try:
        from core import agent_privilege as _ap
        return next((p for p in _ap.transcripts(cwd)
                     if os.path.basename(p) == sid + ".jsonl"), "")
    except Exception:  # noqa: BLE001
        return ""


def _state(conn, target: str) -> tuple:
    try:
        r = conn.execute("SELECT tr_pid, tr_sid, tr_offset, tr_offset_ts FROM "
                         "agent_watch_state WHERE target=?", (target,)).fetchone()
        return tuple(r) if r else (None, None, None, None)
    except Exception:  # noqa: BLE001
        return (None, None, None, None)


def resolve_session(agent: dict, target: str, since: float, conn) -> tuple:
    """(session id, source) or ("", reason)."""
    pid = agent.get("claude_pid") or agent.get("pid")
    if not pid:
        return "", "no_pid"
    cwd = agent.get("claude_cwd") or agent.get("cwd") or ""
    sid, source = "", ""
    try:
        from core import native_sessions as _ns
        sid, source = _ns.session_id_for_pid(pid) or "", "runtime"
    except Exception:  # noqa: BLE001
        sid = ""
    if not sid:
        sid, source = _pidfile_session(pid), "pidfile"
    st_pid, st_sid = _state(conn, target)[:2]
    if not sid and st_sid and st_pid == int(pid):
        path = _transcript(cwd, st_sid)
        try:
            fresh = bool(path) and os.path.getmtime(path) >= since
        except OSError:
            fresh = False
        if fresh:
            sid, source = st_sid, "checkpoint"
    if not sid:
        return "", "session_unresolved"
    if (st_sid, st_pid) != (sid, int(pid)):
        conn.execute("UPDATE agent_watch_state SET tr_pid=?, tr_sid=?, tr_offset=NULL, "
                     "tr_offset_ts=NULL WHERE target=?", (int(pid), sid, target))
    return sid, source


# ── bounded reading ──────────────────────────────────────────────────────────────────────
def _first_ts_at(fh, offset: int) -> Optional[tuple]:
    """(line start, timestamp) of the first complete timestamped line at/after offset."""
    fh.seek(offset)
    blob = fh.read(PROBE_BYTES)
    pos = 0
    if offset:
        nl = blob.find(b"\n")
        if nl < 0:
            return None
        pos = nl + 1
    while True:
        nl = blob.find(b"\n", pos)
        if nl < 0:
            return None
        m = _TS_RE.search(blob, pos, nl)
        t = _epoch(m.group(1)) if m else None
        if t is not None:
            return offset + pos, t
        pos = nl + 1


def _start_offset(fh, size: int, since: float, ckpt: tuple) -> int:
    """A line start at or before the first line written after `since`."""
    off, off_ts = ckpt
    if off is not None and off_ts is not None and off <= size and off_ts <= since:
        lo = int(off)
    else:
        lo = 0
    hi = size
    while hi - lo > PROBE_BYTES:
        mid = (lo + hi) // 2
        hit = _first_ts_at(fh, mid)
        if hit is None or hit[1] >= since:
            hi = mid
        else:
            lo = hit[0]
    return lo


def submitted_prompts(path: str, since: float, *, conn=None, target: str = "") -> dict:
    """{"complete": bool, "prompts": [(ts, {sha256…}), …]} for prompts after `since`."""
    from core import agent_privilege as _ap
    out = {"complete": False, "prompts": []}
    try:
        size = os.path.getsize(path)
        ckpt = _state(conn, target)[2:] if conn is not None else (None, None)
        with _ap.open_regular(path, "rb") as fh:
            start = _start_offset(fh, size, since, ckpt)
            fh.seek(start)
            blob = fh.read(min(size - start, SCAN_BUDGET_BYTES))
    except Exception:  # noqa: BLE001 — unreadable: incomplete
        return out
    end = blob.rfind(b"\n") + 1
    pos, safe_off, safe_ts, past = 0, start, None, False
    for raw in blob[:end].split(b"\n"):
        line_start, pos = pos, pos + len(raw) + 1
        if not raw:
            continue
        m = _TS_RE.search(raw)
        t_line = _epoch(m.group(1)) if m else None
        if not past and t_line is not None:
            if t_line <= since:
                safe_off, safe_ts = start + line_start, t_line
            else:
                past = True
        if b'"human"' not in raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — a torn line
            continue
        content = (d.get("message") or {}).get("content")
        if (d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary")
                or not isinstance(content, str)
                or (d.get("origin") or {}).get("kind") != "human"):
            continue
        t = _epoch(d.get("timestamp"))
        if t is not None and t > since:
            out["prompts"].append((t, _fingerprints(content)))
    out["complete"] = start + len(blob) >= size
    if conn is not None and target and safe_ts is not None:
        # a line start whose own time is not after `since`: always safe to resume from
        conn.execute("UPDATE agent_watch_state SET tr_offset=?, tr_offset_ts=? "
                     "WHERE target=?", (safe_off, safe_ts, target))
    return out


# ── attribution ──────────────────────────────────────────────────────────────────────────
def _deliveries_near(target: str, ts: float) -> Optional[list]:
    """[(created_ts, text_sha256 or None, caller class)] to this session, or None when the
    delivery store cannot be read."""
    try:
        from core import agent_control as _ac
        session = target.split(":", 1)[0]
        like = (session.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + ":%")
        db = sqlite3.connect(f"file:{_ac._db_path()}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute(
                "SELECT d.created_ts, p.text_sha256, a.actor, a.source FROM deliveries d "
                "LEFT JOIN delivery_provenance p ON p.idempotency_key = d.idempotency_key "
                "LEFT JOIN delivery_attribution a ON a.idempotency_key = d.idempotency_key "
                "WHERE (d.target=? OR d.target LIKE ? ESCAPE '\\') "
                "AND d.action != 'agent_stop' AND d.created_ts BETWEEN ? AND ?",
                (target, like, ts - ATTRIBUTION_WINDOW_SECS,
                 ts + ATTRIBUTION_WINDOW_SECS)).fetchall()
        finally:
            db.close()
        return [(float(r[0]), r[1], _ac.caller_class(r[2], r[3])) for r in rows]
    except Exception:  # noqa: BLE001
        return None


def attribute(target: str, ts: float, fingerprints: set) -> str:
    """'api' / 'internal' (an automated sender typed it), 'human', or 'unknown'."""
    rows = _deliveries_near(target, ts)
    if rows is None:
        return "unknown"
    for _t, sha, cls in rows:
        if sha and sha in fingerprints:
            return "api" if cls == "api" else "internal"
    if any(sha is None and abs(t - ts) <= UNFINGERPRINTED_WINDOW_SECS for t, sha, _ in rows):
        return "unknown"            # something typed then, and we cannot tell what
    return "human"


def task_since(agent: dict, target: str, since: float, conn) -> dict:
    """{"verdict": "task"|"none"|"unknown", "ts": float|None, "reason": str}."""
    sid, how = resolve_session(agent, target, since, conn)
    if not sid:
        return {"verdict": "unknown", "ts": None, "reason": how}
    path = _transcript(agent.get("claude_cwd") or agent.get("cwd") or "", sid)
    if not path:
        return {"verdict": "unknown", "ts": None, "reason": "no_transcript"}
    seen = submitted_prompts(path, since, conn=conn, target=target)
    unknown = ""
    for t, fps in sorted(seen["prompts"], reverse=True):
        who = attribute(target, t, fps)
        if who in ("human", "api"):
            return {"verdict": "task", "ts": t, "reason": f"{who}_prompt_via_{how}"}
        if who == "unknown":
            unknown = "unattributed_prompt"
    if unknown:
        return {"verdict": "unknown", "ts": None, "reason": unknown}
    if not seen["complete"]:
        return {"verdict": "unknown", "ts": None, "reason": "transcript_backlog"}
    return {"verdict": "none", "ts": None, "reason": f"no_prompt_via_{how}"}
