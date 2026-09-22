"""Keep a tmux server that Owner OS starts OUT of Owner OS's own service cgroup.

WHY THIS MODULE EXISTS — the 2026-09-22 incident, root-caused, not inferred:

Owner OS runs as `ai-runtime.service` (KillMode=control-group, the systemd default).
Every session it creates goes through `tmux new-session -d`, and when no tmux server is
running that client FORKS the server — which inherits the caller's cgroup, i.e. the
service's. From then on the whole managed fleet lives inside the API daemon's cgroup,
and any restart of the daemon is a restart of the fleet:

  06:18:29  unattended-upgrade begins (libexpat1 among the packages)
  06:20:47  needrestart: `systemctl restart ... ai-runtime.service ...` (36 units)
  06:20:47  systemd SIGTERMs the control group; tmux exits on SIGTERM; every pane of
            server 131368 — owner-os-opus-clean, gaika-ru-sonnet-v1, main105-backup-
            repair, capacity-blockchain — ends in the same second (wtmp)
  06:21:48  event 57007 agent_control_plane_unreachable reason=no_server
  06:22:19  event 57008 agent_dead capacity-blockchain:0.0

The replacement server was spawned through the same path and sat in
`/system.slice/ai-runtime.service` again: the defect is in the spawn, not the host.

THE FIX. When this process is itself inside a systemd `.service`, run `new-session`
through `systemd-run --scope`: the client — and therefore the server it forks — lands
in a transient scope of its own. A scope survives its starter (it is "abandoned", not
killed, when systemd-run exits) and is unloaded by `--collect` only once it is empty,
so a daemon restart no longer reaches the fleet. If a server is already running the
scope holds nothing but a short-lived client and collects itself; there is no
check-then-act race to lose. Outside a service (tests, a shell, a container without
systemd) the prefix is empty and the command is exactly what it was.

This module does not decide WHETHER to start a server — `tmux_control` is explicit that
a missing server is an owner decision. It only decides WHERE one lands when a caller
that is allowed to start one does.
"""
from __future__ import annotations

import os
import shutil
import uuid
from typing import Callable, Optional

_SELF_CGROUP = "/proc/self/cgroup"
_SCOPE_DESCRIPTION = ("tmux server started by Owner OS — kept outside the API daemon's "
                      "cgroup so a daemon restart cannot kill the managed fleet")


def service_unit_from_cgroup(text: str) -> Optional[str]:
    """The systemd `.service` unit named by a /proc/<pid>/cgroup file, or None.

    cgroup v2 is one line, `0::/system.slice/ai-runtime.service`; v1 has one line per
    controller and the unit is on the `name=systemd` one. A `.scope` (an ssh session,
    a `systemd-run --scope`) or a user slice is NOT a service and returns None: those
    are not torn down by a service restart, which is the only thing this guards.
    """
    for line in (text or "").splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) != 3:
            continue
        hier, controllers, path = parts
        if hier != "0" and "name=systemd" not in controllers:
            continue
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if leaf.endswith(".service"):
            return leaf
    return None


def own_service_unit(cgroup_file: str = _SELF_CGROUP) -> Optional[str]:
    """The `.service` this process runs under, or None (not under systemd, or a scope)."""
    try:
        with open(cgroup_file, "r", encoding="utf-8", errors="replace") as fh:
            return service_unit_from_cgroup(fh.read())
    except OSError:
        return None


def unit_of_pid(pid: int) -> Optional[str]:
    """The `.service` a foreign pid runs under (None for a scope or when unreadable)."""
    return own_service_unit(f"/proc/{int(pid)}/cgroup")


def detach_prefix(*, cgroup_file: str = _SELF_CGROUP, which: Callable = shutil.which,
                  unit_name: Optional[str] = None) -> list[str]:
    """argv prefix that moves the tmux client (and any server it forks) into its own
    transient scope. Empty when there is nothing to escape from or no way to do it."""
    if not own_service_unit(cgroup_file):
        return []
    systemd_run = which("systemd-run")
    if not systemd_run:
        return []
    # Unique per call: a name that already exists (a prior scope still holding a
    # server) makes systemd-run refuse, and that must never fail a session start.
    name = unit_name or f"owner-os-tmux-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    return [systemd_run, "--scope", "--quiet", "--collect",
            "--unit", name, "--description", _SCOPE_DESCRIPTION]


def is_new_session(args: list) -> bool:
    return bool(args) and args[0] == "new-session"


def wrap_argv(argv: list[str], *, prefix: Optional[Callable[[], list[str]]] = None) -> list[str]:
    """`[tmux, new-session, ...]` -> `[systemd-run, ..., tmux, new-session, ...]` when this
    process is inside a service; every other tmux command is returned untouched, because
    only `new-session` can fork a server."""
    if len(argv) < 2 or not is_new_session(argv[1:]):
        return list(argv)
    return list((prefix or detach_prefix)()) + list(argv)
