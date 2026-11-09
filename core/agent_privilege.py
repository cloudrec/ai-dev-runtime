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

import errno
import glob
import io
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence

_NAME_RE = re.compile(r"\A[a-z_][a-z0-9_-]{0,31}\Z")
_PROBE_TIMEOUT = 10
_LANG = "C.UTF-8"
_DEFAULT_DB = "/root/ai-dev-runtime/agent_control.db"
_MAX_LINK_HOPS = 40
# Per open, in bytes actually read: a sparse file costs the agent nothing and root its memory.
# Well above the 41MB seen on a live transcript. Seeking to a file's tail spends none of it.
AGENT_FILE_MAX_BYTES = 256 * 1024 * 1024


class AgentPrivilegeError(RuntimeError):
    """AGENT_RUN_AS is set and cannot be honoured. The text names no secret."""


@dataclass(frozen=True)
class Target:
    name: str
    uid: int
    gid: int
    home: str


def _pin_path() -> str:
    return os.getenv("AGENT_CONTROL_DB", _DEFAULT_DB) + ".run_as"


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
    """Written by target() before any dropped work. Raises OSError if it cannot be written.
    link() so concurrent first uses agree: the file appears whole, and never changes name."""
    if _pinned() == t.name:
        return
    path = _pin_path()
    fd, tmp = tempfile.mkstemp(prefix=".run_as.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(t.name + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            if _pinned() != t.name:
                raise
    finally:
        os.unlink(tmp)


def _lookup(name: str) -> Target:
    try:
        p = pwd.getpwnam(name)
    except KeyError:
        raise AgentPrivilegeError("user_not_found") from None
    return Target(name, p.pw_uid, p.pw_gid, p.pw_dir)


def resolve() -> Optional[Target]:
    """The checks of target() without its pin, for a tool that only looks.
    The variable must come with its instance's DB. Without it the pin path falls back to the
    default instance, so a tool run by hand in a shell holding only AGENT_RUN_AS would drop
    the services of that instance to the agent's user."""
    if os.getenv("AGENT_RUN_AS", "").strip() and not os.getenv("AGENT_CONTROL_DB", "").strip():
        raise AgentPrivilegeError("control_db_unset")
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


def target() -> Optional[Target]:
    """Who agents run as; None when AGENT_RUN_AS is unset. Raises when it cannot be honoured.
    Every drop passes here, so the pin exists before the first one, whichever comes first."""
    t = resolve()
    if t:
        try:
            pin(t)
        except OSError:
            raise AgentPrivilegeError("pin_failed") from None
    return t


def _setpriv_bin() -> Optional[str]:
    """Always a root-owned system path. subprocess resolves argv[0] through the PATH of the
    env it is given, and child_env's PATH starts in the agent's home."""
    return shutil.which("setpriv", path="/usr/bin:/bin")


def _setpriv(t: Target) -> list:
    return [*sandbox_prefix(t), _setpriv_bin(), f"--reuid={t.uid}", f"--regid={t.gid}",
            "--clear-groups", "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all"]


# ── what a dropped agent can see ────────────────────────────────────────────────────
# A uid alone hides little here: /opt is world-readable, and so is most of what lives in it,
# payment code and its .env files included (measured 2026-11-09 as `nobody`). With
# AGENT_SANDBOX_KEEP set, every drop first enters a private mount namespace where the
# directories below are empty tmpfs, except the kept paths (the instance's project root,
# and the agent's home) bound back in place. Mounts made there never reach the host.
# The namespace is entered by root and left by exec, so the pane's process ends as the
# agent itself (pane_problem checks that pid). /proc there is a fresh mount with
# hidepid=invisible: the agent sees its own processes only.
SANDBOX_HIDE = ("/opt", "/srv", "/mnt", "/media", "/home", "/root", "/var/www",
                "/var/backups", "/tmp", "/var/tmp")
_SANDBOX_PATH_RE = re.compile(r"\A/[A-Za-z0-9._/-]+\Z")
_SANDBOX_SCRIPT = r"""
set -eu
stage=$(/usr/bin/mktemp -d /run/agent-sandbox.XXXXXX)
n=0; kept=""
while [ "$1" != "--hide" ]; do
  /usr/bin/mkdir -p "$stage/$n"; /usr/bin/mount --bind "$1" "$stage/$n"
  kept="$kept $n:$1"; n=$((n+1)); shift
done
shift
while [ "$1" != "--" ]; do
  d=${1%:*}; m=${1##*:}
  if [ -d "$d" ]; then /usr/bin/mount -t tmpfs -o "mode=$m,nosuid,nodev" agent-sandbox "$d"; fi
  shift
done
shift
for e in $kept; do
  i=${e%%:*}; k=${e#*:}
  /usr/bin/mkdir -p "$k"; /usr/bin/mount --bind "$stage/$i" "$k"; /usr/bin/umount "$stage/$i"
done
# its own view of /proc: other users' processes, and the secrets some keep in their
# arguments (seen 2026-11-09), are invisible; the host's /proc is unchanged
/usr/bin/mount -t proc -o hidepid=invisible,nosuid,nodev,noexec agent-sandbox-proc /proc
exec "$@"
"""


def sandbox_keep(t: Target, raw: Optional[str] = None) -> list:
    """Paths the dropped agent keeps; [] when the sandbox is off. Raises on a bad setting.
    `raw` overrides AGENT_SANDBOX_KEEP for a process that is not itself the sandboxed
    instance but has to run something as its agent (the API reading a project branch)."""
    raw = (os.getenv("AGENT_SANDBOX_KEEP", "") if raw is None else raw).strip()
    if not raw:
        return []
    keep = [p for p in raw.split(":") if p] + [t.home]
    for p in keep:
        if not _SANDBOX_PATH_RE.match(p) or ".." in p.split("/") or \
                os.path.realpath(p) != os.path.normpath(p) or not os.path.isdir(p):
            raise AgentPrivilegeError("sandbox_keep_unsafe")
        if os.path.normpath(p) in ("/", *SANDBOX_HIDE):
            raise AgentPrivilegeError("sandbox_keep_too_broad")
    return [os.path.normpath(p) for p in keep]


def sandbox_prefix(t: Target, keep_raw: Optional[str] = None) -> list:
    keep = sandbox_keep(t, keep_raw)
    if not keep:
        return []
    unshare = shutil.which("unshare", path="/usr/bin:/bin")
    if not unshare or not os.path.exists("/usr/bin/mount"):
        raise AgentPrivilegeError("sandbox_unavailable")
    return [unshare, "--mount", "--propagation", "private", "--", "/bin/sh", "-c",
            _SANDBOX_SCRIPT, "agent-sandbox", *keep, "--hide",
            *(f"{d}:{_hidden_mode(d, keep)}" for d in SANDBOX_HIDE), "--"]


def _hidden_mode(d: str, keep: list) -> str:
    """The empty tmpfs over a hidden directory: temp dirs as usual (sticky, world-writable);
    a directory on the way to a kept path can be passed through but not listed; the rest
    cannot be entered at all."""
    if d in ("/tmp", "/var/tmp"):
        return "1777"
    if any(k.startswith(d.rstrip("/") + "/") for k in keep):
        return "0711"
    return "0700"


def _path(t: Target) -> str:
    return f"{t.home}/.local/bin:/usr/local/bin:/usr/bin:/bin"


def pane_prefix(t: Target) -> list:
    """argv prefix for a tmux pane. --reset-env drops the tmux server's environment and
    sets USER, LOGNAME and SHELL from the target; TERM survives."""
    return [*_setpriv(t), "--reset-env", "env", f"PATH={_path(t)}", f"HOME={t.home}",
            f"LANG={_LANG}"]


def sandboxed_argv(t: Target, keep_raw: str, argv: Sequence[str]) -> list:
    """argv run as `t` inside the sandbox kept to `keep_raw`, from a process whose own
    environment carries no AGENT_RUN_AS. Refuses rather than run unsandboxed."""
    prefix = sandbox_prefix(t, keep_raw)
    if not prefix:
        raise AgentPrivilegeError("sandbox_keep_unset")
    return [*prefix, _setpriv_bin(), f"--reuid={t.uid}", f"--regid={t.gid}", "--clear-groups",
            "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", *argv]


def host_argv(t: Target, argv: Sequence[str]) -> list:
    """argv for a child whose environment the caller passes, built by child_env."""
    return [*_setpriv(t), *argv]


def child_env(t: Target, extra: Optional[dict] = None) -> dict:
    """The whole environment of a dropped child. `extra` is the caller's, minus API variables."""
    from core.agent_auth_mode import is_api_env_var
    env = {k: v for k, v in (extra or {}).items() if not is_api_env_var(k)}
    env.update(PATH=_path(t), HOME=t.home, USER=t.name, LOGNAME=t.name, LANG=_LANG)
    return env


def _owns_tree(t: Target, path: str) -> bool:
    """The tree and its .git are the agent's own. Root's git trusts a root-owned tree, so
    a chmod instead of a chown hands the agent a config root would run."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != t.uid:
            return False
        git = os.lstat(os.path.join(path, ".git"))
    except FileNotFoundError:
        return os.path.lexists(path)
    except OSError:
        return False
    return stat.S_ISDIR(git.st_mode) and git.st_uid == t.uid


def can_use_dir(t: Target, path: str) -> bool:
    """Owned by the target, then asked of the target itself: root's view of a mode says
    nothing about it."""
    if not _owns_tree(t, path):
        return False
    argv = host_argv(t, ["test", "-d", path, "-a", "-r", path, "-a", "-w", path, "-a", "-x", path])
    try:
        return subprocess.run(argv, env=child_env(t), cwd="/", capture_output=True,
                              timeout=_PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def require_root_work(path: str) -> None:
    """For tooling that stays root: the job queue, deliver, release, validator, /run, and
    every root git read of a project. A read still loads the repo's config.
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


class _Capped(io.RawIOBase):
    """A file that refuses past AGENT_FILE_MAX_BYTES read, counted on the reads themselves:
    a size check beside the open would miss a file grown after it."""

    def __init__(self, fd: int):
        super().__init__()
        self._fd, self._left = fd, AGENT_FILE_MAX_BYTES

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd

    def seek(self, pos: int, whence: int = os.SEEK_SET) -> int:
        return os.lseek(self._fd, pos, whence)

    def readinto(self, buf) -> int:
        n = os.readv(self._fd, [memoryview(buf)[:self._left + 1]])
        if n > self._left:
            raise OSError(errno.EFBIG, "agent file over the read limit")
        self._left -= n
        return n

    def close(self) -> None:
        if not self.closed:
            os.close(self._fd)
        super().close()


def _open_no_agent_links(path: str) -> int:
    """Walk the path one directory handle at a time, so nothing can swap a component between
    the check and the use. A link owned by root is followed: the agent can neither make nor
    change one. Any other link in the path refuses, and so does a link as the last component."""
    path = os.path.join(os.getcwd(), os.fspath(path))
    for _ in range(_MAX_LINK_HOPS):
        parts = [p for p in path.split("/") if p and p != "."]
        if not parts:
            raise IsADirectoryError(errno.EISDIR, "not a file", path)
        at = os.open("/", os.O_PATH | os.O_DIRECTORY)
        try:
            for i, name in enumerate(parts[:-1]):
                fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=at)
                st = os.fstat(fd)
                if stat.S_ISDIR(st.st_mode):
                    os.close(at)
                    at = fd
                    continue
                os.close(fd)
                if not stat.S_ISLNK(st.st_mode):
                    raise NotADirectoryError(errno.ENOTDIR, "not a directory", path)
                if st.st_uid != 0:
                    raise OSError(errno.ELOOP, "link not owned by root", path)
                path = os.path.join("/", *parts[:i], os.readlink(name, dir_fd=at),
                                    *parts[i + 1:])
                break
            else:
                return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=at)
        finally:
            os.close(at)
    raise OSError(errno.ELOOP, "too many links", path)


def open_regular(path: str, mode: str = "r", encoding=None, errors=None):
    """open() for a file the agent may own. Under the setting: no agent link anywhere in the
    path, no FIFO to hang on, no more than AGENT_FILE_MAX_BYTES read. An over-long read
    raises OSError from read() or iteration, not from here. Plain open() when AGENT_RUN_AS
    is unset, so a symlinked dotfile keeps working."""
    if not run_as_name():
        return open(path, mode, encoding=encoding, errors=errors)
    if mode not in ("r", "rb"):
        raise ValueError(f"read only: {mode!r}")
    fd = _open_no_agent_links(path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        reader = io.BufferedReader(_Capped(fd))
    except BaseException:
        os.close(fd)
        raise
    return reader if mode == "rb" else io.TextIOWrapper(reader, encoding=encoding, errors=errors)


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
