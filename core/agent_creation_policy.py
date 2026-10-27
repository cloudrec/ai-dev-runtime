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
"""
from __future__ import annotations

import os
from typing import Callable, Optional

HARD_DENY_SESSIONS = frozenset({"payorch-ha-fresh"})
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
    """(scope_name, scope, worktree_key) whose worktree is exactly this dir, else None."""
    real = _real(project_dir)
    scopes = cfg.get("scopes")
    if not isinstance(scopes, dict):
        return None
    for name, sc in scopes.items():
        if not isinstance(sc, dict):
            continue
        wts = sc.get("worktrees")
        if not isinstance(wts, dict):
            continue
        for key, wt in wts.items():
            if isinstance(wt, str) and wt and _real(wt) == real:
                return name, sc, str(key)
    return None


def scope_population(sc: dict, live_cwds: list) -> int:
    wts = [w for w in (sc.get("worktrees") or {}).values() if isinstance(w, str) and w] \
        if isinstance(sc.get("worktrees"), dict) else []
    return sum(1 for c in live_cwds if any(_under(c, w) for w in wts))


def authorize(project_dir: str, session: str, *, caller: str,
              owner_proof: Optional[dict] = None, live_cwds: Optional[list] = None,
              verify_proof: Optional[Callable] = None, cfg: Optional[dict] = None) -> dict:
    """{"ok": bool, "reason": str, "scope": name|None}. Pure apart from proof consumption."""
    cfg = load_config() if cfg is None else cfg
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
        return {"ok": False, "reason": "creation_not_in_any_scope", "scope": None}
    name, sc, key = hit
    callers = [str(c) for c in (sc.get("callers") or [])]
    if caller not in callers:
        return {"ok": False, "reason": "creation_caller_not_allowed", "scope": name}
    want = f"{sc.get('session_prefix') or ''}{key}"
    if (session or "") != want:
        return {"ok": False, "reason": "creation_session_name_mismatch", "scope": name,
                "expected_session": want}
    live = list(live_cwds or [])
    if any(_real(c) == _real(project_dir) for c in live):
        return {"ok": False, "reason": "worktree_already_has_live_agent", "scope": name}
    try:
        cap = int(sc.get("max_concurrent"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "creation_scope_missing_max_concurrent", "scope": name}
    if scope_population(sc, live) >= cap:
        return {"ok": False, "reason": "creation_scope_at_max_concurrent", "scope": name}
    return {"ok": True, "reason": "scope", "scope": name}
