"""Direct Agent Control Plane — manage the Claude Code agents that already run
inside tmux on this host, without using the owner as a command courier.

Why this module exists
----------------------
The Owner OS MCP server runs inside the `seo-backend` container: it has no tmux
binary, no tmux socket and no host PID namespace, so it cannot see or address the
agents at all. This module is the host-side half — it runs inside
`ai-runtime.service` (root, real tmux) and is exposed over the authenticated
`/api/v1/agents/*` endpoints that the MCP tools proxy to.

Design rules this module is built on
------------------------------------
* **Never a shell.** Every tmux call is an argv list, so no payload is ever
  interpreted by a shell. There is deliberately no "run arbitrary command" entry
  point, and `test_tmux_transport_never_uses_a_shell` pins that.
* **Existing agents are preferred over new ones.** `send`/`answer` refuse to
  create an agent implicitly, and `resume` refuses when a matching live Claude
  already exists — duplicate agents are the failure mode this plane exists to
  prevent.
* **Everything is bounded.** Capture line counts, message sizes and report sizes
  all have hard ceilings, so a runaway pane can't blow up the caller.
* **Delivery is proven, not assumed.** `send` captures the pane before and after
  and reports what actually changed rather than trusting tmux's exit code.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

# ── bounds ──────────────────────────────────────────────────────────────────
MAX_CAPTURE_LINES = 2000
DEFAULT_CAPTURE_LINES = 200
MAX_MESSAGE_BYTES = 16384
MAX_REPORT_BYTES = 262144
MAX_REPORT_FILES = 50
_TMUX_TIMEOUT = 15
_IDEMPOTENCY_TTL_SECS = 24 * 3600

# ── validation ──────────────────────────────────────────────────────────────
# tmux session names we accept. Deliberately stricter than tmux itself: no
# whitespace, no ':' / '.' / '$' so a name can never smuggle a second target or
# a tmux format expansion into an argv slot.
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# "session", "session:window" or "session:window.pane".
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}(:\d{1,4}(\.\d{1,4})?)?$")
# Claude conversation/session ids are UUIDs.
_CONVERSATION_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

_CLAUDE_CMD_RE = re.compile(r"(^|/)claude($|\s)")


class AgentControlError(Exception):
    """Invalid request: bad target, failed validation, refused action."""


# ── secret redaction ────────────────────────────────────────────────────────
# Applied to every byte of pane/report content before it leaves this process.
# Panes legitimately contain env dumps, curl commands and .env greps; none of
# that may reach an MCP client.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{8,})"), "sk-ant-***REDACTED***"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{16,})"), "sk-***REDACTED***"),
    (re.compile(r"\bmcp_[A-Za-z0-9_-]{16,}"), "mcp_***REDACTED***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"), "gh*_***REDACTED***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***REDACTED***"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 ***REDACTED***"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:token|secret|password|passwd|api[_-]?key|private[_-]?key)"
                r"[A-Z0-9_]*)\s*[:=]\s*[\"']?([^\s\"'&]{6,})[\"']?"), r"\1=***REDACTED***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "***REDACTED PRIVATE KEY***"),
)


def redact(text: str) -> str:
    """Strip anything that looks like a credential out of captured output."""
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# ── configuration ───────────────────────────────────────────────────────────
def _allowed_roots() -> list[str]:
    raw = os.getenv("AGENT_CONTROL_ALLOWED_ROOTS", "/root/ai-dev-runtime,/opt")
    return [os.path.realpath(r.strip()) for r in raw.split(",") if r.strip()]


def _allowed_sessions() -> Optional[set[str]]:
    """Optional session allowlist. Unset means 'any syntactically valid name'."""
    raw = os.getenv("AGENT_CONTROL_ALLOWED_SESSIONS", "").strip()
    if not raw:
        return None
    return {s.strip() for s in raw.split(",") if s.strip()}


def _db_path() -> str:
    return os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")


def _audit_path() -> str:
    return os.getenv("AGENT_CONTROL_AUDIT", "/var/log/ai-runtime/agent_control.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── validators ──────────────────────────────────────────────────────────────
def validate_session(name: str) -> str:
    if not name or not isinstance(name, str) or not _SESSION_RE.match(name):
        raise AgentControlError(f"invalid session name: {name!r}")
    allowed = _allowed_sessions()
    if allowed is not None and name not in allowed:
        raise AgentControlError(f"session {name!r} is not allowlisted")
    return name


def validate_target(target: str) -> str:
    """Validate a pane target and check its session against the allowlist."""
    if not target or not isinstance(target, str) or not _TARGET_RE.match(target):
        raise AgentControlError(f"invalid tmux target: {target!r}")
    validate_session(target.split(":", 1)[0])
    return target


def validate_project_dir(path: str) -> str:
    """Resolve a project directory and confirm it sits inside an allowed root.

    Uses realpath, so a symlink pointing out of the allowlist is rejected on the
    resolved location rather than the string the caller supplied.
    """
    if not path or not isinstance(path, str):
        raise AgentControlError("project directory is required")
    if "\x00" in path:
        raise AgentControlError("invalid project directory")
    resolved = os.path.realpath(path)
    for root in _allowed_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            if not os.path.isdir(resolved):
                raise AgentControlError(f"project directory does not exist: {resolved}")
            return resolved
    raise AgentControlError(f"project directory {resolved!r} is outside the allowed roots")


def _bound_lines(lines: Any) -> int:
    try:
        n = int(lines) if lines is not None else DEFAULT_CAPTURE_LINES
    except (TypeError, ValueError):
        raise AgentControlError(f"invalid line count: {lines!r}")
    if n < 1:
        raise AgentControlError("line count must be >= 1")
    return min(n, MAX_CAPTURE_LINES)


def _check_message(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise AgentControlError("message text is required")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise AgentControlError(
            f"message is {len(encoded)} bytes, over the {MAX_MESSAGE_BYTES} byte limit")
    return text


# ── tmux transport ──────────────────────────────────────────────────────────
def _tmux_bin() -> Optional[str]:
    return shutil.which("tmux")


def _tmux(args: list[str], stdin: Optional[bytes] = None) -> tuple[int, str, str]:
    """Run one tmux command as an argv list. The single seam tests replace."""
    binary = _tmux_bin()
    if not binary:
        raise AgentControlError("tmux is not available on this host")
    proc = subprocess.run([binary, *args], input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=_TMUX_TIMEOUT)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


_PANE_FORMAT = ("#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}"
                "\t#{pane_current_path}\t#{pane_current_command}\t#{pane_dead}\t#{window_name}")


def parse_panes(raw: str) -> list[dict]:
    """Parse `tmux list-panes -a -F <_PANE_FORMAT>` output.

    Kept separate from the subprocess call so the parser is unit-testable
    against recorded tmux output.
    """
    panes: list[dict] = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        session, window, pane, pane_id, pane_pid, cwd, command, dead = parts[:8]
        window_name = parts[8] if len(parts) > 8 else ""
        try:
            pid = int(pane_pid)
        except ValueError:
            pid = None
        panes.append({
            "session": session,
            "window": window,
            "pane": pane,
            "target": f"{session}:{window}.{pane}",
            "pane_id": pane_id,
            "pid": pid,
            "cwd": cwd,
            "command": command,
            "window_name": window_name,
            "alive": dead.strip() in ("0", ""),
        })
    return panes


# ── process inspection ──────────────────────────────────────────────────────
def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except (OSError, ValueError):
        return ""


def _child_pids(pid: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            return [int(p) for p in fh.read().split()]
    except (OSError, ValueError):
        return []


def _descendants(pid: int, depth: int = 4) -> list[int]:
    """Bounded walk of a pane's process subtree — Claude usually sits one or two
    levels under the pane's shell."""
    found: list[int] = []
    frontier = [pid]
    for _ in range(depth):
        nxt: list[int] = []
        for p in frontier:
            for child in _child_pids(p):
                found.append(child)
                nxt.append(child)
        if not nxt:
            break
        frontier = nxt
    return found


def is_claude_cmdline(cmdline: str) -> bool:
    """Whether a process command line is a Claude Code agent.

    Excludes this control plane's own tooling calls (`claude --version` style
    probes) so a health check is never mistaken for a live agent.
    """
    if not cmdline:
        return False
    first = cmdline.split()[0] if cmdline.split() else ""
    if not _CLAUDE_CMD_RE.search(first) and "claude" not in first:
        return False
    if re.search(r"\s--(version|help)\b", cmdline):
        return False
    return True


def find_claude_in_pane(pane_pid: Optional[int]) -> Optional[dict]:
    """Locate the live Claude process belonging to a pane, if any."""
    if not pane_pid:
        return None
    for pid in [pane_pid, *_descendants(pane_pid)]:
        cmdline = _proc_cmdline(pid)
        if is_claude_cmdline(cmdline):
            return {"pid": pid, "cmdline": redact(cmdline), "cwd": _proc_cwd(pid)}
    return None


# ── idempotency + audit ─────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS deliveries (
        idempotency_key TEXT PRIMARY KEY, target TEXT, action TEXT,
        result TEXT, created_at TEXT, created_ts REAL)""")
    # Supervisor decisions per (target, prompt hash) — persisted so the same
    # prompt is never re-processed or re-alerted after a service restart.
    conn.execute("""CREATE TABLE IF NOT EXISTS supervisor_prompts (
        target TEXT, prompt_hash TEXT, decision TEXT, category TEXT, reason TEXT,
        latency REAL, updated_at TEXT, updated_ts REAL,
        PRIMARY KEY (target, prompt_hash))""")
    # Durable Commander event log — checkpoint / completion / waiting-external /
    # owner-decision events survive between polls and a 45s record overwrite, so
    # Owner OS / ChatGPT can SEE them (the 2026-07-22 failure: events were computed
    # then discarded by the loop). Deduped by (agent,event_type,dedup_key).
    conn.execute("""CREATE TABLE IF NOT EXISTS commander_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ts_epoch REAL, agent TEXT,
        project TEXT, event_type TEXT, payload TEXT, dedup_key TEXT,
        acknowledged INTEGER DEFAULT 0)""")
    conn.commit()
    return conn


def record_commander_event(agent: str, project: str, event_type: str, payload: dict,
                           dedup_key: str = "", dedup_window_secs: int = 1800) -> bool:
    """Append a durable Commander event unless an identical unacknowledged one
    (same agent, event_type, dedup_key) was recorded within the window. Returns
    True when a NEW event was written."""
    conn = _db()
    try:
        # Dedup against ALL recent events (acked OR not) — acking must not let the
        # same event re-emit + re-deliver moments later (the 2026-07-22 repeated
        # false job completion). A genuinely new event carries a distinct dedup_key.
        row = conn.execute(
            "SELECT ts_epoch FROM commander_events WHERE agent=? AND event_type=? AND "
            "dedup_key=? ORDER BY ts_epoch DESC LIMIT 1",
            (agent, event_type, dedup_key)).fetchone()
        if row and (time.time() - float(row[0])) < dedup_window_secs:
            return False
        conn.execute(
            "INSERT INTO commander_events(ts,ts_epoch,agent,project,event_type,payload,dedup_key) "
            "VALUES(?,?,?,?,?,?,?)",
            (_now(), time.time(), agent, project, event_type,
             json.dumps(payload, default=str), dedup_key))
        conn.commit()
        audit("commander_event", agent, event_type=event_type, project=project,
              payload=payload, dedup_key=dedup_key)
        return True
    finally:
        conn.close()


def list_commander_events(since_epoch: float = 0.0, limit: int = 50,
                          unacked_only: bool = False) -> list:
    conn = _db()
    try:
        q = ("SELECT id,ts,ts_epoch,agent,project,event_type,payload,acknowledged "
             "FROM commander_events WHERE ts_epoch >= ?")
        if unacked_only:
            q += " AND acknowledged=0"
        q += " ORDER BY ts_epoch DESC LIMIT ?"
        rows = conn.execute(q, (since_epoch, limit)).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "ts": r[1], "ts_epoch": r[2], "agent": r[3],
                        "project": r[4], "event_type": r[5],
                        "payload": json.loads(r[6]) if r[6] else {}, "acknowledged": bool(r[7])})
        return out
    finally:
        conn.close()


def latest_commander_event(agent: str, within_secs: float = 900) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id,ts_epoch,event_type,payload FROM commander_events WHERE agent=? "
            "AND ts_epoch >= ? ORDER BY ts_epoch DESC LIMIT 1",
            (agent, time.time() - within_secs)).fetchone()
        if not row:
            return None
        return {"id": row[0], "ts_epoch": row[1], "event_type": row[2],
                "payload": json.loads(row[3]) if row[3] else {}}
    finally:
        conn.close()


def ack_commander_events(ids: list) -> int:
    if not ids:
        return 0
    conn = _db()
    try:
        conn.executemany("UPDATE commander_events SET acknowledged=1 WHERE id=?",
                         [(i,) for i in ids])
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


# Condition events that a later ACTIVE/COMPLETED state contradicts (a stale
# blocker/stall/waiting/test-killed still sitting UNDELIVERED in the queue).
_CONDITION_EVENT_TYPES = ("agent_externally_blocked", "agent_owner_decision", "agent_waiting_input",
                          "agent_unexpected_idle", "agent_process_failed", "agent_recovery_failure")
_ACTIVE_OR_DONE = ("working", "shell_running", "completed")


def retract_stale_condition_events(agent: str, current_state: str, reason: str = "") -> list:
    """SOURCE-SIDE retraction: when the orchestrator sees `agent` ACTIVE/COMPLETED
    again, retract its still-UNACKED condition events (blocker/stall/waiting/
    test-killed) so the notifier never delivers a contradicted alert. Atomic +
    idempotent + restart-safe: only rows still `acknowledged=0` are acked (race-safe
    against the notifier's own ack), and a per-event lineage marker
    (`commander_event_retracted`, dedup_key `retract-src:<id>`) is recorded once.
    Returns the retracted event ids. No-op unless the state is active/completed."""
    if current_state not in _ACTIVE_OR_DONE:
        return []
    conn = _db()
    try:
        ph = ",".join("?" for _ in _CONDITION_EVENT_TYPES)
        rows = conn.execute(
            f"SELECT id,event_type,project FROM commander_events WHERE agent=? AND acknowledged=0 "
            f"AND event_type IN ({ph}) ORDER BY id", (agent, *_CONDITION_EVENT_TYPES)).fetchall()
        if not rows:
            return []
        conn.executemany("UPDATE commander_events SET acknowledged=1 WHERE id=? AND acknowledged=0",
                         [(r[0],) for r in rows])
        conn.commit()
    finally:
        conn.close()
    retracted = []
    for eid, etype, project in rows:
        record_commander_event(
            agent, project or "", "commander_event_retracted",
            {"retracted_event_id": eid, "retracted_event_type": etype, "agent": agent,
             "subject_key": f"agent:{agent}", "current_state": current_state,
             "reason": reason or f"agent {current_state} again — event stale"},
            dedup_key=f"retract-src:{eid}", dedup_window_secs=604800)
        retracted.append(eid)
    return retracted


def get_prompt_decision(target: str, prompt_hash: str) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute("SELECT decision, category, reason, latency, updated_ts FROM "
                           "supervisor_prompts WHERE target=? AND prompt_hash=?",
                           (target, prompt_hash)).fetchone()
        if not row:
            return None
        return {"decision": row[0], "category": row[1], "reason": row[2],
                "latency": row[3], "updated_ts": row[4]}
    finally:
        conn.close()


def record_prompt_decision(target: str, prompt_hash: str, decision: str,
                           category: str = "", reason: str = "", latency: float | None = None) -> None:
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO supervisor_prompts VALUES (?,?,?,?,?,?,?,?)",
                     (target, prompt_hash, decision, category, reason, latency, _now(), time.time()))
        conn.commit()
    finally:
        conn.close()


def approve_prompt(target: str) -> bool:
    """Send the approval key for a Claude Code permission dialog.

    Sends exactly the digit '1' — "Yes" for THIS command only. It deliberately
    never sends '2' ("Yes, and don't ask again"), so every future command is
    re-evaluated by the classifier. Read-only w.r.t. everything except this one
    keystroke to the named pane.
    """
    validate_target(target)
    rc, _, err = _tmux(["send-keys", "-t", target, "1"])
    audit("approve_prompt", target, sent=(rc == 0))
    if rc != 0:
        raise AgentControlError(f"failed to send approval to {target!r}: {err.strip()[:200]}")
    return True


# ── execution-mode (Shift+Tab cycle) detection + enforcement ────────────────
# Claude Code shows the active permission mode in the footer. `auto mode on` /
# `accept edits on` auto-approve routine work so it does not stall on prompts;
# `plan mode` blocks edits; normal prompts for everything. Shift+Tab cycles them.
_MODE_AUTO_RE = re.compile(r"\bauto[- ]?mode on\b", re.I)
_MODE_ACCEPT_RE = re.compile(r"\baccept edits on\b", re.I)
_MODE_PLAN_RE = re.compile(r"\bplan mode on\b", re.I)

# Modes in which routine work does NOT stall on a permission prompt.
NONSTALL_MODES = ("auto", "accept_edits")


def pending_input_text(target: str, tail: str | None = None) -> str:
    """Text typed into the agent's input line but NOT yet submitted (e.g. an
    owner/ChatGPT-queued instruction). CRITICAL for /clear safety: agent_send
    pastes then hits Enter WITHOUT clearing the line, so sending `/clear` while
    this is non-empty would concatenate and SUBMIT the queued text — possibly an
    external/financial/production action. A context rotation must refuse when this
    is non-empty. A numbered menu selection (`❯ 1. Yes`) is not input-line text."""
    text = tail if tail is not None else _pane_tail(target, 10)
    for line in reversed((text or "").splitlines()):
        m = re.search(r"❯\s?(.*)$", line.rstrip())
        if not m:
            continue
        content = m.group(1).strip()
        if re.match(r"^\d+\.", content):        # menu option, not the input line
            return ""
        return content
    return ""


def submit_clear(target: str, idempotency_key: Optional[str] = None) -> bool:
    """Submit an EXISTING bare `/clear` (or `/compact`) the agent already typed in
    its input line, by pressing Enter. RE-VERIFIES at submit time that the input
    line is exactly a clear command — never submits an arbitrary queued
    instruction (defends against a race between check and submit)."""
    validate_target(target)
    pending = pending_input_text(target)
    if not re.match(r"^/(clear|compact)\s*$", pending):
        raise AgentControlError(
            f"refusing to submit: input line is not a bare clear command: {pending!r}")
    rc, _, err = _tmux(["send-keys", "-t", target, "Enter"])
    audit("submit_clear", target, idempotency_key, submitted=(rc == 0), pending=pending[:40])
    if rc != 0:
        raise AgentControlError(f"failed to submit clear to {target!r}: {err.strip()[:200]}")
    return True


def detect_exec_mode(pane_tail: str) -> str:
    """Visible Claude Code permission mode: 'auto' | 'accept_edits' | 'plan' |
    'normal'. 'unknown' when the footer is not present in the captured tail."""
    tail = pane_tail or ""
    if not (_MODE_AUTO_RE.search(tail) or _MODE_ACCEPT_RE.search(tail)
            or _MODE_PLAN_RE.search(tail) or "shift+tab to cycle" in tail.lower()):
        return "unknown"
    if _MODE_AUTO_RE.search(tail):
        return "auto"
    if _MODE_ACCEPT_RE.search(tail):
        return "accept_edits"
    if _MODE_PLAN_RE.search(tail):
        return "plan"
    return "normal"


def ensure_auto_mode(target: str, tail_fn=None, max_cycles: int = 3) -> dict:
    """Bring a MANAGED-AUTO agent into a non-stalling mode (`auto mode on`).

    Caller MUST gate this to managed-auto sessions only. Sends Shift+Tab (BTab)
    and re-reads the footer after each press, stopping as soon as a non-stalling
    mode is visible; never presses more than one full cycle, so it cannot land the
    pane somewhere worse than it started. A mode toggle is NOT a command approval:
    every command still passes the resolver/owner gates. No-op when already
    non-stalling or when the mode cannot be read (unknown → never guess)."""
    validate_target(target)
    tail_fn = tail_fn or (lambda: _pane_tail(target, 6))
    mode = detect_exec_mode(tail_fn())
    if mode in NONSTALL_MODES:
        return {"target": target, "mode": mode, "action": "none", "already": True}
    if mode == "unknown":
        return {"target": target, "mode": mode, "action": "none", "reason": "mode not visible"}
    presses = 0
    for _ in range(max_cycles):
        rc, _out, err = _tmux(["send-keys", "-t", target, "BTab"])
        presses += 1
        if rc != 0:
            audit("ensure_auto_mode", target, sent=False, error=err.strip()[:120])
            return {"target": target, "mode": mode, "action": "error", "error": err.strip()[:120]}
        time.sleep(0.4)
        mode = detect_exec_mode(tail_fn())
        if mode in NONSTALL_MODES:
            audit("ensure_auto_mode", target, restored_to=mode, presses=presses)
            return {"target": target, "mode": mode, "action": "restored", "presses": presses}
    audit("ensure_auto_mode", target, restored_to=mode, presses=presses, incomplete=True)
    return {"target": target, "mode": mode, "action": "incomplete", "presses": presses}


def _seen_delivery(key: str) -> Optional[dict]:
    """Return a prior delivery for this key, or None. Expired rows are dropped."""
    conn = _db()
    try:
        conn.execute("DELETE FROM deliveries WHERE created_ts < ?", (time.time() - _IDEMPOTENCY_TTL_SECS,))
        conn.commit()
        row = conn.execute("SELECT result FROM deliveries WHERE idempotency_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def _record_delivery(key: str, target: str, action: str, result: dict) -> None:
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)",
                     (key, target, action, json.dumps(result), _now(), time.time()))
        conn.commit()
    finally:
        conn.close()


def audit(action: str, target: str, idempotency_key: Optional[str] = None, **fields: Any) -> None:
    """Append-only action log: timestamp, target, idempotency key, outcome."""
    entry = {"ts": _now(), "action": action, "target": target,
             "idempotency_key": idempotency_key, **fields}
    path = _audit_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        # Auditing must never take down the action it is recording.
        pass


# ══════════════════════════════ TOOLS ═══════════════════════════════════════
# ── observable agent-state classification ───────────────────────────────────
# Distinguishing "a tmux pane is alive" from "an agent is doing real work".
#
# The hard-won lesson: a live Claude process, a queued owner prompt, an old
# spinner glyph, or prior output are NOT evidence of work. Claude Code parks at a
# "new task? /clear to save …" prompt when idle, and its spinner lines turn
# PAST-tense there ("Worked for 1m 47s", "Brewed for 50m"). Only three things
# prove an agent is executing RIGHT NOW:
#   1. an "esc to interrupt" hint (Claude is mid-turn),
#   2. a live spinner with a running timer: "…​ (12s" / "…​ (1m 2s",
#   3. a streaming token counter with an arrow: "↓ 2.4k tokens" / "↑ …".
# Absent that, the agent is idle / waiting / externally_blocked, never "working".
# A finished report with an empty "❯" prompt has NO active indicator, so it is at
# rest — even when it carries no "new task?" line and its text differs from a
# stale cached sample. Output progression alone is NOT accepted as work: a
# scrollback shift or a stale cache diff must never read as "working".
AGENT_STATES = ("working", "shell_running", "waiting_input", "idle", "waiting_owner",
                "externally_blocked", "completed", "dead", "stale")

# Active-execution evidence — the ONLY positive proof of "working". A live agent
# ALWAYS shows one of these while a turn is running; at rest it shows none.
#   * "esc to interrupt" — Claude is mid-turn,
#   * a live spinner with a running timer: "…​ (12s" / "…​ (1m 2s",
#   * a streaming token counter with an arrow: "↓ 2.4k tokens" / "↑ …".
# Past-tense spinners ("Worked for", "Brewed for 11m 24s") are NOT active.
_STATE_ACTIVE_RUN_RE = re.compile(
    r"(esc to interrupt|…\s*\(\d+\s*m?s\b|[↑↓]\s*[\d.]+\s*k?\s*tokens\b)", re.I)
# Blocked on something outside the agent's control (vendor key, credentials, quota).
# NARROW on purpose: generic words like "waiting for", "timed out", "network error",
# "reconnect", "502/503" appear constantly in benign SHELL/tool output and must NOT
# read as an external block (that mis-classified a capacity agent running a live
# shell as externally_blocked). Only strong, agent-level block phrases qualify, and
# only near the END of the pane (current status), never deep scrollback.
_STATE_EXTERNAL_RE = re.compile(
    r"(verification key|awaiting vendor|vendor key|input[_ ]required|"
    r"api key required|credentials? required|quota exceeded|rate.?limit(ed|ing)?\b|"
    r"429 too many|externally blocked|blocked on (an? )?external)", re.I)
# Claude is asking the owner a question (not the owner's own queued input line).
_STATE_WAIT_OWNER_RE = re.compile(
    r"(\(y/n\)|\[y/n\]|do you want (me )?to|shall i\b|proceed\?|may i\b|"
    r"which (option|approach)|choose an option|awaiting your (approval|decision|confirmation))", re.I)


def classify_state(alive: bool, is_agent: bool, output_tail: str, prev_tail: str | None = None,
                   *, pending_input: str = "", shell_running: bool = False) -> str:
    """Observable state of an agent pane.

    `working` requires concrete active-execution evidence — a live spinner timer,
    streaming tokens, or "esc to interrupt". `shell_running` (a live non-Claude
    foreground command in the pane) is also real work, not idle/blocked.
    `pending_input` (a real, non-empty, non-ghost `❯` line the caller extracted)
    means a command was typed/pasted but NOT submitted → `waiting_input`, so such
    an agent is never lost as plain idle. `prev_tail` is accepted for signature
    compatibility but does not influence `working`. New signals default off so the
    behaviour is unchanged for callers that do not pass them."""
    if not alive:
        return "dead"
    if not is_agent:
        return "stale"          # alive pane, no live Claude process
    tail = (output_tail or "")[-1500:]

    # 1) concrete active-execution evidence — the only path to "working".
    if _STATE_ACTIVE_RUN_RE.search(tail):
        return "working"
    # 1b) a live shell command running in the pane is work in progress (shell-
    #     running), NOT idle and NOT an external block.
    if shell_running:
        return "shell_running"
    # 2) an ACTIVE permission dialog is waiting_owner even if the command it shows
    #    contains an external-looking word; the command text is not evidence of a
    #    real external block. Checked BEFORE the external heuristic.
    if _STATE_WAIT_OWNER_RE.search(tail):
        return "waiting_owner"
    # 3) a typed/pasted but unsubmitted command → waiting_input (owner must submit).
    #    Recovers idle agents with a staged command that were previously lost.
    if pending_input and pending_input.strip():
        return "waiting_input"
    # 4) at rest — classify what it is resting on. An external block must appear in
    #    the CURRENT status (last ~500 chars), never in deep scrollback, so shell
    #    output higher up cannot trip it.
    if _STATE_EXTERNAL_RE.search(tail[-500:]):
        return "externally_blocked"
    return "idle"


def _pane_tail(target: str, lines: int = 25) -> str:
    rc, out, _ = _tmux(["capture-pane", "-p", "-t", target, "-S", f"-{lines}"])
    return redact(out) if rc == 0 else ""


# Foreground commands that mean the pane is at rest (an interactive shell or the
# Claude/node process itself), NOT running a shell command.
_IDLE_FG_COMMANDS = {"bash", "-bash", "zsh", "-zsh", "sh", "-sh", "fish", "node",
                     "claude", "tmux", "login", "starship"}
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _pane_shell_running(pane: dict) -> bool:
    """A live, non-idle foreground command in the pane = a shell command running.
    An idle interactive shell or the Claude/node process is not 'running'."""
    cmd = (pane.get("command") or "").strip().lower()
    return bool(cmd) and cmd not in _IDLE_FG_COMMANDS


def _pane_pending_input(target: str) -> str:
    """Real, non-empty text typed/pasted at the `❯` prompt but NOT submitted.
    Returns '' for an empty prompt or a dim RECALL GHOST (SGR 2), so a ghost is
    never mistaken for a staged command."""
    rc, out, _ = _tmux(["capture-pane", "-e", "-p", "-t", target, "-S", "-6"])
    if rc != 0:
        return ""
    prompt_lines = [ln for ln in out.splitlines() if "❯" in ln]
    if not prompt_lines:
        return ""
    after = prompt_lines[-1].split("❯", 1)[1]
    if "\x1b[2m" in after:                 # dim ghost = recall hint, not staged input
        return ""
    return _SGR_RE.sub("", after).replace("\xa0", " ").strip()


def agent_list() -> dict:
    """Inventory every tmux pane and the Claude agents running in them."""
    rc, out, err = _tmux(["list-panes", "-a", "-F", _PANE_FORMAT])
    if rc != 0:
        if "no server running" in (err or "").lower():
            audit("agent_list", "-", agents=0, note="no tmux server")
            return {"agents": [], "sessions": [], "duplicates": [], "tmux_running": False}
        raise AgentControlError(f"tmux list-panes failed: {err.strip()[:200]}")

    agents = []
    for pane in parse_panes(out):
        claude = find_claude_in_pane(pane["pid"])
        is_agent = bool(claude)
        # Classify only real agents from a short pane tail; a dead pane needs no
        # capture. `working` is decided solely from active-execution evidence in
        # the tail — no cached-tail comparison.
        tail = _pane_tail(pane["target"]) if (is_agent and pane["alive"]) else ""
        # Extra signals only for an at-rest agent (skip when already active/working)
        # so an active agent costs no extra tmux capture.
        shell_running, pending = False, ""
        if is_agent and pane["alive"] and not _STATE_ACTIVE_RUN_RE.search(tail[-1500:]):
            shell_running = _pane_shell_running(pane)
            if not shell_running:
                pending = _pane_pending_input(pane["target"])
        state = classify_state(pane["alive"], is_agent, tail,
                               pending_input=pending, shell_running=shell_running)
        # Last non-blank pane line as a short activity snippet + a redacted queued-input
        # preview (a typed/pasted-but-unsubmitted command). Both secret-redacted.
        last_line = ""
        for _ln in reversed(tail.splitlines()):
            if _ln.strip():
                last_line = redact(_ln.strip())[:160]
                break
        agents.append({
            **pane,
            "command": redact(pane["command"]),
            "claude": claude,
            "is_agent": is_agent,
            "claude_pid": claude["pid"] if claude else None,
            "claude_cwd": claude["cwd"] if claude else None,
            "state": state,
            "queued_input": redact(pending)[:200] if pending else "",
            "last_pane_line": last_line,
        })

    # Duplicate agents = more than one live Claude on the same working directory.
    # That is the state `agent_resume` exists to avoid creating.
    by_cwd: dict[str, list[str]] = {}
    for a in agents:
        if a["is_agent"] and a["alive"]:
            by_cwd.setdefault(a["claude_cwd"] or a["cwd"], []).append(a["target"])
    duplicates = [{"cwd": cwd, "targets": targets, "count": len(targets)}
                  for cwd, targets in by_cwd.items() if len(targets) > 1]

    audit("agent_list", "-", agents=len(agents), duplicates=len(duplicates))
    return {
        "agents": agents,
        "sessions": sorted({a["session"] for a in agents}),
        "duplicates": duplicates,
        "tmux_running": True,
    }


def agent_status(target: str) -> dict:
    """Inspect one existing agent. Read-only: changes nothing about the pane."""
    validate_target(target)
    inventory = agent_list()
    matches = [a for a in inventory["agents"]
               if a["target"] == target or a["session"] == target]
    if not matches:
        audit("agent_status", target, found=False)
        raise AgentControlError(f"no tmux pane matches target {target!r}")
    if len(matches) > 1:
        raise AgentControlError(
            f"target {target!r} is ambiguous — it matches {len(matches)} panes: "
            f"{', '.join(m['target'] for m in matches)}")

    agent = matches[0]
    evidence = conversation_evidence(agent.get("claude_cwd") or agent.get("cwd") or "")
    rc, out, _ = _tmux(["capture-pane", "-p", "-t", agent["target"], "-S", "-40"])
    recent = redact(out) if rc == 0 else ""
    shell_running, pending = False, ""
    if agent["alive"] and agent["is_agent"] and not _STATE_ACTIVE_RUN_RE.search(recent[-1500:]):
        shell_running = _pane_shell_running(agent)
        if not shell_running:
            pending = _pane_pending_input(agent["target"])
    state = classify_state(agent["alive"], agent["is_agent"], recent,
                           pending_input=pending, shell_running=shell_running)
    audit("agent_status", agent["target"], found=True, alive=agent["alive"], is_agent=agent["is_agent"])
    return {
        "target": agent["target"],
        "session": agent["session"],
        "alive": agent["alive"],
        "is_agent": agent["is_agent"],
        "pid": agent["pid"],
        "claude_pid": agent["claude_pid"],
        "cwd": agent["claude_cwd"] or agent["cwd"],
        "command": agent["command"],
        "claude_cmdline": (agent["claude"] or {}).get("cmdline"),
        "conversation": evidence,
        "state": state,
        "pending": _pending_prompt_info(recent) if state == "waiting_owner" else None,
        "recent_activity": recent[-4000:],
        "checked_at": _now(),
    }


def _pending_prompt_info(pane_tail: str) -> Optional[dict]:
    """For a waiting_owner pane, surface the pending command + its structured
    safety verdict so callers can render an owner decision or auto-continue."""
    try:
        from core import permission_resolver as _pr
    except Exception:  # noqa: BLE001
        return None
    command = _pr.extract_pending_command(pane_tail)
    if not command:
        return {"command": None, "safe": False, "category": "unextractable",
                "hash": _pr.command_hash((pane_tail or "")[-200:])}
    v = _pr.classify_command(command)
    return {"command": command, "safe": v["safe"], "category": v["category"],
            "reason": v["reason"], "hash": v["hash"]}


def conversation_evidence(cwd: str) -> dict:
    """Evidence that a conversation exists for a project, from Claude's own
    project history directory. Read-only and best-effort."""
    if not cwd:
        return {"present": False}
    slug = "-" + cwd.strip("/").replace("/", "-")
    proj_dir = os.path.join(os.path.expanduser("~/.claude/projects"), slug)
    if not os.path.isdir(proj_dir):
        return {"present": False, "project_dir": proj_dir}
    try:
        entries = [f for f in os.listdir(proj_dir) if f.endswith(".jsonl")]
    except OSError:
        return {"present": False, "project_dir": proj_dir}
    sessions = []
    for name in entries:
        full = os.path.join(proj_dir, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        sessions.append({
            "conversation_id": name[:-6],
            "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "size_bytes": st.st_size,
        })
    sessions.sort(key=lambda s: s["modified_at"], reverse=True)
    return {
        "present": bool(sessions),
        "project_dir": proj_dir,
        "count": len(sessions),
        "latest": sessions[0] if sessions else None,
        "sessions": sessions[:10],
    }


def agent_read(target: str, lines: Any = DEFAULT_CAPTURE_LINES) -> dict:
    """Read recent output from a pane, bounded and redacted."""
    validate_target(target)
    n = _bound_lines(lines)
    rc, out, err = _tmux(["capture-pane", "-p", "-t", target, "-S", f"-{n}"])
    if rc != 0:
        audit("agent_read", target, ok=False, error=err.strip()[:120])
        raise AgentControlError(f"cannot read {target!r}: {err.strip()[:200]}")
    text = redact(out)
    captured = text.splitlines()
    # `capture-pane -S -N` starts N lines above the visible pane, so it returns
    # the pane *plus* N — not N lines. The slice is the real bound, and
    # lines_returned must describe what the caller actually got.
    returned = captured[-n:]
    audit("agent_read", target, ok=True, lines=len(returned))
    return {
        "target": target,
        "lines_requested": n,
        "lines_returned": len(returned),
        "lines_available": len(captured),
        "truncated": len(captured) > len(returned),
        "output": "\n".join(returned),
        "captured_at": _now(),
    }


def _pane_is_live_agent(target: str) -> dict:
    """Resolve a target to exactly one live pane, or refuse.

    `send`/`answer` must never create an agent, so a missing target is an error
    here rather than an invitation to start something.
    """
    inventory = agent_list()
    matches = [a for a in inventory["agents"] if a["target"] == target or a["session"] == target]
    if not matches:
        raise AgentControlError(
            f"no existing agent at {target!r} — refusing to create one implicitly")
    if len(matches) > 1:
        raise AgentControlError(
            f"target {target!r} is ambiguous — matches {len(matches)} panes")
    pane = matches[0]
    if not pane["alive"]:
        raise AgentControlError(f"pane {target!r} is dead — refusing to send")
    return pane


def _deliver(target: str, text: str, action: str, idempotency_key: Optional[str]) -> dict:
    """Deliver multiline text to a pane through a tmux buffer.

    A tmux buffer is used rather than `send-keys` because send-keys would
    interpret the payload as key names, mangling multiline text and turning any
    text the owner did not write into a potential key-sequence injection.
    Delivery is *proven* by diffing the pane capture before and after.
    """
    _check_message(text)
    pane = _pane_is_live_agent(target)
    resolved = pane["target"]
    key = idempotency_key or str(uuid.uuid4())

    prior = _seen_delivery(key)
    if prior is not None:
        audit(action, resolved, key, duplicate=True, delivered=False)
        return {**prior, "duplicate": True, "delivered": False,
                "note": "idempotency key already used — not delivered again"}

    rc_before, before, _ = _tmux(["capture-pane", "-p", "-t", resolved, "-S", "-5"])
    buffer_name = f"agentctl-{key[:24]}"
    rc, _, err = _tmux(["load-buffer", "-b", buffer_name, "-"], stdin=text.encode("utf-8"))
    if rc != 0:
        raise AgentControlError(f"failed to stage message: {err.strip()[:200]}")
    try:
        rc, _, err = _tmux(["paste-buffer", "-b", buffer_name, "-t", resolved, "-d"])
        if rc != 0:
            raise AgentControlError(f"failed to paste message: {err.strip()[:200]}")
        # Submit. Enter is sent as a key name, never as part of the payload.
        rc_enter, _, enter_err = _tmux(["send-keys", "-t", resolved, "Enter"])
    finally:
        _tmux(["delete-buffer", "-b", buffer_name])

    time.sleep(0.4)
    rc_after, after, _ = _tmux(["capture-pane", "-p", "-t", resolved, "-S", "-5"])
    pane_changed = (rc_before == 0 and rc_after == 0 and before != after)
    result = {
        "target": resolved,
        "action": action,
        "idempotency_key": key,
        "delivered": rc_enter == 0,
        "submitted": rc_enter == 0,
        "duplicate": False,
        "bytes": len(text.encode("utf-8")),
        "pane_changed": pane_changed,
        "delivery_evidence": redact(after)[-1200:] if rc_after == 0 else None,
        "agent_created": False,
        "delivered_at": _now(),
    }
    if rc_enter != 0:
        result["error"] = enter_err.strip()[:200]
    _record_delivery(key, resolved, action, result)
    audit(action, resolved, key, delivered=result["delivered"], pane_changed=pane_changed,
          bytes=result["bytes"])
    return result


def agent_send(target: str, text: str, idempotency_key: Optional[str] = None) -> dict:
    """Send a message to an existing agent. Never creates one."""
    validate_target(target)
    return _deliver(target, text, "agent_send", idempotency_key)


def agent_answer(target: str, text: str, idempotency_key: Optional[str] = None) -> dict:
    """Answer a prompt an existing agent is waiting on. Never creates one."""
    validate_target(target)
    return _deliver(target, text, "agent_answer", idempotency_key)


def find_live_agent_for_dir(project_dir: str) -> Optional[dict]:
    """The live Claude agent working in `project_dir`, if one exists."""
    resolved = os.path.realpath(project_dir)
    for agent in agent_list()["agents"]:
        if not (agent["is_agent"] and agent["alive"]):
            continue
        agent_cwd = os.path.realpath(agent["claude_cwd"] or agent["cwd"] or "")
        if agent_cwd == resolved:
            return agent
    return None


def agent_resume(project_dir: str, conversation_id: Optional[str] = None,
                 session_name: Optional[str] = None) -> dict:
    """Resume a Claude agent — but only when no matching live one exists.

    Returns `duplicate_created: False` in every path: either an existing agent
    was found and returned untouched, or exactly one new session was created
    after proving none existed.
    """
    resolved = validate_project_dir(project_dir)
    if conversation_id and not _CONVERSATION_RE.match(str(conversation_id)):
        raise AgentControlError(f"invalid conversation id: {conversation_id!r}")

    existing = find_live_agent_for_dir(resolved)
    if existing:
        audit("agent_resume", existing["target"], resumed=False, reason="live agent already exists")
        return {
            "resumed": False,
            "reason": "a live Claude agent already works in this directory — reusing it",
            "existing_agent": {"target": existing["target"], "session": existing["session"],
                               "claude_pid": existing["claude_pid"], "cwd": existing["claude_cwd"]},
            "duplicate_created": False,
            "agent_created": False,
        }

    name = validate_session(session_name or f"agent-{os.path.basename(resolved)[:40]}")
    rc, _, _ = _tmux(["has-session", "-t", name])
    if rc == 0:
        audit("agent_resume", name, resumed=False, reason="tmux session name already exists")
        return {
            "resumed": False,
            "reason": f"tmux session {name!r} already exists — refusing to create a duplicate session",
            "existing_session": name,
            "duplicate_created": False,
            "agent_created": False,
        }

    argv = ["new-session", "-d", "-s", name, "-c", resolved, "claude"]
    if conversation_id:
        argv += ["--resume", str(conversation_id)]
    rc, _, err = _tmux(argv)
    if rc != 0:
        audit("agent_resume", name, resumed=False, error=err.strip()[:120])
        raise AgentControlError(f"failed to start session {name!r}: {err.strip()[:200]}")

    audit("agent_resume", name, resumed=True, conversation_id=conversation_id, cwd=resolved)
    return {
        "resumed": True,
        "session": name,
        "target": f"{name}:0.0",
        "cwd": resolved,
        "conversation_id": conversation_id,
        "duplicate_created": False,
        "agent_created": True,
    }


_REPORT_SUBDIRS = ("reports", "docs")
_REPORT_SUFFIXES = (".md", ".json", ".txt")


def agent_report(project_dir: str, limit: int = 20) -> dict:
    """List report/status files for a project from allowlisted subdirectories."""
    resolved = validate_project_dir(project_dir)
    try:
        limit = max(1, min(int(limit), MAX_REPORT_FILES))
    except (TypeError, ValueError):
        raise AgentControlError(f"invalid limit: {limit!r}")

    found: list[dict] = []
    for sub in _REPORT_SUBDIRS:
        base = os.path.join(resolved, sub)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            # Re-validate every directory we descend into: a symlinked subtree
            # must not walk us out of the project.
            if not os.path.realpath(root).startswith(resolved + os.sep):
                continue
            for name in files:
                if not name.endswith(_REPORT_SUFFIXES):
                    continue
                full = os.path.join(root, name)
                if not os.path.realpath(full).startswith(resolved + os.sep):
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found.append({
                    "path": os.path.relpath(full, resolved),
                    "size_bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
    found.sort(key=lambda f: f["modified_at"], reverse=True)
    audit("agent_report", resolved, files=len(found))
    return {"project_dir": resolved, "count": len(found), "reports": found[:limit],
            "checked_at": _now()}


def agent_report_read(project_dir: str, path: str) -> dict:
    """Read one report file, bounded and redacted. Path must stay in-project."""
    resolved = validate_project_dir(project_dir)
    if not path or os.path.isabs(path) or "\x00" in path:
        raise AgentControlError(f"invalid report path: {path!r}")
    full = os.path.realpath(os.path.join(resolved, path))
    if not (full.startswith(resolved + os.sep) and
            any(full.startswith(os.path.join(resolved, s) + os.sep) for s in _REPORT_SUBDIRS)):
        raise AgentControlError(f"report path {path!r} is outside the allowed report directories")
    if not os.path.isfile(full):
        raise AgentControlError(f"report not found: {path}")
    with open(full, "r", errors="replace") as fh:
        content = fh.read(MAX_REPORT_BYTES + 1)
    truncated = len(content) > MAX_REPORT_BYTES
    audit("agent_report_read", resolved, path=path, truncated=truncated)
    return {"project_dir": resolved, "path": path, "truncated": truncated,
            "content": redact(content[:MAX_REPORT_BYTES])}


def agent_stop(target: str, confirm: bool = False, idempotency_key: Optional[str] = None) -> dict:
    """Stop an agent. Destructive: requires explicit confirmation and an
    unambiguous target, and only ever kills the resolved pane."""
    validate_target(target)
    if confirm is not True:
        audit("agent_stop", target, stopped=False, reason="not confirmed")
        raise AgentControlError(
            "agent_stop is destructive and requires confirm=true for the exact target")

    inventory = agent_list()
    matches = [a for a in inventory["agents"] if a["target"] == target or a["session"] == target]
    if not matches:
        raise AgentControlError(f"no pane matches target {target!r}")
    if len(matches) > 1:
        raise AgentControlError(
            f"target {target!r} is ambiguous — matches {len(matches)} panes: "
            f"{', '.join(m['target'] for m in matches)} — refusing to stop any of them")

    pane = matches[0]
    key = idempotency_key or str(uuid.uuid4())
    prior = _seen_delivery(key)
    if prior is not None:
        return {**prior, "duplicate": True, "stopped": False}

    # kill-pane, never kill-session: only the resolved target dies, and any
    # unrelated pane sharing the session survives.
    rc, _, err = _tmux(["kill-pane", "-t", pane["target"]])
    result = {
        "target": pane["target"],
        "stopped": rc == 0,
        "killed_pid": pane["claude_pid"] or pane["pid"],
        "idempotency_key": key,
        "duplicate": False,
        "stopped_at": _now(),
    }
    if rc != 0:
        result["error"] = err.strip()[:200]
    _record_delivery(key, pane["target"], "agent_stop", result)
    audit("agent_stop", pane["target"], key, stopped=result["stopped"])
    return result
