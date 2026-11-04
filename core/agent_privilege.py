"""Run reviewed agents under their own uid, so they cannot read what the runtime can.

Off unless AGENT_RUN_AS names a user. With it set, the runtime stays root and everything
that runs code the agent can influence drops to that user first: the agent's pane, session
recovery, the full-cycle engine's git, checks and file access, and job steps. The two sides
must keep different uids: an agent sharing the runtime's uid could rewrite the client tokens
file and read the owner key, so a non-root runtime is refused here, not supported.

A problem with the setting refuses the start. It never falls back to root. Tooling that
stays root refuses to run at all under the setting: see require_root_work.

The setting is pinned beside the agent DB when the first dropped agent starts, so a worker,
companion or recovery process of the same instance that lacks the variable still obeys it.
The pin is per instance, not host-wide: another runtime on this host is not affected.

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


def _pin_path() -> str:
    db = os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")
    return db + ".run_as"


def _pinned() -> str:
    """The pinned name, "" when there is none, or a !reason that target() refuses."""
    try:
        st = os.lstat(_pin_path())
    except OSError:
        return ""
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or (st.st_mode & 0o022):
        return "!pin_unsafe"
    try:
        with open(_pin_path(), encoding="ascii") as fh:
            return fh.read(64).strip() or "!pin_unsafe"
    except (OSError, ValueError):
        return "!pin_unsafe"


def run_as_name() -> str:
    """Truthy whenever the setting is in force or broken. Never raises: status paths use it."""
    env, pin = os.getenv("AGENT_RUN_AS", "").strip(), _pinned()
    if pin.startswith("!"):
        return pin
    if env and pin and env != pin:
        return "!setting_mismatch"
    return env or pin


def pin(t: Target) -> None:
    """Called before the first dropped agent starts. Raises OSError if it cannot be written."""
    if _pinned() == t.name:
        return
    tmp = _pin_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(t.name + "\n")
    os.replace(tmp, _pin_path())


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
    if name.startswith("!"):
        raise AgentPrivilegeError(name[1:])
    if not _NAME_RE.match(name):
        raise AgentPrivilegeError("bad_user_name")
    t = _lookup(name)
    if t.uid == 0 or t.gid == 0:
        raise AgentPrivilegeError("target_is_root")
    if os.geteuid() != 0:
        raise AgentPrivilegeError("runtime_not_root")
    if not _setpriv_bin():
        raise AgentPrivilegeError("setpriv_missing")
    return t


def _setpriv_bin() -> Optional[str]:
    """Always a root-owned system path. subprocess resolves argv[0] through the PATH of the
    env it is given, and child_env's PATH starts in the agent's home."""
    return shutil.which("setpriv", path="/usr/bin:/bin")


def _setpriv(t: Target) -> list:
    return [_setpriv_bin(), f"--reuid={t.uid}", f"--regid={t.gid}", "--clear-groups",
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


def require_root_work(path: str) -> None:
    """For tooling that stays root: the job queue, deliver, release, validator and /run.
    They run git, pytest and file walks, and no check of a tree holds: ownership, a `.git`
    file pointing elsewhere, one writable subdirectory, a symlink swapped after the check.
    Two audits broke each narrower rule. So with AGENT_RUN_AS in force they do not run at
    all, and an instance with agents under their own uid works through the full-cycle
    engine only, which runs as the agent. Raises; silent when the setting is off."""
    if target():
        raise AgentPrivilegeError("root_tooling_disabled")


def process_uid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{int(pid)}/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[2])       # effective
    except (OSError, ValueError, IndexError, TypeError):
        pass
    return None


def pane_problem(*pids: Optional[int]) -> Optional[str]:
    """Do the pane's process and its agent all run as the target? A pane started before the
    setting or by hand would otherwise be driven as root, and a root shell around a dropped
    claude would receive the typed text once claude exits."""
    try:
        t = target()
    except AgentPrivilegeError as e:
        return str(e)
    if not t or (pids and all(process_uid(p) == t.uid for p in pids)):
        return None
    return "pane_uid_mismatch"


def write_as_agent(path: str, content: str) -> bool:
    """For a file in the agent's tree. As root a planted symlink would pick the target."""
    from core.fullcycle.hosts import HostError, LocalHost
    try:
        LocalHost().write_file(path, content)
    except HostError:
        return False
    return True


def open_regular(path: str, *args, **kwargs):
    """open() for a file the agent may own: no symlink, and a FIFO cannot hang the caller.
    Plain open() when AGENT_RUN_AS is unset, so a symlinked dotfile keeps working."""
    if not run_as_name():
        return open(path, *args, **kwargs)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        return os.fdopen(fd, *args, **kwargs)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def claude_home() -> str:
    """Whose ~/.claude holds the transcripts. Never raises: it is on the status path."""
    if not run_as_name():
        return "/root"
    try:
        t = target()
    except AgentPrivilegeError:
        t = None
    return t.home if t else "/nonexistent"     # not root's transcripts: nothing is better


def transcripts(cwd: str) -> list:
    """This project's transcript files, regular files only. Root reads these, and the agent
    owns the directory: a FIFO there would hang the reader, a symlink would redirect it."""
    proj = os.path.join(claude_home(), ".claude", "projects", (cwd or "").replace("/", "-"))
    found = glob.glob(proj + "/*.jsonl")
    if not run_as_name():
        return found
    out = []
    for path in found:
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                out.append(path)
        except OSError:
            continue
    return out
