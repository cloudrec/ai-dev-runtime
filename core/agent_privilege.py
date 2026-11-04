"""Run reviewed agents under their own uid, so they cannot read what the runtime can.

Off unless AGENT_RUN_AS names a user. With it set, the runtime stays root and everything
that runs code the agent can influence drops to that user first: the agent's pane, session
recovery, the full-cycle engine's git, checks and file access, and job steps. The two sides
must keep different uids: an agent sharing the runtime's uid could rewrite the client tokens
file and read the owner key, so a non-root runtime is refused here, not supported.

A problem with the setting refuses the start. It never falls back to root.

setpriv clears supplementary groups, so membership of `docker` does not survive, and sets
no_new_privs, so sudo and su cannot climb back. The environment is rebuilt, not filtered: a
tmux server hands its panes whatever the process that started it had.
"""
from __future__ import annotations

import glob
import os
import pwd
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

_NAME_RE = re.compile(r"\A[a-z_][a-z0-9_-]{0,31}\Z")
_PROBE_TIMEOUT = 10
_LANG = "C.UTF-8"


class AgentPrivilegeError(RuntimeError):
    """AGENT_RUN_AS is set and cannot be honoured. The text names no secret."""


@dataclass(frozen=True)
class Target:
    name: str
    uid: int
    gid: int
    home: str


def run_as_name() -> str:
    return os.getenv("AGENT_RUN_AS", "").strip()


def _lookup(name: str) -> Target:
    try:
        p = pwd.getpwnam(name)
    except KeyError:
        raise AgentPrivilegeError("user_not_found") from None
    return Target(name, p.pw_uid, p.pw_gid, p.pw_dir)


def target() -> Optional[Target]:
    """Who agents run as; None when AGENT_RUN_AS is unset. Raises when it cannot be honoured."""
    name = run_as_name()
    if not name:
        return None
    if not _NAME_RE.match(name):
        raise AgentPrivilegeError("bad_user_name")
    t = _lookup(name)
    if t.uid == 0 or t.gid == 0:
        raise AgentPrivilegeError("target_is_root")
    if os.geteuid() != 0:
        raise AgentPrivilegeError("runtime_not_root")
    if not shutil.which("setpriv"):
        raise AgentPrivilegeError("setpriv_missing")
    return t


def _setpriv(t: Target) -> list:
    return ["setpriv", f"--reuid={t.uid}", f"--regid={t.gid}", "--clear-groups",
            "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all"]


def _path(t: Target) -> str:
    return f"{t.home}/.local/bin:/usr/local/bin:/usr/bin:/bin"


def pane_prefix(t: Target) -> list:
    """argv prefix for a tmux pane. --reset-env drops the tmux server's environment and
    sets USER, LOGNAME and SHELL from the target; TERM survives."""
    return [*_setpriv(t), "--reset-env", "env", f"PATH={_path(t)}", f"HOME={t.home}",
            f"LANG={_LANG}"]


def host_argv(t: Target, argv: Sequence[str]) -> list:
    """argv for a child whose environment the caller passes, built by child_env."""
    return [*_setpriv(t), *argv]


def child_env(t: Target, extra: Optional[dict] = None) -> dict:
    """The whole environment of a dropped child. `extra` is the caller's, minus API variables."""
    from core.agent_auth_mode import is_api_env_var
    env = {k: v for k, v in (extra or {}).items() if not is_api_env_var(k)}
    env.update(PATH=_path(t), HOME=t.home, USER=t.name, LOGNAME=t.name, LANG=_LANG)
    return env


def can_use_dir(t: Target, path: str) -> bool:
    """Asked of the target itself: root's view of a mode says nothing about it."""
    argv = host_argv(t, ["test", "-d", path, "-a", "-r", path, "-a", "-w", path, "-a", "-x", path])
    try:
        return subprocess.run(argv, env=child_env(t), cwd="/", capture_output=True,
                              timeout=_PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def claude_home() -> str:
    """Whose ~/.claude holds the transcripts. Never raises: it is on the status path."""
    name = run_as_name()
    if name:
        try:
            return _lookup(name).home
        except AgentPrivilegeError:
            pass
    return "/root"


def transcripts(cwd: str) -> list:
    """This project's transcript files, regular files only. Root reads these, and the agent
    owns the directory: a FIFO there would hang the reader, a symlink would redirect it."""
    proj = os.path.join(claude_home(), ".claude", "projects", (cwd or "").replace("/", "-"))
    out = []
    for path in glob.glob(proj + "/*.jsonl"):
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                out.append(path)
        except OSError:
            continue
    return out
