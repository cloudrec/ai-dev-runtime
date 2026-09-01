"""The installed Claude Code's own view of its sessions.

WHY THIS EXISTS. Owner OS has two long-standing defects that are both the same
mistake: inferring, from the outside, a fact the runtime already knows exactly.

  * `control_plane.discovery` identified an agent by asking which conversation was
    newest in its CWD. That is a per-DIRECTORY answer to a per-PANE question, so
    two agents in one directory are indistinguishable — which is how a pane that
    had genuinely died was labelled `renamed_from` of the pane that replaced it
    (event 18172), and why `8aba07f` had to widen an identity set rather than
    simply know it.
  * `closed_loop_wake` decides an agent is stalled by counting EVENTS, because it
    had no way to ask whether the agent was working. A turn that runs half an hour
    emits nothing while it works, so it is indistinguishable from a dead pane. Both
    `8aba07f` and `3d8d4bf` subtract exceptions from that proxy; neither replaces it.

`claude agents --json` answers both directly: it lists active sessions —
interactive and background — each with `sessionId`, `pid`, `cwd`, `name`, and a
`status`/`state` of `busy` / `idle` / `blocked`.

FAIL OPEN, ALWAYS. Every failure — no binary, a timeout, malformed JSON, a session
this listing does not know — yields "no opinion", never "not alive". A supervisor
that treats an unreadable listing as evidence of death would invent exactly the
false crashes this module exists to stop. Callers keep the behaviour they had
before whenever this module cannot answer.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

# Off switch, on by default. A native view that starts misbehaving must be
# removable without a deploy of every caller.
ENABLED = os.getenv("OWNEROS_NATIVE_SESSIONS", "1") not in ("0", "", "false", "no")

# The companion ticks every 20 s and several callers ask per tick. Measured cost of
# one call on this host: 0.80 / 1.26 / 1.99 s. A short TTL keeps that to roughly one
# call per tick while never serving an answer old enough to be about a different
# process.
TTL_SECS = float(os.getenv("OWNEROS_NATIVE_SESSIONS_TTL", "15"))
TIMEOUT_SECS = float(os.getenv("OWNEROS_NATIVE_SESSIONS_TIMEOUT", "20"))
_BIN = os.getenv("CLAUDE_BIN", "claude")

_cache: dict = {"ts": 0.0, "rows": []}


def _list_raw() -> list:
    """One real call. Injectable: tests replace this, never the subprocess itself."""
    out = subprocess.run([_BIN, "agents", "--json"], capture_output=True, text=True,
                         timeout=TIMEOUT_SECS)
    if out.returncode != 0:
        return []
    body = (out.stdout or "").strip()
    rows = json.loads(body) if body else []
    return rows if isinstance(rows, list) else []


def sessions(*, now: Optional[float] = None, refresh: bool = False) -> list:
    """Active sessions as the runtime sees them. `[]` means "no opinion"."""
    if not ENABLED:
        return []
    now = now if now is not None else time.monotonic()
    # Freshness gates the cache, NOT truthiness. Keying on "we have rows" meant an
    # EMPTY answer was never cached, so a host with no sessions — or a binary that
    # has started failing, which is exactly when this must stay cheap — paid a
    # subprocess call on every single lookup. Measured: five lookups, five calls,
    # and 49 watch evaluations took 27 s instead of milliseconds.
    if not refresh and _cache["ts"] and (now - _cache["ts"]) < TTL_SECS:
        return _cache["rows"]
    try:
        rows = [r for r in _list_raw() if isinstance(r, dict)]
    except Exception:  # noqa: BLE001 — an unreadable listing is never evidence
        # A failure does not refresh the clock: the next call may still retry, but a
        # STALE-BUT-FRESH-ENOUGH answer is preferred to none while it does.
        return _cache["rows"] if (_cache["ts"] and (now - _cache["ts"]) < TTL_SECS) else []
    _cache["ts"], _cache["rows"] = now, rows
    return rows


def reset_cache() -> None:
    _cache["ts"], _cache["rows"] = 0.0, []


#: How far up a process tree to look for the pane that owns a session. A pane that
#: runs `claude` directly needs 0 hops; one where the operator typed `claude` into an
#: already-open shell needs 1. A small bound keeps a pathological /proc from turning
#: a lookup into a walk to init.
_MAX_ANCESTRY_HOPS = int(os.getenv("OWNEROS_NATIVE_ANCESTRY_HOPS", "4"))


def _ppid_of(pid: int) -> int:
    """Parent of this pid, or 0. Reads /proc directly — no subprocess, no shell.

    The comm field can contain spaces and parentheses, so the ppid is taken from
    after the LAST ')' rather than by splitting the whole line.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        return int(data[data.rindex(")") + 1:].split()[1])
    except Exception:  # noqa: BLE001 — an unreadable process is simply not an ancestor
        return 0


def _is_descendant_of(child_pid, ancestor_pid: int) -> bool:
    try:
        cur = int(child_pid)
    except (TypeError, ValueError):
        return False
    for _ in range(_MAX_ANCESTRY_HOPS):
        cur = _ppid_of(cur)
        if not cur or cur == 1:
            return False
        if cur == ancestor_pid:
            return True
    return False


def by_pid(pid, *, now: Optional[float] = None) -> Optional[dict]:
    """The session running as this pid — or as a CHILD of it.

    Owner OS records the tmux PANE's pid; the runtime records the `claude` process
    itself. They are the same number only when the pane runs `claude` directly. Where
    the operator typed `claude` into an already-open shell, the pane is `-bash` and
    `claude` is its child: measured on this host, 8 of 10 agents matched directly and
    2 did not (`email:0.0` pane 1692437 -> claude 1695585, `hostsecure:0.0` pane
    3260897 -> claude 3262329). Those two silently lost every native answer.

    The direct match is tried first and costs nothing; the ancestry walk runs only
    when it fails, and only for as many sessions as are listed.
    """
    try:
        want = int(pid)
    except (TypeError, ValueError):
        return None
    if not want:
        return None
    rows = sessions(now=now)
    for r in rows:
        try:
            if int(r.get("pid") or 0) == want:
                return r
        except (TypeError, ValueError):
            continue
    for r in rows:
        if r.get("pid") and _is_descendant_of(r.get("pid"), want):
            return r
    return None


def session_id_for_pid(pid, *, now: Optional[float] = None) -> str:
    """The runtime's own id for the session running as this pid, or ""."""
    return ((by_pid(pid, now=now) or {}).get("sessionId") or "").strip()


def _state_of(row: dict) -> str:
    # Interactive rows carry `status`; background rows carry `state`. Same question.
    return str(row.get("status") or row.get("state") or "").strip().lower()


def status_for_session(session_id: str, *, now: Optional[float] = None) -> str:
    """`busy` / `idle` / `blocked` for this session id, or "" when unknown.

    Matching accepts a PREFIX, because Owner OS addresses hook-sourced agents as
    `session:<id[:12]>` — the id is truncated at the point it becomes an agent_id,
    so a caller holding only that much can still ask.
    """
    want = (session_id or "").strip()
    if not want:
        return ""
    for r in sessions(now=now):
        sid = str(r.get("sessionId") or "")
        if sid and (sid == want or sid.startswith(want)):
            return _state_of(r)
    return ""


def status_for_pid(pid, *, now: Optional[float] = None) -> str:
    row = by_pid(pid, now=now)
    return _state_of(row) if row else ""


#: States that positively mean the session is doing work right now. Deliberately
#: excludes `idle` (at rest is not progress) and `blocked` (waiting, which is what
#: the callers are trying to distinguish, not evidence against it).
WORKING_STATES = frozenset({"busy"})


def is_working(session_id: str = "", *, pid=None, now: Optional[float] = None) -> bool:
    """Positive evidence only. Unknown is False, and False is never "dead"."""
    state = ""
    if pid is not None:
        state = status_for_pid(pid, now=now)
    if not state and session_id:
        state = status_for_session(session_id, now=now)
    return state in WORKING_STATES
