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

Binding: a grant belongs to the EXACT actor that requested it (`requested_by`) and only
that actor can use it. `api:client/<id>` actors come from a per-client runtime token and are
AUTHENTICATED (binding `authenticated`); `api:bearer/<name>` / `api:hmac/<name>` carry a
name declared by a shared-token holder (binding `declared`) — e.g. the MCP server naming
the verified MCP token it serves — which separates MCP connectors but not other holders of
the shared runtime token. Requests from an unnamed caller are refused; once a client-token
file is configured (or GRANT_REQUIRE_AUTHENTICATED_CLIENT=1) only authenticated clients
may request.

Checks used by the enforcement points:
  `allows_send(session, action, actor)` → grant id or None (agent_control._deliver)
  `allows_create(dir, session, actor, live_cwds)` → {ok, reason, grant} (creation gate)

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
MAX_FAILED_APPROVALS = 5               # per 15 min, then approvals lock (brute force)
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
HARD_DENY_SESSIONS = frozenset({"payorch-ha-fresh"})
MAX_TTL_SECS = int(os.getenv("GRANT_MAX_TTL_SECS", str(7 * 86400)))
PENDING_TTL_SECS = 900
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class GrantError(ValueError):
    pass


def _scrypt(secret: str, salt: str) -> str:
    return hashlib.scrypt(secret.encode(), salt=salt.encode(), **_SCRYPT).hex()


def passphrase_file() -> str:
    return os.getenv("OWNER_GRANT_PASSPHRASE_FILE", "/etc/ai-runtime/owner-grant-passphrase.json")


def _load_passphrase() -> Optional[dict]:
    """{salt, hash} from a regular root-owned 0600/0400 file, else None (fail closed)."""
    import stat as _stat
    path = passphrase_file()
    try:
        st = os.stat(path, follow_symlinks=False)
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != 0 or (st.st_mode & 0o077):
            return None
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    ok = isinstance(d, dict) and re.fullmatch(r"[0-9a-f]{32}", str(d.get("salt", ""))) \
        and re.fullmatch(r"[0-9a-f]{64}", str(d.get("hash", "")))
    return d if ok else None


def set_passphrase(passphrase: str) -> None:
    """Owner-only (tools/owner_grant.py set-passphrase). Never overwrites."""
    if len(passphrase or "") < 12:
        raise GrantError("passphrase must be at least 12 characters")
    path = passphrase_file()
    if os.path.lexists(path):
        raise GrantError(f"{path} exists; not overwritten")
    salt = secrets.token_hex(16)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"salt": salt, "hash": _scrypt(passphrase, salt)}, fh)


def _db():
    from core import agent_control as ac
    conn = ac._db()
    conn.execute("CREATE TABLE IF NOT EXISTS automation_grant_attempts (ts REAL, via TEXT, "
                 "grant_id TEXT, ok INTEGER)")
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


def binding_of(actor: str) -> str:
    a = actor or ""
    if re.fullmatch(r"api:client/[A-Za-z0-9._-]{1,48}", a):
        return "authenticated"
    if re.fullmatch(r"api:[a-z]+/[A-Za-z0-9 ._:@/+-]{1,64}", a):
        return "declared"
    return "unidentified"


def require_authenticated() -> bool:
    flag = os.getenv("GRANT_REQUIRE_AUTHENTICATED_CLIENT", "").strip()
    if flag:
        return flag == "1"
    try:
        from api import v1 as _v1            # client tokens configured => fail closed
        return bool(_v1._client_tokens())
    except Exception:  # noqa: BLE001
        return False


def request(spec: dict, requested_by: str, now: Optional[float] = None) -> dict:
    """Create a PENDING grant. Returns {id, status, summary}. Never activates anything."""
    now = time.time() if now is None else now
    binding = binding_of(requested_by)
    if binding == "unidentified":
        raise GrantError("a grant needs an identified client (X-Runtime-Actor or a client token)")
    if require_authenticated() and binding != "authenticated":
        raise GrantError("this runtime requires an authenticated client token for grants")
    norm = validate_spec(spec)
    norm["bound_to"] = requested_by
    norm["binding"] = binding
    gid = "g" + secrets.token_hex(6)
    # ~72 bits, stored as scrypt: not brute-forceable from the stored hash
    code = secrets.token_urlsafe(9)
    conn = _db()
    try:
        conn.execute("INSERT INTO automation_grants (id,status,spec,spec_hash,requested_by,"
                     "requested_at,code_hash) VALUES (?,?,?,?,?,?,?)",
                     (gid, "pending", json.dumps(norm, sort_keys=True), _hash(norm),
                      (requested_by or "")[:120], now,
                      _scrypt(code, gid)))
        conn.commit()
    finally:
        conn.close()
    _audit("grant_requested", gid, requested_by=requested_by, spec=norm)
    _notify_owner(gid, norm, code)
    return {"id": gid, "status": "pending", "summary": summary(norm)}


def summary(spec: dict) -> str:
    parts = [f"project {spec['project']}", f"actions {','.join(spec['actions'])}"]
    if spec.get("bound_to"):
        parts.append(f"only for {spec['bound_to']} ({spec.get('binding')})")
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


def _attempts_locked(conn, now: float) -> bool:
    n = conn.execute("SELECT COUNT(*) FROM automation_grant_attempts WHERE ok=0 AND ts>?",
                     (now - 900,)).fetchone()[0]
    return n >= MAX_FAILED_APPROVALS


def _record_attempt(via: str, gid: str, ok: bool, now: float) -> None:
    conn = _db()
    try:
        conn.execute("INSERT INTO automation_grant_attempts VALUES (?,?,?,?)",
                     (now, via, gid, 1 if ok else 0))
        conn.commit()
    finally:
        conn.close()


def approve(gid: str, *, via: str, expected_hash: str, code: Optional[str] = None,
            passphrase: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Activate a pending grant. Requires an OWNER SECRET verified here — the `via` string
    alone authorizes nothing, so in-process callers (agents, tools) cannot self-approve:
      owner_cli          → the owner passphrase (scrypt, root-only file);
      telegram_verified  → the one-time code the server sent to the owner's Telegram chat.
    Plus the hash of the content the owner was shown. 5 failures / 15 min lock approvals."""
    if via not in ("owner_cli", "telegram_verified"):
        raise GrantError("approval is only possible through an owner channel")
    now = time.time() if now is None else now
    conn = _db()
    try:
        locked = _attempts_locked(conn, now)
    finally:
        conn.close()
    if locked:
        raise GrantError("approvals locked after repeated failures; retry later")
    if via == "owner_cli":
        rec = _load_passphrase()
        if rec is None:
            raise GrantError("no owner passphrase configured (tools/owner_grant.py set-passphrase)")
        if not passphrase or not hmac.compare_digest(_scrypt(passphrase, rec["salt"]),
                                                     rec["hash"]):
            _record_attempt(via, gid, False, now)
            raise GrantError("wrong owner passphrase")
    g = get(gid)
    if g is None:
        raise GrantError("unknown grant")
    if effective_status(g, now) != "pending":
        raise GrantError(f"grant is {effective_status(g, now)}, not pending")
    if via == "telegram_verified":
        if not code or not hmac.compare_digest(_scrypt(code, gid), g["code_hash"] or ""):
            _record_attempt(via, gid, False, now)
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
    _record_attempt(via, gid, True, now)
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


def allows_send(session: str, action: str, actor: str, now: Optional[float] = None,
                cwd: Optional[str] = None) -> Optional[str]:
    """Grant id that lets exactly `actor` (an api caller) do `action` on `session`. A session
    granted through `create` must be running IN its granted worktree (`cwd`), so a renamed
    or reused session name elsewhere is not covered."""
    if not (actor or "").startswith("api:"):
        return None
    act = {"agent_send": "send", "agent_answer": "answer"}.get(action)
    sess = (session or "").strip()
    if not act or sess.lower() in HARD_DENY_SESSIONS:
        return None
    for g in _active(time.time() if now is None else now):
        spec = g["spec"]
        if spec.get("bound_to") != actor or act not in spec["actions"]:
            continue
        if sess in set(spec.get("targets") or []):
            return g["id"]
        c = spec.get("create")
        if c:
            for key, wt in c["worktrees"].items():
                if sess == c["session_prefix"] + key and cwd and _real(cwd) == _real(wt):
                    return g["id"]
    return None


def allows_create(project_dir: str, session: str, actor: str, live_cwds: list,
                  now: Optional[float] = None) -> dict:
    if not (actor or "").startswith("api:"):
        return {"ok": False, "reason": "grant_caller_not_api"}
    real = _real(project_dir)
    for g in _active(time.time() if now is None else now):
        c = g["spec"].get("create")
        if not c or "create" not in g["spec"]["actions"] or g["spec"].get("bound_to") != actor:
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
