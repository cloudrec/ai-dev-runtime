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
import re
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


def _payment_dirs() -> list:
    try:
        from core import agent_creation_policy as acp
        dirs = acp.load_config().get("hard_deny_dirs") or []
        return [d for d in dirs if isinstance(d, str) and d]
    except Exception:  # noqa: BLE001
        return []


def check_project_root(root: str) -> Optional[str]:
    if not root or not os.path.isabs(root):
        return "project_root_not_absolute"
    if not os.path.isdir(root):
        return "project_root_missing"
    real = _real(root)
    home = _real(os.path.expanduser("~"))
    if real in ("/", home, "/root", "/home", "/tmp", "/var", "/srv", "/opt"):
        return f"project_root_too_broad:{real}"
    for d in deny_roots():
        # a worktree BESIDE the live tree (/root/ai-dev-runtime-x) is not under it;
        # a root that CONTAINS a denied tree would hand it to the agent
        if _under(real, d):
            return f"project_root_denied:{d}"
        if os.path.exists(d) and _under(d, real):
            return f"project_root_contains_denied:{d}"
    paid = _payment_denied(real)
    if paid:
        return paid
    if any(os.path.exists(d) and _under(d, real) for d in _payment_dirs()):
        return "project_root_contains_payment_dir"
    return None


# Check commands from anyone other than the spec author (a GPT-added task) may only run
# a known test runner on files inside the project: a check is a command the ENGINE runs.
CHECK_RUNNERS = ("python", "python3", "pytest", "node", "npm", "go", "cargo", "make")
_RUNNER_ALLOWED_SUBCMDS = {"npm": {"test"}, "go": {"test", "vet"}, "cargo": {"test"},
                           "make": {"test", "check"}}


_WORD_BREAK = re.compile(r"[\s;&|()<>'\"`=,:]+")


def _names_api_launcher(argv: list) -> bool:
    """Whole words only. `tests/api` is a project path, not a launcher called `api`: the
    launcher lives outside the project, so a plain relative path reaches it only by spelling
    out its own path. This is a lint on specs: a glob or a spliced variable gets past it,
    and indirect invocation always will."""
    from core import agent_auth_mode as aam
    paths = {aam.DEFAULT_LAUNCHER}
    cfg = aam.load_config()
    if cfg.get("launcher"):
        paths.add(str(cfg["launcher"]))
    names = {os.path.basename(p) for p in paths} - {""}
    words = (w for a in argv for w in _WORD_BREAK.split(str(a).replace("\\", "")) if w)
    return any(w.rsplit("/", 1)[-1] in names and (
        "/" not in w or w[0] in "/.~$" or any(p.endswith("/" + w) for p in paths))
        for w in words)


def check_argv(argv: list, *, root: str, trusted: bool) -> Optional[str]:
    """None = may run. Trusted (spec-authored) checks only need to be argv lists;
    untrusted ones are confined to a test runner over project files."""
    if not argv or not all(isinstance(a, str) for a in argv):
        return "check_not_argv"
    # The API launcher never runs as a check, trusted or not: that path has no owner proof,
    # cap or ledger (N2). Any argument counts, so `sh -c "claude-owner-api …"` is caught.
    if _names_api_launcher(argv):
        return "check_api_launcher_forbidden"
    if trusted:
        return None
    import sys
    exe = os.path.basename(argv[0])
    if argv[0] == sys.executable:
        exe = "python"
    if exe not in CHECK_RUNNERS:
        return f"check_runner_not_allowed:{exe}"
    if exe in _RUNNER_ALLOWED_SUBCMDS and (len(argv) < 2 or argv[1] not in
                                           _RUNNER_ALLOWED_SUBCMDS[exe]):
        return f"check_subcommand_not_allowed:{exe}"
    args = argv[1:]
    if exe.startswith("python"):
        if "-c" in args:
            return "check_inline_code_not_allowed"
        if "-m" in args:
            mod = (args[args.index("-m") + 1] if args.index("-m") + 1 < len(args) else "")
            top = mod.split(".")[0]
            if top not in ("pytest", "unittest") and not (
                    os.path.isdir(os.path.join(root, top)) or
                    os.path.isfile(os.path.join(root, top + ".py"))):
                return f"check_module_outside_project:{mod}"
    if exe == "node" and "-e" in args:
        return "check_inline_code_not_allowed"
    for a in args:
        if a.startswith("/") and not _under(a, root):
            return f"check_path_outside_project:{a}"
        if ".." in a.split("/"):
            return f"check_path_escape:{a}"
    return None


def check_dispatch(*, root: str, executor: str, contract: dict,
                   checks: Optional[list] = None, trusted_checks: bool = True) -> Optional[str]:
    """None = allowed; otherwise the refusal reason. Checked before EVERY dispatch."""
    r = check_project_root(root)
    if r:
        return r
    for argv in checks or []:
        r = check_argv(argv, root=root, trusted=trusted_checks)
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
        if any(n == p.rstrip("/") or n.startswith(p.rstrip("/") + "/")
               for p in ignore_prefixes):
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
