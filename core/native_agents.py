"""Native Claude Code session state for Owner OS.

Primary source: the official `claude agents --json` (Claude Code >= 2.1.x), which prints
every active interactive and background session as a JSON array and exits without a TTY.
This replaces TUI scraping as the PRIMARY state source. The existing `agent_control`
inventory is used only when the command is unavailable or returns something that does not
validate, and every result says which source it came from.

Bounded: one subprocess, hard timeout, capped output size, strict JSON shape validation.
Read-only: nothing here starts, stops, messages or types into a session.

Join to tmux: a native session row carries the Claude `pid`; `agent_control.agent_list()`
carries the same pid as `claude_pid` per pane, so a pane target is attached when (and only
when) exactly one pane matches. The native row stays authoritative for state.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Callable, Optional

CLAUDE_BIN = os.getenv("RUNTIME_CLAUDE_BIN", "/root/.local/bin/claude")
TIMEOUT_SECS = int(os.getenv("NATIVE_AGENTS_TIMEOUT_SECS", "30"))
MAX_BYTES = int(os.getenv("NATIVE_AGENTS_MAX_BYTES", str(2 * 1024 * 1024)))
SOURCE_NATIVE = "claude_agents_json"
SOURCE_FALLBACK = "agent_control_fallback"

# interactive rows report `status`, background rows report `state`
_STATES = {"idle", "busy", "shell", "blocked", "running", "completed", "exited", "stopped",
           "waiting", "error", "starting"}
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

Runner = Callable[[list], tuple]          # argv -> (returncode, stdout, stderr)


def _run(argv: list) -> tuple:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_SECS,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, b"", type(e).__name__.encode()
    return p.returncode, p.stdout[:MAX_BYTES + 1], p.stderr[:4000]


def claude_version(run: Runner = _run) -> Optional[tuple]:
    rc, out, _ = run([CLAUDE_BIN, "--version"])
    m = _VERSION_RE.match((out or b"").decode("utf-8", "replace").strip()) if rc == 0 else None
    return tuple(int(x) for x in m.groups()) if m else None


def capabilities(run: Runner = _run) -> dict:
    """What this installed Claude Code supports, detected from its own help text."""
    ver = claude_version(run)
    rc, out, _ = run([CLAUDE_BIN, "agents", "--help"])
    help_txt = (out or b"").decode("utf-8", "replace") if rc == 0 else ""
    return {"version": ".".join(map(str, ver)) if ver else None,
            "agents_json": "--json" in help_txt}


def validate(rows) -> list:
    """Strict shape check. Raises ValueError on anything that is not the documented array."""
    if not isinstance(rows, list):
        raise ValueError("not a JSON array")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError("row is not an object")
        name, kind, cwd = r.get("name"), r.get("kind"), r.get("cwd")
        sid = r.get("sessionId") or r.get("id")
        st = r.get("status") or r.get("state")
        if not (isinstance(name, str) and name and isinstance(cwd, str) and cwd
                and kind in ("interactive", "background") and isinstance(sid, str) and sid):
            raise ValueError(f"row missing required fields: {sorted(r)}")
        if not isinstance(st, str) or st not in _STATES:
            st = "unknown"                 # an unknown state is never read as idle
        pid = r.get("pid")
        out.append({"name": name, "kind": kind, "cwd": cwd, "session_id": sid,
                    "short_id": r.get("id") if r.get("sessionId") else None,
                    "state": st, "pid": pid if isinstance(pid, int) else None,
                    "started_at": r.get("startedAt")})
    return out


def _fallback(reason: str, inventory: Optional[Callable]) -> dict:
    from core import agent_control as ac
    inv = (inventory or ac.agent_list)()
    sessions = [{"name": a.get("session"), "kind": "interactive", "cwd": a.get("claude_cwd")
                 or a.get("cwd"), "session_id": None, "short_id": None,
                 "state": a.get("state") or "unknown", "pid": a.get("claude_pid"),
                 "started_at": None, "tmux_target": a.get("target")}
                for a in inv.get("agents", []) if a.get("is_agent")]
    return {"source": SOURCE_FALLBACK, "fallback_reason": reason, "sessions": sessions}


def list_sessions(run: Runner = _run, inventory: Optional[Callable] = None) -> dict:
    """{source, sessions:[{name, kind, cwd, session_id, state, pid, tmux_target}]}"""
    rc, out, err = run([CLAUDE_BIN, "agents", "--json"])
    if rc != 0:
        return _fallback(f"agents_json_rc_{rc}", inventory)
    if len(out) > MAX_BYTES:
        return _fallback("agents_json_output_too_large", inventory)
    try:
        sessions = validate(json.loads(out.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as e:
        return _fallback(f"agents_json_invalid:{str(e)[:80]}", inventory)
    _attach_tmux(sessions, inventory)
    return {"source": SOURCE_NATIVE, "sessions": sessions}


def _attach_tmux(sessions: list, inventory: Optional[Callable]) -> None:
    try:
        from core import agent_control as ac
        panes = (inventory or ac.agent_list)().get("agents", [])
    except Exception:  # noqa: BLE001 — the join is a convenience; native state stands alone
        panes = []
    for s in sessions:
        hits = [p for p in panes if s["pid"] and p.get("claude_pid") == s["pid"]]
        s["tmux_target"] = hits[0].get("target") if len(hits) == 1 else None


def find(name_or_target: str, listing: dict) -> Optional[dict]:
    """Exactly one session by native name, tmux target or tmux session name; else None."""
    key = (name_or_target or "").strip()
    hits = [s for s in listing.get("sessions", [])
            if key and (s.get("name") == key or s.get("tmux_target") == key
                        or (s.get("tmux_target") or "").split(":")[0] == key)]
    return hits[0] if len(hits) == 1 else None
