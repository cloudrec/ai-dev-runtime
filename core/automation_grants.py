"""Owner delegation grants: one approval lets ChatGPT/Owner OS (caller class `api`) act on an
explicit set of existing agents and, optionally, create workers in explicit worktrees —
until the grant expires or is revoked. No per-action tmux confirmation.

Lifecycle (table `automation_grants` in the agent_control DB):
  pending  — `request()` from the API. Anyone holding the runtime token can ask; asking
             grants nothing. Validated: no wildcard, no payment agent, bounded TTL,
             actions ⊆ {send, answer, create}, create needs explicit worktrees + cap.
  active   — ONLY through an owner-identity channel, never through the API:
               * `tools/owner_grant.py approve <id>` on the host (root, interactive TTY,
                 the owner retypes the confirmation code shown from the SERVER copy), or
               * Telegram from the configured owner user id replying
                 `/approve <id> <code>` to the server-generated summary
                 (core/owner_telegram.py; disabled until bot token + owner id exist).
             The approval binds to the request's content hash: what was shown is what
             is activated.
  revoked  — `revoke()`, allowed from the API too (narrowing is always safe).
  expired  — implicitly, once `expires_at` passes; pending requests lapse after
             PENDING_TTL_SECS.

Checks used by the enforcement points:
  `allows_send(session, action, caller)` → grant id or None (agent_control._deliver)
  `allows_create(dir, session, caller, live_cwds)` → {ok, reason, grant} (creation gate)

Not a defence against a malicious ROOT process on this host (it could write this table
directly) — like every local gate here. It defends against remote API/MCP callers,
prompt-injected text and self-declared roles: no text, actor name or API call activates
a grant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Optional

ACTIONS = frozenset({"send", "answer", "create"})
HARD_DENY_SESSIONS = frozenset({"payorch-ha-fresh"})
MAX_TTL_SECS = int(os.getenv("GRANT_MAX_TTL_SECS", str(7 * 86400)))
PENDING_TTL_SECS = 900
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class GrantError(ValueError):
    pass


def _db():
    from core import agent_control as ac
    conn = ac._db()
    conn.execute("""CREATE TABLE IF NOT EXISTS automation_grants (
        id TEXT PRIMARY KEY, status TEXT, spec TEXT, spec_hash TEXT, requested_by TEXT,
        requested_at REAL, expires_at REAL, approved_via TEXT, approved_at REAL,
        revoked_at REAL, revoked_by TEXT, code_hash TEXT)""")
    return conn


def _hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _real(p: str) -> str:
    return os.path.realpath(os.path.abspath(p or ""))


def validate_spec(spec: dict) -> dict:
    """Normalised spec or GrantError. Refuses anything wider than explicit."""
    if not isinstance(spec, dict):
        raise GrantError("spec must be an object")
    project = str(spec.get("project") or "").strip()
    if not project:
        raise GrantError("project is required")
    actions = sorted({str(a) for a in (spec.get("actions") or [])})
    if not actions or not set(actions) <= ACTIONS:
        raise GrantError(f"actions must be a non-empty subset of {sorted(ACTIONS)}")
    targets = sorted({str(t).strip() for t in (spec.get("targets") or [])})
    for t in targets:
        if not _SESSION_RE.match(t):
            raise GrantError(f"target {t!r} must be an exact tmux session name (no wildcard)")
        if t.lower() in HARD_DENY_SESSIONS:
            raise GrantError("the payment agent cannot be granted (owner proof per command)")
    create = None
    if "create" in actions:
        c = spec.get("create") or {}
        wts = c.get("worktrees") or {}
        prefix = str(c.get("session_prefix") or "")
        try:
            cap = int(c.get("max_sessions"))
        except (TypeError, ValueError):
            raise GrantError("create.max_sessions is required") from None
        if not isinstance(wts, dict) or not wts or not prefix or not 1 <= cap <= 10:
            raise GrantError("create needs worktrees, session_prefix and 1 <= max_sessions <= 10")
        from core import agent_creation_policy as acp
        cfg = acp.load_config()
        norm = {}
        for key, d in wts.items():
            key, d = str(key), str(d)
            if not re.match(r"^[a-z0-9-]{1,32}$", key) or not _SESSION_RE.match(prefix + key):
                raise GrantError(f"bad worktree key {key!r}")
            if not d.startswith("/opt/") or os.path.abspath(d) != _real(d):
                raise GrantError(f"worktree {d!r} must be a real (non-symlink) path under /opt")
            if acp.hard_denied(d, prefix + key, cfg):
                raise GrantError(f"worktree {d!r} is hard-denied")
            norm[key] = os.path.abspath(d)
        if len(set(norm.values())) != len(norm):
            raise GrantError("two worktrees resolve to one directory")
        create = {"worktrees": norm, "session_prefix": prefix, "max_sessions": cap}
    elif not targets:
        raise GrantError("targets are required")
    try:
        ttl = int(spec.get("ttl_secs") or 0)
    except (TypeError, ValueError):
        raise GrantError("ttl_secs must be an integer") from None
    if not 60 <= ttl <= MAX_TTL_SECS:
        raise GrantError(f"ttl_secs must be within 60..{MAX_TTL_SECS}")
    return {"project": project, "actions": actions, "targets": targets, "create": create,
            "ttl_secs": ttl}


def request(spec: dict, requested_by: str, now: Optional[float] = None) -> dict:
    """Create a PENDING grant. Returns {id, status, summary}. Never activates anything."""
    now = time.time() if now is None else now
    norm = validate_spec(spec)
    gid = "g" + secrets.token_hex(6)
    code = f"{secrets.randbelow(10**6):06d}"
    conn = _db()
    try:
        conn.execute("INSERT INTO automation_grants (id,status,spec,spec_hash,requested_by,"
                     "requested_at,code_hash) VALUES (?,?,?,?,?,?,?)",
                     (gid, "pending", json.dumps(norm, sort_keys=True), _hash(norm),
                      (requested_by or "")[:120], now,
                      hashlib.sha256(f"{gid}:{code}".encode()).hexdigest()))
        conn.commit()
    finally:
        conn.close()
    _audit("grant_requested", gid, requested_by=requested_by, spec=norm)
    _notify_owner(gid, norm, code)
    return {"id": gid, "status": "pending", "summary": summary(norm)}


def summary(spec: dict) -> str:
    parts = [f"project {spec['project']}", f"actions {','.join(spec['actions'])}"]
    if spec.get("targets"):
        parts.append(f"agents {','.join(spec['targets'])}")
    if spec.get("create"):
        c = spec["create"]
        parts.append(f"create up to {c['max_sessions']} as {c['session_prefix']}<key> in "
                     + ", ".join(f"{k}={v}" for k, v in sorted(c["worktrees"].items())))
    parts.append(f"for {spec['ttl_secs'] // 60} min")
    return "; ".join(parts)


def get(gid: str) -> Optional[dict]:
    conn = _db()
    try:
        r = conn.execute("SELECT id,status,spec,spec_hash,requested_by,requested_at,expires_at,"
                         "approved_via,approved_at,revoked_at,revoked_by,code_hash FROM "
                         "automation_grants WHERE id=?", (gid,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    keys = ("id", "status", "spec", "spec_hash", "requested_by", "requested_at", "expires_at",
            "approved_via", "approved_at", "revoked_at", "revoked_by", "code_hash")
    g = dict(zip(keys, r))
    g["spec"] = json.loads(g["spec"])
    return g


def list_grants(now: Optional[float] = None) -> list:
    now = time.time() if now is None else now
    conn = _db()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM automation_grants ORDER BY "
                                          "requested_at DESC LIMIT 100")]
    finally:
        conn.close()
    out = []
    for gid in ids:
        g = get(gid)
        g.pop("code_hash", None)
        g["effective"] = effective_status(g, now)
        out.append(g)
    return out


def effective_status(g: dict, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    if g["status"] == "active" and g.get("expires_at") and now >= g["expires_at"]:
        return "expired"
    if g["status"] == "pending" and now - (g.get("requested_at") or 0) > PENDING_TTL_SECS:
        return "lapsed"
    return g["status"]


def approve(gid: str, *, via: str, expected_hash: str, code: Optional[str] = None,
            now: Optional[float] = None) -> dict:
    """Activate a pending grant. Called ONLY by the owner channels, with the hash of the
    content the owner was shown. Telegram must also carry the one-time code the server sent
    to the owner chat; the host CLI does its own interactive confirmation instead."""
    if via not in ("owner_cli", "telegram_verified"):
        raise GrantError("approval is only possible through an owner channel")
    now = time.time() if now is None else now
    g = get(gid)
    if g is None:
        raise GrantError("unknown grant")
    if effective_status(g, now) != "pending":
        raise GrantError(f"grant is {effective_status(g, now)}, not pending")
    if via == "telegram_verified":
        want = hashlib.sha256(f"{gid}:{code or ''}".encode()).hexdigest()
        if not hmac.compare_digest(want, g["code_hash"] or ""):
            raise GrantError("wrong confirmation code")
    if not hmac.compare_digest(expected_hash, g["spec_hash"]) or _hash(g["spec"]) != g["spec_hash"]:
        raise GrantError("grant content changed since it was shown")
    conn = _db()
    try:
        n = conn.execute("UPDATE automation_grants SET status='active', approved_via=?, "
                         "approved_at=?, expires_at=?, code_hash=NULL WHERE id=? AND "
                         "status='pending'", (via, now, now + g["spec"]["ttl_secs"], gid)).rowcount
        conn.commit()
    finally:
        conn.close()
    if n != 1:
        raise GrantError("grant changed concurrently")
    _audit("grant_approved", gid, via=via)
    return get(gid)


def revoke(gid: str, by: str, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    conn = _db()
    try:
        n = conn.execute("UPDATE automation_grants SET status='revoked', revoked_at=?, "
                         "revoked_by=? WHERE id=? AND status IN ('pending','active')",
                         (now, (by or "")[:120], gid)).rowcount
        conn.commit()
    finally:
        conn.close()
    if n:
        _audit("grant_revoked", gid, by=by)
    return n == 1


def _active(now: float) -> list:
    conn = _db()
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM automation_grants WHERE "
                                          "status='active' AND expires_at>?", (now,))]
    finally:
        conn.close()
    return [g for g in (get(i) for i in ids) if g and _hash(g["spec"]) == g["spec_hash"]]


def _granted_sessions(spec: dict) -> set:
    s = set(spec.get("targets") or [])
    c = spec.get("create")
    if c:
        s |= {c["session_prefix"] + k for k in c["worktrees"]}
    return s


def allows_send(session: str, action: str, caller: str,
                now: Optional[float] = None) -> Optional[str]:
    """Grant id that lets `caller` (only `api`) do `action` on `session`, else None."""
    if caller != "api":
        return None
    act = {"agent_send": "send", "agent_answer": "answer"}.get(action)
    sess = (session or "").strip()
    if not act or sess.lower() in HARD_DENY_SESSIONS:
        return None
    for g in _active(time.time() if now is None else now):
        if act in g["spec"]["actions"] and sess in _granted_sessions(g["spec"]):
            return g["id"]
    return None


def allows_create(project_dir: str, session: str, caller: str, live_cwds: list,
                  now: Optional[float] = None) -> dict:
    if caller != "api":
        return {"ok": False, "reason": "grant_caller_not_api"}
    real = _real(project_dir)
    for g in _active(time.time() if now is None else now):
        c = g["spec"].get("create")
        if not c or "create" not in g["spec"]["actions"]:
            continue
        for key, wt in c["worktrees"].items():
            if _real(wt) != real:
                continue
            if os.path.abspath(wt) != _real(wt):
                return {"ok": False, "reason": "grant_worktree_is_symlink", "grant": g["id"]}
            if session != c["session_prefix"] + key:
                return {"ok": False, "reason": "grant_session_name_mismatch", "grant": g["id"]}
            if any(_real(x) == real for x in live_cwds):
                return {"ok": False, "reason": "worktree_already_has_live_agent",
                        "grant": g["id"]}
            pop = sum(1 for x in live_cwds
                      if any(_real(x) == _real(w) for w in c["worktrees"].values()))
            if pop >= c["max_sessions"]:
                return {"ok": False, "reason": "grant_at_max_sessions", "grant": g["id"]}
            return {"ok": True, "reason": "grant", "grant": g["id"]}
    return {"ok": False, "reason": "no_active_grant"}


def _audit(event: str, gid: str, **kw) -> None:
    try:
        from core import agent_control as ac
        ac.audit(event, gid, None, **kw)
    except Exception:  # noqa: BLE001 — auditing must never break a grant decision
        pass


def _notify_owner(gid: str, spec: dict, code: str) -> None:
    try:
        from core import owner_telegram as ot
        ot.send_approval_request(gid, summary(spec), _hash(spec), code)
    except Exception:  # noqa: BLE001 — no owner channel configured is a normal state
        pass
