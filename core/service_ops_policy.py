"""Narrow service-operations auto-approval for managed approved tasks.

A systemctl / docker command is UNSAFE by default. This grants a NARROW, stateful
exception so the owner is not asked for routine safe internal service work:

  * `systemctl restart <service>` — RESTART ONLY (never stop/disable/mask/unmask/
    kill/reload/start/enable/isolate), and only a service on the task/project
    allowlist (`ai-runtime.service` + the project's recorded services);
  * `docker compose build|up|create|restart <service…>` — only EXACT task-scoped
    services in the current project's EXISTING compose file, with NO `down`, `rm`,
    `stop`, `kill`, `prune`, volume removal (`-v`/`--volumes`), `--remove-orphans`,
    `--rmi`, or image deletion.

Deny-by-default: any other verb/flag/service/project, a glob, `sudo`, arbitrary
docker/systemctl, a destructive data migration, or an external send/payment/account/
credential stops with the exact reason. Before a mutation it captures ROLLBACK
evidence (current active state / image digest); the caller health-checks after and
rolls back or safely surfaces on failure. Reads state READ-ONLY, never prints a
secret, `GIT`/prompt-free.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

from core import permission_resolver as pr

_FORBIDDEN_SYSTEMCTL = {"stop", "disable", "mask", "unmask", "kill", "reload",
                        "daemon-reload", "start", "enable", "isolate", "poweroff",
                        "reboot", "halt", "set-property", "edit", "revert"}
_FORBIDDEN_COMPOSE = {"down", "rm", "stop", "kill", "prune", "pause", "unpause",
                      "push", "pull", "cp", "exec", "run", "logs", "config"}
_ALLOWED_COMPOSE = {"build", "up", "create", "restart"}
_FORBIDDEN_COMPOSE_FLAGS = {"-v", "--volumes", "--remove-orphans", "--rmi",
                            "-V", "--renew-anon-volumes", "--remove-orphans=true"}
_GLOB_RE = re.compile(r"[*?\[\]]")


class ServiceOpError(Exception):
    pass


def is_service_op(command: str) -> bool:
    try:
        return parse_service_op(command) is not None
    except ServiceOpError:
        return True                    # a service-op shape we recognise but reject


def _segments(command: str) -> list[str]:
    masked = pr._mask_quotes(command)
    scan = pr._SAFE_REDIRECT_RE.sub(lambda m: " " * len(m.group(0)), masked)
    return pr._split_segments(command, scan)


def parse_service_op(command: str) -> Optional[dict]:
    """Return {kind, service|services, sub, compose_file} for a recognised service
    op, None if not one, or raise ServiceOpError for a rejected shape. Every OTHER
    segment must be a harmless `cd`/builtin — a service op hidden among writes is
    not routine."""
    op = None
    for seg in _segments(command):
        try:
            toks = pr._tokens(seg)
        except pr._Unsafe:
            raise ServiceOpError("unparseable command")
        if not toks:
            continue
        prog = toks[0].rsplit("/", 1)[-1]
        if prog in ("cd", "pushd", "popd", "true", ":"):
            continue
        if prog == "sudo":
            raise ServiceOpError("sudo not allowed")
        if prog in ("systemctl", "service"):
            if op is not None:
                raise ServiceOpError("more than one service op")
            op = _parse_systemctl(toks)
        elif prog in ("docker", "docker-compose"):
            if op is not None:
                raise ServiceOpError("more than one service op")
            op = _parse_compose(prog, toks)
        else:
            # A non-service command. Alone → this is not a service op at all
            # (return None). Mixed WITH a service op → reject the whole thing.
            if op is None:
                return None
            raise ServiceOpError(f"non-service command present: {prog}")
    return op


def _parse_systemctl(toks: list[str]) -> dict:
    args = [a for a in toks[1:]]
    verb = next((a for a in args if not a.startswith("-")), None)
    if verb in _FORBIDDEN_SYSTEMCTL:
        raise ServiceOpError(f"systemctl {verb} not allowed (restart only)")
    if verb != "restart":
        raise ServiceOpError(f"systemctl {verb or '?'} not allowed (restart only)")
    names = [a for a in args[args.index("restart") + 1:] if not a.startswith("-")]
    if len(names) != 1:
        raise ServiceOpError("systemctl restart needs exactly one service")
    if _GLOB_RE.search(names[0]):
        raise ServiceOpError("glob service name not allowed")
    return {"kind": "systemctl_restart", "service": names[0]}


def _parse_compose(prog: str, toks: list[str]) -> dict:
    args = toks[1:]
    if prog == "docker":
        if not args or args[0] != "compose":
            raise ServiceOpError("only `docker compose` is a service op, not raw docker")
        args = args[1:]
    compose_file = None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-f", "--file"):
            compose_file = args[i + 1] if i + 1 < len(args) else None
            i += 2
        else:
            i += 1
    sub = args[i] if i < len(args) else None
    if sub in _FORBIDDEN_COMPOSE:
        raise ServiceOpError(f"docker compose {sub} not allowed")
    if sub not in _ALLOWED_COMPOSE:
        raise ServiceOpError(f"docker compose {sub or '?'} not allowed (build/up/create/restart only)")
    rest = args[i + 1:]
    for a in rest:
        if a in _FORBIDDEN_COMPOSE_FLAGS or a.split("=")[0] in _FORBIDDEN_COMPOSE_FLAGS:
            raise ServiceOpError(f"forbidden compose flag: {a}")
    services = [a for a in rest if not a.startswith("-")]
    for s in services:
        if _GLOB_RE.search(s):
            raise ServiceOpError("glob service name not allowed")
    return {"kind": f"compose_{sub}", "sub": sub, "services": services,
            "compose_file": compose_file}


def _run(cwd: Optional[str], *args: str, timeout: int = 12) -> tuple[int, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(list(args), cwd=cwd or None, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, env=env)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)[:200]


def evaluate_service_op(command: str, cwd: str, project: dict) -> dict:
    """Decide whether a service op may be auto-approved. Deny-by-default. `project`
    must carry `service_ops: true`, `services` (systemctl allowlist), and for
    compose `compose_file` + `task_scoped_services`. Returns {allowed, reason,
    checks, rollback}."""
    checks: dict = {}

    def deny(reason: str) -> dict:
        return {"allowed": False, "reason": reason, "checks": checks}

    if not project or not project.get("service_ops"):
        return deny("project has not opted in to service-ops auto-approval")
    try:
        op = parse_service_op(command)
    except ServiceOpError as e:
        return deny(str(e))
    if op is None:
        return deny("not a recognised service op")
    checks["op"] = op

    if op["kind"] == "systemctl_restart":
        svc = op["service"]
        allow = set(project.get("services") or []) | {"ai-runtime.service"}
        # accept with or without the `.service` suffix
        if svc not in allow and (svc + ".service") not in allow and svc.rstrip(".service") not in \
                {s.rstrip(".service") for s in allow}:
            return deny(f"service {svc!r} is not on the project allowlist")
        rc, active = _run(None, "systemctl", "is-active", svc)
        checks["pre_active"] = active
        return {"allowed": True, "reason": f"routine restart of allowlisted service {svc}",
                "kind": "systemctl_restart", "service": svc, "checks": checks,
                "rollback": {"unit": svc, "pre_active": active}}

    # docker compose build/up/create/restart
    task_svcs = set(project.get("task_scoped_services") or [])
    if not task_svcs:
        return deny("project records no task_scoped_services")
    svcs = op["services"]
    if not svcs:
        return deny("compose op must name explicit task-scoped services")
    extra = [s for s in svcs if s not in task_svcs]
    if extra:
        return deny(f"services not task-scoped: {extra}")
    cfile = op.get("compose_file") or project.get("compose_file")
    if not cfile or not os.path.exists(cfile):
        return deny(f"compose file not found: {cfile!r}")
    checks["compose_file"] = cfile
    # rollback evidence: current image digest of each task container (read-only).
    rollback = {}
    for s in svcs:
        cname = (project.get("container_names") or {}).get(s, s)
        rc, img = _run(os.path.dirname(cfile), "docker", "inspect",
                       "--format", "{{.Image}}", cname)
        rollback[s] = {"container": cname, "image": img if rc == 0 else None}
    checks["rollback_captured"] = True
    return {"allowed": True, "reason": f"task-scoped compose {op['sub']} of {svcs}",
            "kind": op["kind"], "services": svcs, "compose_file": cfile,
            "checks": checks, "rollback": rollback}


def health_check(project: dict, op: dict) -> dict:
    """Verify health AFTER the op. systemctl → is-active; compose → container health/
    running. Returns {healthy, detail}. Read-only."""
    if op.get("kind") == "systemctl_restart":
        rc, active = _run(None, "systemctl", "is-active", op["service"])
        return {"healthy": active == "active", "detail": {"is_active": active}}
    detail = {}
    healthy = True
    for s in op.get("services") or []:
        cname = (project.get("container_names") or {}).get(s, s)
        rc, out = _run(None, "docker", "inspect", "--format",
                       "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cname)
        detail[s] = out
        if out not in ("healthy", "running"):
            healthy = False
    return {"healthy": healthy, "detail": detail}
