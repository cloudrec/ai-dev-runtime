"""Permission gates. Deny by default; every refusal names its reason.

Before dispatch (`check_dispatch`):
  * the project root is a real directory, not under a denied root (the live Owner OS
    tree, system and credential paths, FULLCYCLE_DENY_ROOTS) and not a payment
    directory per core.agent_creation_policy.hard_denied (fail closed on an
    unreadable policy);
  * the executor profile is tested (core.fullcycle.executor.profile);
  * the task's contract carries no push/deploy power — those stay owner-only.

On returned work (`check_changes`): every changed path, as seen by git in the workdir
(not as claimed by the agent), lies inside the task scope and outside do_not_touch.

On a permission prompt mid-task (`check_permission_prompt`): the command is classified
by core.permission_resolver for the record, and the answer is always "owner": this
slice never answers an agent's permission dialog on anyone's behalf.
"""
from __future__ import annotations

import os
from typing import Optional

from core.fullcycle.executor import ExecutorUnavailable, profile

DEFAULT_DENY_ROOTS = ("/root/ai-dev-runtime", "/etc", "/root/.ssh", "/root/.config",
                      "/var/lib", "/boot", "/usr")


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def _under(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def deny_roots() -> list:
    extra = [p for p in os.getenv("FULLCYCLE_DENY_ROOTS", "").split(":") if p]
    return list(DEFAULT_DENY_ROOTS) + extra


def _payment_denied(root: str) -> Optional[str]:
    try:
        from core import agent_creation_policy as acp
        return "payment_hard_denied" if acp.hard_denied(root, "", acp.load_config()) else None
    except Exception:  # noqa: BLE001 — unreadable policy: fail closed
        return "payment_policy_unreadable"


def check_project_root(root: str) -> Optional[str]:
    if not root or not os.path.isabs(root):
        return "project_root_not_absolute"
    if not os.path.isdir(root):
        return "project_root_missing"
    real = _real(root)
    for d in deny_roots():
        # a worktree BESIDE the live tree (/root/ai-dev-runtime-x) is not under it
        if _under(real, d):
            return f"project_root_denied:{d}"
    return _payment_denied(real)


def check_dispatch(*, root: str, executor: str, contract: dict) -> Optional[str]:
    """None = allowed; otherwise the refusal reason."""
    r = check_project_root(root)
    if r:
        return r
    try:
        profile(executor)
    except ExecutorUnavailable as e:
        return f"executor_unavailable:{e}"
    if contract.get("push_allowed"):
        return "push_requires_owner"
    if contract.get("deploy_allowed"):
        return "deploy_requires_owner"
    return None


def _norm(rel: str) -> str:
    n = os.path.normpath(rel)
    return "" if n == "." else n


def check_changes(changed: list, *, scope: list, do_not_touch: list,
                  ignore_prefixes: tuple = (".fullcycle/",)) -> list:
    """Violations (empty = clean). Paths are relative to the workdir. An empty scope
    means the whole workdir; `..` escapes are always a violation."""
    out = []
    for f in changed:
        n = os.path.normpath(f)
        if n.startswith("..") or os.path.isabs(n):
            out.append(f"outside_workdir:{f}")
            continue
        if any(n.startswith(p.rstrip("/")) for p in ignore_prefixes):
            continue
        if any(n == _norm(d) or n.startswith(_norm(d).rstrip("/") + "/") for d in do_not_touch):
            out.append(f"do_not_touch:{f}")
        elif scope and not any(n == _norm(s) or n.startswith(_norm(s).rstrip("/") + "/")
                               for s in scope):
            out.append(f"outside_scope:{f}")
    return out


def check_permission_prompt(command: str, *, root: str) -> dict:
    from core import permission_resolver as pr
    c = pr.classify_command(command or "", cwd=root, project_roots=[root]) if command else \
        {"safe": False, "category": "unparsed", "reason": "command not isolated"}
    return {"decision": "owner", "command": command, "classified_safe": bool(c.get("safe")),
            "category": c.get("category"), "reason": c.get("reason")}
