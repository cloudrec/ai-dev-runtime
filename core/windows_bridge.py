"""Windows Agent Bridge (task 220) — server half.

Owner OS already controls the Claude Code agents that run in tmux ON THIS HOST
(core.agent_control) and the Runtime workers in core.job_store, and presents
both through one inventory (core.agent_fabric). The owner's Windows PC is a
third kind of place where Claude Code runs, and it must be reachable with the
same verbs — list / status / read / send / resume / stop — without becoming a
second control plane and without ever becoming a remote shell.

Why a queue instead of an RPC
-----------------------------
The Windows machine sits behind NAT on a home network. Nothing may listen for
inbound connections there: the ONLY direction that ever opens a socket is
Windows -> Owner OS, over TLS. So this module is a durable command queue that
the device drains by long-polling. The owner-facing API enqueues a command and
waits for its result; the device leases it, executes it inside an explicitly
enrolled workspace, and posts the result back. If the laptop is asleep the
command simply expires — an offline device is a refusal with a reason, never a
hang and never a half-applied action.

Security properties this module is responsible for
--------------------------------------------------
* **Per-device identity.** Enrollment is a single-use, expiring code that the
  owner generates and types once. It is exchanged for a device id and a device
  secret that never travels again — every later request is HMAC-SHA256 signed
  over (device, timestamp, nonce, path, body-hash). The secret is rotatable and
  the device is revocable, both without touching any other device.
* **No replay.** A signature is only accepted inside a clock-skew window AND
  only once: the nonce is recorded per device and a second use is refused.
  Signing the body hash and the path means a captured signature cannot be
  re-pointed at a different command or a different route.
* **No arbitrary execution.** ACTIONS below is the entire remote surface. There
  is deliberately no "run command" verb; adding one would be a design change,
  not a parameter.
* **No paths on the wire.** A command names a workspace ID, never a filesystem
  path. The device resolves that ID against its own local enrollment file, so a
  traversal payload has nowhere to land — the server literally cannot express
  "and by the way, run it in C:\\Windows\\System32".
* **Idempotent commands.** Every command carries a UUID; re-enqueuing the same
  id returns the existing row instead of running the work twice, and a device
  that re-posts a result for a completed command is a no-op.
* **Redacted transport.** Everything a device returns passes through
  agent_control.redact() before it is stored, so a pane full of environment
  variables cannot turn into a credential leak in the control plane database.

What this module deliberately does NOT do: talk to the network, start
processes, or interpret Claude output. It decides, records and hands over.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from typing import Any, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# ── bounds ──────────────────────────────────────────────────────────────────
MAX_TEXT_BYTES = 16384          # one message to an agent (same ceiling as tmux send)
MAX_RESULT_BYTES = 262144       # one result posted back by a device
MAX_LINES = 2000
DEFAULT_LINES = 200
MAX_WORKSPACES = 64             # per device
MAX_LEASE = 16                  # commands handed to one poll
CLOCK_SKEW_SECS = 300           # signature freshness window
NONCE_TTL_SECS = 900            # how long a used nonce stays refused
COMMAND_TTL_SECS = 900          # a pending command older than this is expired
DEVICE_ONLINE_SECS = 180        # no poll within this -> offline
ENROLL_CODE_TTL_SECS = 900
MAX_POLL_WAIT_SECS = 55         # long-poll ceiling (under any 60s proxy timeout)

# ── validation ──────────────────────────────────────────────────────────────
# Device ids are generated here, never supplied by the client.
_DEVICE_RE = re.compile(r"^win-[0-9a-f]{16}$")
# Workspace ids are chosen by the owner on the Windows side and are the ONLY
# way a command addresses a directory. Stricter than a filename on purpose: no
# dots, no slashes, no backslashes, no spaces — a traversal cannot be spelled.
_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LABEL_RE = re.compile(r"^[\w .:()\\/-]{0,120}$")
_NONCE_RE = re.compile(r"^[0-9a-zA-Z_-]{8,64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# The complete remote surface. Nothing outside this dict can ever be asked of a
# Windows device; `params` names the keys each action accepts, and anything
# else in the payload is a refusal rather than an ignored extra.
ACTIONS: dict[str, tuple[str, ...]] = {
    "workspace.list": (),                      # no workspace_id — device-wide
    "agent.status": (),
    "agent.read": ("lines",),
    "agent.start": ("text", "idempotency_key"),
    "agent.send": ("text", "idempotency_key"),
    "agent.stop": ("confirm",),
    # Read-only repository/tree inspection of ONE enrolled workspace. Added for
    # the GAIKA reconciliation: comparing two copies of a repo needs facts from
    # the Windows side (branch, HEAD, remotes, dirty files, content hashes), and
    # the alternative - asking a Claude on that machine to run commands - is both
    # slower and exactly the arbitrary execution this surface refuses. The device
    # runs a FIXED set of git argv lists and hashes files; nothing in `params`
    # reaches a command line.
    "workspace.inspect": ("max_files",),
}
# Actions that address the device rather than one workspace.
_DEVICE_ACTIONS = ("workspace.list",)

_COMMAND_STATES = ("pending", "leased", "done", "failed", "expired")


class WindowsBridgeError(Exception):
    """Refusal with an exact reason — mapped to HTTP 400/401 by the API layer."""


class AuthError(WindowsBridgeError):
    """Authentication/replay refusal — mapped to HTTP 401."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS win_device (
    device_id TEXT PRIMARY KEY,
    name TEXT, secret TEXT, status TEXT DEFAULT 'active',
    os_version TEXT, agent_version TEXT,
    created_at TEXT, last_seen_at TEXT, last_seen_ts REAL DEFAULT 0,
    rotated_at TEXT, revoked_at TEXT, enrolled_from TEXT
);
CREATE TABLE IF NOT EXISTS win_enrollment (
    code_hash TEXT PRIMARY KEY, label TEXT,
    created_at TEXT, expires_ts REAL,
    used_at TEXT, device_id TEXT
);
CREATE TABLE IF NOT EXISTS win_workspace (
    device_id TEXT, workspace_id TEXT, label TEXT, path_hint TEXT,
    state TEXT, session_id TEXT, enabled INTEGER DEFAULT 1,
    first_seen_at TEXT, updated_at TEXT,
    PRIMARY KEY (device_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS win_command (
    command_id TEXT PRIMARY KEY,
    device_id TEXT, workspace_id TEXT, action TEXT, params TEXT,
    status TEXT DEFAULT 'pending', created_by TEXT,
    created_at TEXT, created_ts REAL, leased_ts REAL,
    completed_at TEXT, ok INTEGER, result TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS win_nonce (
    device_id TEXT, nonce TEXT, ts REAL,
    PRIMARY KEY (device_id, nonce)
);
CREATE INDEX IF NOT EXISTS ix_win_command_device
    ON win_command (device_id, status, created_ts)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def _row(cur) -> Optional[dict]:
    r = cur.fetchone()
    if r is None:
        return None
    return {d[0]: r[i] for i, d in enumerate(cur.description)}


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _redact(text: str) -> str:
    """Every byte a device sends back goes through the control plane's own
    redactor — a Windows pane is exactly as likely to hold an API key as a
    tmux one."""
    from core import agent_control
    return agent_control.redact(text or "")


def _redact_obj(obj: Any, _depth: int = 0) -> Any:
    """Redact INSIDE a structure, never across its serialization.

    Running the redactor over a finished JSON document corrupts it: the
    `KEY=value` rule ends its match at the optional closing quote, so
    `{"tail": "API_KEY=hunter2"}` comes back missing a quote and no longer
    parses. Redacting each string value and then serializing keeps the document
    valid AND still strips the secret."""
    if _depth > 12:
        return "***DEPTH LIMIT***"
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, dict):
        return {str(k): _redact_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v, _depth + 1) for v in obj]
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _redact(str(obj))


def _audit(type_: str, device_id: str, **payload: Any) -> None:
    """Best-effort event-log entry. An audit failure must never fail the
    operation it describes, but a silent bridge would be worse than no bridge."""
    try:
        from core.control_plane.api import append_event
        append_event("windows_bridge", type_, entity_type="win_device",
                     entity_id=device_id, payload=payload, severity="info")
    except Exception:  # noqa: BLE001
        pass


# ── enrollment ──────────────────────────────────────────────────────────────

def _hash_code(code: str) -> str:
    return hashlib.sha256(("oos-enroll:" + (code or "").strip().upper()).encode()).hexdigest()


def _format_code(raw: bytes) -> str:
    body = base64.b32encode(raw).decode().rstrip("=")
    return "OOS-" + "-".join(body[i:i + 5] for i in range(0, 15, 5))


def create_enrollment_code(label: str = "", ttl_secs: int = ENROLL_CODE_TTL_SECS,
                           conn=None, now: Optional[float] = None) -> dict:
    """Mint a single-use, expiring enrollment code. The plaintext is returned
    EXACTLY once and is never stored — only its hash is, so a dump of the
    control plane database cannot enroll a device."""
    if not _LABEL_RE.match(label or ""):
        raise WindowsBridgeError("label must be <=120 chars of [word . : ( ) / \\ -]")
    ttl = max(60, min(int(ttl_secs or ENROLL_CODE_TTL_SECS), 86400))
    now = now if now is not None else now_ts()
    code = _format_code(secrets.token_bytes(10))
    conn, own = _conn(conn)
    try:
        conn.execute("INSERT INTO win_enrollment (code_hash,label,created_at,expires_ts) "
                     "VALUES (?,?,?,?)",
                     (_hash_code(code), label or "", now_iso(), now + ttl))
        conn.commit()
    finally:
        if own:
            conn.close()
    # The plaintext code leaves this process once, to the owner. Never logged.
    return {"code": code, "label": label or "", "expires_in_secs": ttl,
            "expires_ts": now + ttl}


def enroll(code: str, *, device_name: str = "", os_version: str = "",
           agent_version: str = "", enrolled_from: str = "", conn=None,
           now: Optional[float] = None) -> dict:
    """Exchange a valid code for a device identity + secret. Single use: the
    code is consumed atomically, so a code observed in flight is worthless once
    the real device has used it."""
    if not _LABEL_RE.match(device_name or ""):
        raise WindowsBridgeError("device_name must be <=120 chars of [word . : ( ) / \\ -]")
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        row = _row(conn.execute(
            "SELECT code_hash, expires_ts, used_at FROM win_enrollment WHERE code_hash=?",
            (_hash_code(code),)))
        if not row:
            raise AuthError("unknown enrollment code")
        if row["used_at"]:
            raise AuthError("enrollment code already used")
        if float(row["expires_ts"] or 0) < now:
            raise AuthError("enrollment code expired")
        device_id = "win-" + secrets.token_hex(8)
        secret = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO win_device (device_id,name,secret,status,os_version,"
            "agent_version,created_at,last_seen_at,last_seen_ts,enrolled_from) "
            "VALUES (?,?,?,'active',?,?,?,?,?,?)",
            (device_id, (device_name or device_id)[:120], secret,
             (os_version or "")[:120], (agent_version or "")[:40], now_iso(),
             now_iso(), now, (enrolled_from or "")[:60]))
        # Consume the code in the same transaction as the device it created:
        # a crash between the two would otherwise leave a reusable code.
        cur = conn.execute(
            "UPDATE win_enrollment SET used_at=?, device_id=? "
            "WHERE code_hash=? AND used_at IS NULL",
            (now_iso(), device_id, row["code_hash"]))
        if cur.rowcount != 1:
            conn.rollback()
            raise AuthError("enrollment code already used")
        conn.commit()
    finally:
        if own:
            conn.close()
    _audit("windows_device_enrolled", device_id, name=device_name[:120],
           os_version=os_version[:120], agent_version=agent_version[:40])
    return {"device_id": device_id, "secret": secret, "server_time": now_iso()}


# ── device identity / signing ───────────────────────────────────────────────

def canonical_request(device_id: str, ts: str, nonce: str, path: str, body: bytes) -> str:
    """What the signature actually covers. Binding the PATH and the BODY HASH
    (not just a timestamp, which is how the runtime's own bearer+HMAC path
    works) means a captured signature cannot be replayed against a different
    route or a different payload."""
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join(["oos-win-v1", device_id, str(ts), nonce, path, body_hash])


def sign(secret: str, device_id: str, ts: str, nonce: str, path: str, body: bytes) -> str:
    return hmac.new(secret.encode(),
                    canonical_request(device_id, ts, nonce, path, body).encode(),
                    hashlib.sha256).hexdigest()


def get_device(device_id: str, conn=None) -> Optional[dict]:
    conn, own = _conn(conn)
    try:
        d = _row(conn.execute("SELECT * FROM win_device WHERE device_id=?", (device_id,)))
    finally:
        if own:
            conn.close()
    return d


def _public_device(d: dict, now: Optional[float] = None) -> dict:
    now = now if now is not None else now_ts()
    last = float(d.get("last_seen_ts") or 0)
    return {"device_id": d["device_id"], "name": d.get("name") or "",
            "status": d.get("status") or "active",
            "os_version": d.get("os_version") or "",
            "agent_version": d.get("agent_version") or "",
            "created_at": d.get("created_at"), "last_seen_at": d.get("last_seen_at"),
            "online": bool(d.get("status") == "active" and last and
                           (now - last) <= DEVICE_ONLINE_SECS),
            "seconds_since_seen": round(now - last, 1) if last else None}


def verify_request(device_id: str, ts: Any, nonce: str, path: str, body: bytes,
                   signature: str, conn=None, now: Optional[float] = None) -> dict:
    """Authenticate one device request. Raises AuthError with an exact reason;
    returns the device row on success and burns the nonce."""
    now = now if now is not None else now_ts()
    if not device_id or not _DEVICE_RE.match(device_id or ""):
        raise AuthError("bad device id")
    if not nonce or not _NONCE_RE.match(nonce or ""):
        raise AuthError("bad nonce")
    if not signature or not _HEX_RE.match((signature or "").lower()):
        raise AuthError("bad signature format")
    try:
        ts_val = float(ts)
    except (TypeError, ValueError):
        raise AuthError("bad timestamp")
    if abs(now - ts_val) > CLOCK_SKEW_SECS:
        raise AuthError("stale request (replay window)")

    conn, own = _conn(conn)
    try:
        device = _row(conn.execute("SELECT * FROM win_device WHERE device_id=?",
                                   (device_id,)))
        if not device:
            raise AuthError("unknown device")
        if (device.get("status") or "active") != "active":
            raise AuthError(f"device {device.get('status')}")
        expected = sign(device["secret"], device_id, str(ts), nonce, path, body)
        if not hmac.compare_digest(expected, (signature or "").lower()):
            raise AuthError("bad signature")
        # Nonce burn AFTER the signature check, so an unauthenticated caller
        # cannot exhaust a device's nonce space by guessing.
        conn.execute("DELETE FROM win_nonce WHERE ts < ?", (now - NONCE_TTL_SECS,))
        try:
            conn.execute("INSERT INTO win_nonce (device_id,nonce,ts) VALUES (?,?,?)",
                         (device_id, nonce, now))
        except Exception:  # noqa: BLE001 — PRIMARY KEY collision == replay
            conn.rollback()
            raise AuthError("nonce already used (replay)")
        conn.commit()
        return device
    finally:
        if own:
            conn.close()


def touch_device(device_id: str, *, agent_version: str = "", os_version: str = "",
                 conn=None, now: Optional[float] = None) -> None:
    """Heartbeat. Every authenticated request is one — a device that is polling
    is a device that is alive; there is no separate keepalive to get out of sync
    with the work."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute("UPDATE win_device SET last_seen_at=?, last_seen_ts=?, "
                     "agent_version=COALESCE(NULLIF(?,''),agent_version), "
                     "os_version=COALESCE(NULLIF(?,''),os_version) WHERE device_id=?",
                     (now_iso(), now, (agent_version or "")[:40],
                      (os_version or "")[:120], device_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def rotate_secret(device_id: str, conn=None) -> dict:
    """Device-initiated rotation over an already-authenticated request. The new
    secret is returned once; the old one stops working immediately."""
    conn, own = _conn(conn)
    try:
        if not _row(conn.execute("SELECT device_id FROM win_device WHERE device_id=?",
                                 (device_id,))):
            raise WindowsBridgeError(f"unknown device {device_id}")
        secret = secrets.token_hex(32)
        conn.execute("UPDATE win_device SET secret=?, rotated_at=? WHERE device_id=?",
                     (secret, now_iso(), device_id))
        conn.commit()
    finally:
        if own:
            conn.close()
    _audit("windows_device_rotated", device_id)
    return {"device_id": device_id, "secret": secret}


def revoke_device(device_id: str, *, reason: str = "", conn=None) -> dict:
    """Kill one device without touching any other. Pending commands for it are
    expired rather than left to be leased by a re-enrolled impostor."""
    conn, own = _conn(conn)
    try:
        cur = conn.execute("UPDATE win_device SET status='revoked', revoked_at=? "
                           "WHERE device_id=?", (now_iso(), device_id))
        if cur.rowcount != 1:
            raise WindowsBridgeError(f"unknown device {device_id}")
        conn.execute("UPDATE win_command SET status='expired', error='device revoked' "
                     "WHERE device_id=? AND status IN ('pending','leased')", (device_id,))
        conn.commit()
    finally:
        if own:
            conn.close()
    _audit("windows_device_revoked", device_id, reason=reason[:200])
    return {"device_id": device_id, "status": "revoked"}


def list_devices(conn=None, now: Optional[float] = None) -> dict:
    conn, own = _conn(conn)
    try:
        rows = _rows(conn.execute("SELECT * FROM win_device ORDER BY created_at DESC"))
    finally:
        if own:
            conn.close()
    return {"devices": [_public_device(d, now=now) for d in rows]}


# ── workspaces (the device is the authority on what is enrolled) ────────────

def report_workspaces(device_id: str, workspaces: Any, conn=None,
                      now: Optional[float] = None) -> dict:
    """The device tells the server which workspaces the owner enrolled LOCALLY.
    The server never learns a filesystem path it could send back: `path_hint` is
    display-only and is never used to address anything.

    Enrollment is therefore always an explicit local act on the Windows machine
    — the server cannot add a workspace to a device, only refuse one."""
    if not isinstance(workspaces, list):
        raise WindowsBridgeError("workspaces must be a list")
    if len(workspaces) > MAX_WORKSPACES:
        raise WindowsBridgeError(f"too many workspaces (max {MAX_WORKSPACES})")
    seen, clean = set(), []
    for w in workspaces:
        if not isinstance(w, dict):
            raise WindowsBridgeError("each workspace must be an object")
        wid = (w.get("workspace_id") or "").strip()
        if not _WORKSPACE_RE.match(wid):
            raise WindowsBridgeError(f"bad workspace_id {wid!r}")
        if wid in seen:
            raise WindowsBridgeError(f"duplicate workspace_id {wid!r}")
        seen.add(wid)
        clean.append({
            "workspace_id": wid,
            "label": (w.get("label") or "")[:120],
            "path_hint": _redact((w.get("path_hint") or ""))[:300],
            "state": (w.get("state") or "unknown")[:40],
            "session_id": (w.get("session_id") or "")[:80],
        })
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        for w in clean:
            existing = _row(conn.execute(
                "SELECT first_seen_at FROM win_workspace WHERE device_id=? AND workspace_id=?",
                (device_id, w["workspace_id"])))
            if existing:
                conn.execute(
                    "UPDATE win_workspace SET label=?, path_hint=?, state=?, session_id=?, "
                    "updated_at=? WHERE device_id=? AND workspace_id=?",
                    (w["label"], w["path_hint"], w["state"], w["session_id"], now_iso(),
                     device_id, w["workspace_id"]))
            else:
                conn.execute(
                    "INSERT INTO win_workspace (device_id,workspace_id,label,path_hint,"
                    "state,session_id,enabled,first_seen_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,1,?,?)",
                    (device_id, w["workspace_id"], w["label"], w["path_hint"],
                     w["state"], w["session_id"], now_iso(), now_iso()))
        # A workspace the device stopped reporting is un-enrolled locally, so it
        # must stop being addressable here too.
        if clean:
            ph = ",".join("?" * len(clean))
            conn.execute(f"DELETE FROM win_workspace WHERE device_id=? "
                         f"AND workspace_id NOT IN ({ph})",
                         [device_id] + [w["workspace_id"] for w in clean])
        else:
            conn.execute("DELETE FROM win_workspace WHERE device_id=?", (device_id,))
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"device_id": device_id, "count": len(clean)}


def list_workspaces(device_id: str = "", conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        if device_id:
            rows = _rows(conn.execute(
                "SELECT * FROM win_workspace WHERE device_id=? ORDER BY workspace_id",
                (device_id,)))
        else:
            rows = _rows(conn.execute(
                "SELECT * FROM win_workspace ORDER BY device_id, workspace_id"))
    finally:
        if own:
            conn.close()
    return {"workspaces": rows, "count": len(rows)}


def set_workspace_enabled(device_id: str, workspace_id: str, enabled: bool,
                          conn=None) -> dict:
    """The owner's server-side off switch for one workspace. Local enrollment
    grants reachability; this can withdraw it without touching the device."""
    conn, own = _conn(conn)
    try:
        cur = conn.execute("UPDATE win_workspace SET enabled=?, updated_at=? "
                           "WHERE device_id=? AND workspace_id=?",
                           (int(bool(enabled)), now_iso(), device_id, workspace_id))
        if cur.rowcount != 1:
            raise WindowsBridgeError(f"unknown workspace {device_id}/{workspace_id}")
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"device_id": device_id, "workspace_id": workspace_id,
            "enabled": bool(enabled)}


# ── commands ────────────────────────────────────────────────────────────────

def validate_params(action: str, params: Any) -> dict:
    """Closed-vocabulary parameter check. Unknown keys are a refusal, not an
    ignored extra: silently dropping a key the caller believed in is how a
    "confirm" ends up meaning nothing."""
    if action not in ACTIONS:
        raise WindowsBridgeError(
            f"unknown action {action!r}; allowed: {tuple(ACTIONS)}")
    params = params or {}
    if not isinstance(params, dict):
        raise WindowsBridgeError("params must be an object")
    allowed = set(ACTIONS[action])
    extra = set(params) - allowed
    if extra:
        raise WindowsBridgeError(
            f"params {sorted(extra)} not accepted by {action} (allowed: {sorted(allowed)})")
    out: dict[str, Any] = {}
    if "text" in allowed:
        text = params.get("text")
        if action == "agent.send" and not (text or "").strip():
            raise WindowsBridgeError("agent.send requires non-empty text")
        text = text or ""
        if not isinstance(text, str):
            raise WindowsBridgeError("text must be a string")
        if len(text.encode("utf-8", "ignore")) > MAX_TEXT_BYTES:
            raise WindowsBridgeError(f"text exceeds {MAX_TEXT_BYTES} bytes")
        if "\x00" in text:
            raise WindowsBridgeError("text may not contain NUL")
        out["text"] = text
    if "max_files" in allowed:
        try:
            mx = int(params.get("max_files") or 500)
        except (TypeError, ValueError):
            raise WindowsBridgeError("max_files must be an integer")
        out["max_files"] = max(1, min(mx, 2000))
    if "lines" in allowed:
        try:
            lines = int(params.get("lines") or DEFAULT_LINES)
        except (TypeError, ValueError):
            raise WindowsBridgeError("lines must be an integer")
        out["lines"] = max(1, min(lines, MAX_LINES))
    if "confirm" in allowed:
        confirm = bool(params.get("confirm"))
        if action == "agent.stop" and not confirm:
            raise WindowsBridgeError("agent.stop requires confirm=true")
        out["confirm"] = confirm
    if "idempotency_key" in allowed and params.get("idempotency_key"):
        key = str(params["idempotency_key"])[:80]
        if not re.match(r"^[\w:-]{1,80}$", key):
            raise WindowsBridgeError("idempotency_key must be [A-Za-z0-9_:-]{1,80}")
        out["idempotency_key"] = key
    return out


def enqueue(device_id: str, action: str, *, workspace_id: str = "",
            params: Optional[dict] = None, command_id: str = "",
            created_by: str = "owner", conn=None,
            now: Optional[float] = None) -> dict:
    """Queue one allowlisted command for a device. Idempotent on command_id."""
    clean_params = validate_params(action, params)
    workspace_id = (workspace_id or "").strip()
    if action in _DEVICE_ACTIONS:
        if workspace_id:
            raise WindowsBridgeError(f"{action} addresses the device, not a workspace")
    else:
        if not _WORKSPACE_RE.match(workspace_id):
            raise WindowsBridgeError(f"bad workspace_id {workspace_id!r}")
    command_id = (command_id or str(uuid.uuid4())).strip()
    if not re.match(r"^[0-9a-fA-F-]{8,64}$", command_id):
        raise WindowsBridgeError("command_id must be a UUID-shaped token")
    now = now if now is not None else now_ts()

    conn, own = _conn(conn)
    try:
        existing = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                     (command_id,)))
        if existing:
            # Replaying the same id is a lookup, never a second execution.
            return _public_command(existing)
        device = _row(conn.execute("SELECT device_id, status FROM win_device "
                                   "WHERE device_id=?", (device_id,)))
        if not device:
            raise WindowsBridgeError(f"unknown device {device_id}")
        if (device["status"] or "active") != "active":
            raise WindowsBridgeError(f"device {device_id} is {device['status']}")
        if workspace_id:
            ws = _row(conn.execute(
                "SELECT enabled FROM win_workspace WHERE device_id=? AND workspace_id=?",
                (device_id, workspace_id)))
            if not ws:
                raise WindowsBridgeError(
                    f"workspace {workspace_id!r} is not enrolled on {device_id} "
                    f"(enroll it on the Windows machine first)")
            if not int(ws["enabled"] or 0):
                raise WindowsBridgeError(f"workspace {workspace_id!r} is disabled")
        conn.execute(
            "INSERT INTO win_command (command_id,device_id,workspace_id,action,params,"
            "status,created_by,created_at,created_ts) VALUES (?,?,?,?,?,'pending',?,?,?)",
            (command_id, device_id, workspace_id, action, json.dumps(clean_params),
             (created_by or "owner")[:60], now_iso(), now))
        conn.commit()
        row = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                (command_id,)))
    finally:
        if own:
            conn.close()
    return _public_command(row)


def _public_command(row: dict) -> dict:
    params = row.get("params")
    try:
        params = json.loads(params) if params else {}
    except Exception:  # noqa: BLE001
        params = {}
    result = row.get("result")
    try:
        result = json.loads(result) if result else None
    except Exception:  # noqa: BLE001
        result = None
    return {"command_id": row["command_id"], "device_id": row["device_id"],
            "workspace_id": row.get("workspace_id") or "", "action": row["action"],
            "params": params, "status": row.get("status"),
            "created_at": row.get("created_at"), "completed_at": row.get("completed_at"),
            "ok": None if row.get("ok") is None else bool(row["ok"]),
            "result": result, "error": row.get("error") or ""}


def expire_stale(conn=None, now: Optional[float] = None) -> int:
    """A command nobody could deliver is a refusal with a reason, not a hang."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "UPDATE win_command SET status='expired', "
            "error='device did not collect the command in time', completed_at=? "
            "WHERE status IN ('pending','leased') AND created_ts < ?",
            (now_iso(), now - COMMAND_TTL_SECS))
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def lease(device_id: str, *, max_commands: int = MAX_LEASE, conn=None,
          now: Optional[float] = None) -> list[dict]:
    """Hand this device its pending commands and mark them leased."""
    now = now if now is not None else now_ts()
    expire_stale(conn=conn, now=now)
    limit = max(1, min(int(max_commands or MAX_LEASE), MAX_LEASE))
    conn, own = _conn(conn)
    try:
        rows = _rows(conn.execute(
            "SELECT * FROM win_command WHERE device_id=? AND status='pending' "
            "ORDER BY created_ts LIMIT ?", (device_id, limit)))
        for r in rows:
            conn.execute("UPDATE win_command SET status='leased', leased_ts=? "
                         "WHERE command_id=? AND status='pending'",
                         (now, r["command_id"]))
        conn.commit()
    finally:
        if own:
            conn.close()
    return [_public_command(r) for r in rows]


def complete(device_id: str, command_id: str, *, ok: bool, result: Any = None,
             error: str = "", conn=None, now: Optional[float] = None) -> dict:
    """Record a device's answer. Bounded, redacted, and only for a command that
    actually belongs to this device — a device can never answer for another."""
    now = now if now is not None else now_ts()
    payload = json.dumps(_redact_obj(result if result is not None else {}),
                         ensure_ascii=False, default=str)
    if len(payload.encode("utf-8", "ignore")) > MAX_RESULT_BYTES:
        payload = json.dumps({"truncated": True,
                              "note": f"result exceeded {MAX_RESULT_BYTES} bytes"})
    conn, own = _conn(conn)
    try:
        row = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                (command_id,)))
        if not row:
            raise WindowsBridgeError(f"unknown command {command_id}")
        if row["device_id"] != device_id:
            raise AuthError("command belongs to a different device")
        if row["status"] in ("done", "failed"):
            return _public_command(row)      # idempotent re-post
        if row["status"] == "expired":
            # The owner has ALREADY been told this command was refused, and may
            # have re-issued it on that basis. Letting a late device flip
            # expired -> done would retroactively turn a refusal into a success
            # and hide a double execution — precisely the "half-applied action"
            # this module's contract disclaims. `expire_stale` retires `leased`
            # commands too, so this is reachable whenever a device takes work and
            # then goes dark mid-execution.
            #
            # The late result is not stored (that would overwrite the refusal the
            # owner saw) but it is NOT silent either: it is audited, because a
            # device reporting work against an expired command means that work
            # probably ran.
            _audit("windows_late_result_after_expiry", device_id,
                   command_id=command_id, action=row.get("action"),
                   reported_ok=bool(ok))
            return _public_command(row)
        conn.execute(
            "UPDATE win_command SET status=?, ok=?, result=?, error=?, completed_at=? "
            "WHERE command_id=?",
            ("done" if ok else "failed", int(bool(ok)), payload,
             _redact(str(error or ""))[:500], now_iso(), command_id))
        conn.commit()
        row = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                (command_id,)))
    finally:
        if own:
            conn.close()
    if not ok:
        _audit("windows_command_failed", device_id, command_id=command_id,
               action=row.get("action"), error=str(error or "")[:200])
    return _public_command(row)


def get_command(command_id: str, conn=None) -> Optional[dict]:
    """Read one command, expiring it in place if its TTL has passed.

    `expire_stale()` only ever ran inside `lease()`, i.e. only when the device
    polled. A device that never comes back therefore left its commands `pending`
    forever: `wait_for_result` could never observe `expired` and always exited
    via `timed_out`, and the status endpoint reported `pending` indefinitely.
    That contradicts this module's stated contract — "if the laptop is asleep the
    command simply expires ... never a hang" — precisely in the case the contract
    is about.

    Only the row being read is touched, and only when it is genuinely stale, so
    the common read takes no write lock."""
    conn, own = _conn(conn)
    try:
        row = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                (command_id,)))
        if (row and row["status"] in ("pending", "leased")
                and float(row["created_ts"] or 0) < now_ts() - COMMAND_TTL_SECS):
            conn.execute(
                "UPDATE win_command SET status='expired', "
                "error='device did not collect the command in time', completed_at=? "
                "WHERE command_id=? AND status IN ('pending','leased')",
                (now_iso(), command_id))
            conn.commit()
            row = _row(conn.execute("SELECT * FROM win_command WHERE command_id=?",
                                    (command_id,)))
    finally:
        if own:
            conn.close()
    return _public_command(row) if row else None


def wait_for_result(command_id: str, *, timeout_secs: float = 30.0,
                    poll_secs: float = 0.25, conn=None) -> dict:
    """Block until a command reaches a terminal state or the timeout expires.
    A timeout returns the command in its current state with `timed_out` set —
    the work may still land later, and pretending otherwise would invent a
    failure the device never reported."""
    deadline = time.monotonic() + max(0.0, float(timeout_secs))
    while True:
        cmd = get_command(command_id, conn=conn)
        if cmd and cmd["status"] in ("done", "failed", "expired"):
            return {**cmd, "timed_out": False}
        if time.monotonic() >= deadline:
            return {**(cmd or {"command_id": command_id, "status": "unknown"}),
                    "timed_out": True}
        time.sleep(poll_secs)


def dispatch(device_id: str, action: str, *, workspace_id: str = "",
             params: Optional[dict] = None, command_id: str = "",
             created_by: str = "owner", wait_secs: float = 0.0) -> dict:
    """Enqueue + optionally wait. The one call the owner-facing API and the
    agent fabric both use, so there is exactly one place where a Windows
    command is created."""
    cmd = enqueue(device_id, action, workspace_id=workspace_id, params=params,
                  command_id=command_id, created_by=created_by)
    if wait_secs and cmd["status"] not in ("done", "failed", "expired"):
        return wait_for_result(cmd["command_id"], timeout_secs=wait_secs)
    return {**cmd, "timed_out": False}


# ── inventory (feeds core.agent_fabric) ─────────────────────────────────────

# Windows workspace state -> Task Contract fabric state, mirroring the tmux map
# in core.agent_fabric so one inventory speaks one vocabulary.
FABRIC_STATE = {
    "working": "WORKING",
    "idle": "WORKING",
    "waiting_owner": "OWNER_DECISION",
    "waiting_input": "BLOCKED",
    "error": "VERIFICATION_FAILED",
    "unknown": "WORKING",
}


def inventory(conn=None, now: Optional[float] = None) -> list[dict]:
    """Every enrolled workspace on every active device, in the fabric's shape.
    An offline device still lists its workspaces — with alive=false — because
    "the laptop is asleep" is information the owner needs, not a row to hide."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        devices = {d["device_id"]: d for d in
                   _rows(conn.execute("SELECT * FROM win_device WHERE status='active'"))}
        rows = _rows(conn.execute("SELECT * FROM win_workspace ORDER BY device_id, "
                                  "workspace_id"))
    finally:
        if own:
            conn.close()
    out = []
    for w in rows:
        d = devices.get(w["device_id"])
        if not d:
            continue
        pub = _public_device(d, now=now)
        state = (w.get("state") or "unknown").strip() or "unknown"
        out.append({
            "ref": f"win:{w['device_id']}:{w['workspace_id']}",
            "kind": "win",
            "platform": "windows",
            "project": w["workspace_id"],
            "server": pub["name"] or w["device_id"],
            "device_id": w["device_id"],
            "workspace_id": w["workspace_id"],
            "cwd": w.get("path_hint") or "",
            "tmux_target": "",
            "session_id": w.get("session_id") or "",
            "model": "",
            "state": state,
            "fabric_state": FABRIC_STATE.get(state, "WORKING"),
            "current_task": w.get("label") or "",
            "last_activity": w.get("updated_at") or "",
            "alive": bool(pub["online"] and int(w.get("enabled") or 0)),
            "healthy": bool(pub["online"] and state != "error"),
            "enabled": bool(int(w.get("enabled") or 0)),
            "online": pub["online"],
            "capabilities": ["send", "read", "status", "stop", "resume"],
        })
    return out


def policy() -> dict:
    """Static description of the remote surface — for docs, the adapter UI and
    anyone auditing what a Windows device can be asked to do."""
    return {
        "actions": {a: list(p) for a, p in ACTIONS.items()},
        "device_actions": list(_DEVICE_ACTIONS),
        "ref_format": "win:<device_id>:<workspace_id>",
        "limits": {"max_text_bytes": MAX_TEXT_BYTES, "max_result_bytes": MAX_RESULT_BYTES,
                   "max_lines": MAX_LINES, "max_workspaces": MAX_WORKSPACES,
                   "clock_skew_secs": CLOCK_SKEW_SECS,
                   "command_ttl_secs": COMMAND_TTL_SECS,
                   "device_online_secs": DEVICE_ONLINE_SECS},
        "no_shell": True,
        "paths_on_the_wire": False,
    }
