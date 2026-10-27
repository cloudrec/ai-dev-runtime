"""Fail-closed authorization for creating / resuming agent sessions (2026-10-27).

Every path that starts a Claude process in a pane — `agent_control.agent_resume` (API
`/agents/resume`, `agent_fabric.start_or_resume`) and `session_recovery.recover`
(`commander_autopilot`) — asks `authorize()` first, before any tmux mutation.

Allowed only when ONE of:
  * a valid owner proof for exactly this creation (`agent_control.verify_owner_proof`,
    action "agent_resume", target = the resolved project dir, text = the session name) —
    explicit, verifiable human action; or
  * `AGENT_CREATION_POLICY=off` (owner-gated escape hatch; hard deny still applies); or
  * an explicit SCOPE in `config/agent_creation.yaml` (path: AGENT_CREATION_CONFIG) where
    - the caller class is listed (`api`, an in-process actor name, or `unattributed`) —
      HMAC API auth is a caller class, never owner authority;
    - the directory is EXACTLY one of the scope's worktrees and the session name is
      `<session_prefix><key>` for THAT worktree's key (e.g. country) — no cross-project,
      no cross-country mutation;
    - fewer than `max_concurrent` live agents already run in the scope's worktrees.
No config / no matching scope → refused. One live session per worktree is enforced by the
caller (`find_live_agent_for_dir`) and re-checked here. The payment agent is hard-denied
(session name or any directory listed in `hard_deny_dirs`).

Races: callers hold `creation_guard()` (an exclusive flock shared by every process) from the
check until the tmux session exists, and `reserve()` the worktree; a reservation counts as a
live agent for RESERVATION_TTL_SECS, because a new Claude process is not visible in the pane
inventory for a moment after `new-session`. A configured worktree path that is (or passes
through) a symlink, or two worktrees resolving to one directory, is refused: retargeting a
link must never move a scope. Optional top-level `max_live_agents` caps all live Claude
processes on the host.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Callable, Optional

HARD_DENY_SESSIONS = frozenset({"payorch-ha-fresh"})
RESERVATION_TTL_SECS = 180
_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "agent_creation.yaml")


def config_path() -> str:
    return os.getenv("AGENT_CREATION_CONFIG", _DEFAULT_CONFIG)


def load_config(path: Optional[str] = None) -> dict:
    """{} on any problem (missing, unreadable, malformed) — which authorizes nothing."""
    try:
        import yaml
        with open(path or config_path(), encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _state_base() -> str:
    return os.getenv("AGENT_CREATION_STATE",
                     os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")
                     + ".creation")


@contextlib.contextmanager
def creation_guard():
    """Exclusive, cross-process: check-then-create runs one at a time on this host."""
    import fcntl
    fd = os.open(_state_base() + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _load_reservations(now: float) -> dict:
    try:
        with open(_state_base() + ".json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)
            and now - float(v.get("ts") or 0) < RESERVATION_TTL_SECS}


def reserved_cwds(now: Optional[float] = None) -> list:
    return list(_load_reservations(time.time() if now is None else now))


def reserve(project_dir: str, session: str, now: Optional[float] = None) -> None:
    """Call inside creation_guard() right after the session was created."""
    now = time.time() if now is None else now
    data = _load_reservations(now)
    data[_real(project_dir)] = {"session": session, "ts": now}
    tmp = _state_base() + ".json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _state_base() + ".json")


def release(project_dir: str) -> None:
    """Drop a reservation (the created process failed verification and is gone)."""
    with creation_guard():
        data = _load_reservations(time.time())
        if data.pop(_real(project_dir), None) is not None:
            tmp = _state_base() + ".json.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, _state_base() + ".json")


def _real(p: str) -> str:
    return os.path.realpath(os.path.abspath(p or ""))


def _under(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def hard_denied(project_dir: str, session: str, cfg: dict) -> bool:
    if (session or "").strip().lower() in HARD_DENY_SESSIONS:
        return True
    dirs = cfg.get("hard_deny_dirs")
    return isinstance(dirs, list) and any(_under(project_dir, d) for d in dirs
                                          if isinstance(d, str) and d)


def _scope_for(project_dir: str, cfg: dict):
    """(scope_name, scope, worktree_key) whose worktree is exactly this dir; None when no
    scope matches; a str reason when the match is unsafe (symlinked or ambiguous)."""
    real = _real(project_dir)
    scopes = cfg.get("scopes")
    if not isinstance(scopes, dict):
        return None
    hits = []
    for name, sc in scopes.items():
        if not isinstance(sc, dict):
            continue
        wts = sc.get("worktrees")
        if not isinstance(wts, dict):
            continue
        for key, wt in wts.items():
            if isinstance(wt, str) and wt and _real(wt) == real:
                hits.append((name, sc, str(key), wt))
    if not hits:
        return None
    if len(hits) > 1:
        return "creation_scope_ambiguous"
    name, sc, key, wt = hits[0]
    if os.path.abspath(wt) != _real(wt):
        return "creation_scope_worktree_is_symlink"
    return name, sc, key


def scope_population(sc: dict, live_cwds: list) -> int:
    wts = [w for w in (sc.get("worktrees") or {}).values() if isinstance(w, str) and w] \
        if isinstance(sc.get("worktrees"), dict) else []
    return sum(1 for c in live_cwds if any(_under(c, w) for w in wts))


def authorize(project_dir: str, session: str, *, caller: str,
              owner_proof: Optional[dict] = None, live_cwds: Optional[list] = None,
              verify_proof: Optional[Callable] = None, cfg: Optional[dict] = None) -> dict:
    """{"ok": bool, "reason": str, "scope": name|None}. Pure apart from proof consumption."""
    cfg = load_config() if cfg is None else cfg
    # one session per worktree holds on EVERY allow path, owner proof and policy off too:
    # a session created moments ago is not yet visible as a live Claude process
    if _real(project_dir) in {_real(c) for c in reserved_cwds()}:
        return {"ok": False, "reason": "worktree_reserved_by_recent_creation", "scope": None}
    if owner_proof is not None:
        if verify_proof is None:
            from core import agent_control as ac
            verify_proof = ac.verify_owner_proof
        why = verify_proof(owner_proof, "agent_resume", _real(project_dir), session or "")
        return {"ok": not why, "reason": why or "owner_proof", "scope": None}
    if hard_denied(project_dir, session, cfg):
        return {"ok": False, "reason": "hard_denied_target", "scope": None}
    if os.getenv("AGENT_CREATION_POLICY", "enforce").strip().lower() == "off":
        return {"ok": True, "reason": "policy_off", "scope": None}
    hit = _scope_for(project_dir, cfg)
    if hit is None:
        # no static scope: an ACTIVE owner delegation grant may cover this exact worktree
        from core import automation_grants as ag
        g = ag.allows_create(project_dir, session, caller,
                             list(live_cwds or []) + reserved_cwds())
        if g["ok"]:
            return {"ok": True, "reason": f"grant:{g['grant']}", "scope": None}
        if g["reason"] not in ("no_active_grant", "grant_caller_not_api"):
            return {"ok": False, "reason": g["reason"], "scope": None}
        return {"ok": False, "reason": "creation_not_in_any_scope", "scope": None}
    if isinstance(hit, str):
        return {"ok": False, "reason": hit, "scope": None}
    name, sc, key = hit
    callers = [str(c) for c in (sc.get("callers") or [])]
    if caller not in callers:
        return {"ok": False, "reason": "creation_caller_not_allowed", "scope": name}
    want = f"{sc.get('session_prefix') or ''}{key}"
    if (session or "") != want:
        return {"ok": False, "reason": "creation_session_name_mismatch", "scope": name,
                "expected_session": want}
    live = list(live_cwds or []) + reserved_cwds()
    if any(_real(c) == _real(project_dir) for c in live):
        return {"ok": False, "reason": "worktree_already_has_live_agent", "scope": name}
    try:
        cap = int(sc.get("max_concurrent"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "creation_scope_missing_max_concurrent", "scope": name}
    if scope_population(sc, live) >= cap:
        return {"ok": False, "reason": "creation_scope_at_max_concurrent", "scope": name}
    host_cap = cfg.get("max_live_agents")
    if host_cap is not None:
        try:
            if len({_real(c) for c in live}) >= int(host_cap):
                return {"ok": False, "reason": "creation_host_at_max_live_agents",
                        "scope": name}
        except (TypeError, ValueError):
            return {"ok": False, "reason": "creation_bad_max_live_agents", "scope": name}
    return {"ok": True, "reason": "scope", "scope": name}
