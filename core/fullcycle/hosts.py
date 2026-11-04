"""Where an executor runs. One contract, three places:

  LocalHost   this machine (Linux; macOS is the same POSIX contract, not yet run there)
  SshHost     a remote POSIX machine over OpenSSH — the remote mode
  WindowsBridgeHost  the owner's Windows PC through core.windows_bridge — TODO

The tmux adapter only ever calls run / write_file / read_file / exists, so the same
adapter drives a local pane or a pane on a remote host. SshHost builds a plain `ssh`
argv (BatchMode, no password prompts, no agent forwarding); the tests drive it through
a stand-in `ssh` binary that executes locally, which proves the argv and quoting
contract — not that any particular remote machine is reachable.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from abc import ABC, abstractmethod
from typing import Optional, Sequence

from core.agent_auth_mode import is_api_env_var, subscription_env


class HostError(RuntimeError):
    pass


class ExecutionHost(ABC):
    kind = "abstract"
    #: has this host kind been driven by the test suite end to end?
    tested = False

    @abstractmethod
    def run(self, argv: Sequence[str], *, cwd: Optional[str] = None,
            timeout: float = 60, stdin: Optional[bytes] = None,
            env: Optional[dict] = None) -> tuple:
        """(returncode, stdout, stderr) — never raises for a non-zero exit. `env` is
        ADDED to the host's environment."""

    def write_file(self, path: str, data: str) -> None:
        """Atomic: written to a sibling temp name, then renamed."""
        d = os.path.dirname(path)
        rc, _, err = self.run(["sh", "-c", 'mkdir -p "$1" && cat > "$2.tmp" && mv "$2.tmp" "$2"',
                               "sh", d, path], stdin=data.encode())
        if rc != 0:
            raise HostError(f"write_file {path}: {err.strip()}")

    def read_file(self, path: str) -> Optional[str]:
        rc, out, _ = self.run(["cat", path])
        return out if rc == 0 else None

    def exists(self, path: str) -> bool:
        return self.run(["test", "-e", path])[0] == 0


# what commonly stands in front of a command, with its options that take a value
_EXEC_WRAPPERS = {"env": ("-u", "-C", "--unset", "--chdir"), "nohup": (), "setsid": (),
                  "timeout": ("-k", "-s", "--kill-after", "--signal"), "nice": ("-n",),
                  "xargs": ("-I", "-L", "-P", "-d", "-n", "-s"), "sudo": ("-u", "-g"),
                  "command": (), "exec": (), "time": ()}
_SHELLS = {"sh", "bash", "dash", "zsh"}
_SHELL_KEYWORDS = {"if", "then", "elif", "else", "while", "until", "do", "{", "!"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"[0-9.]+[smhd]?\Z")
_CLAUDE_WORD = re.compile(r"(?:^|[\s/;&|(=])claude(?:[\s;&|)]|$)")


def _after_wrapper(exe: str, words: list) -> list:
    """The command an exec wrapper runs: its options, assignments and numbers skipped."""
    takes_value = _EXEC_WRAPPERS[exe]
    while words:
        w = words[0]
        if w == "--":
            return words[1:]
        if w in takes_value:
            words = words[2:]
        elif w.startswith("-") or _ASSIGNMENT.match(w) or _DURATION.match(w):
            words = words[1:]
        else:
            break
    return words


def _script_runs_claude(script: str) -> bool:
    """Whether a shell string has `claude` in command position. Quoting is honoured, so a
    word inside an argument stays data; a string that cannot be parsed is read loosely."""
    lex = shlex.shlex(script.replace("\n", ";"), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return bool(_CLAUDE_WORD.search(script))
    command: list = []
    for token in tokens + [";"]:
        if token and set(token) <= set("();<>|&"):
            if _runs_claude(command):
                return True
            command = []
        elif command or not (token in _SHELL_KEYWORDS or _ASSIGNMENT.match(token)):
            command.append(token)
    return False


def _runs_claude(argv: Sequence[str]) -> bool:
    """A direct `claude`, or one a shell or exec wrapper will run (N3). Only command position
    counts, so `bash -c "pytest -k claude"` keeps its environment (L2). A wrapper this cannot
    see through falls to the API-variable denylist in _child_env."""
    words = [str(a) for a in argv]
    while words:
        exe = os.path.basename(words[0])
        if exe == "claude":
            return True
        if exe in _SHELLS:
            rest = words[1:]
            while rest and rest[0].startswith(("-", "+")):
                opt, rest = rest[0], rest[1:]
                if opt in ("-o", "+o", "-O", "+O"):
                    rest = rest[1:]
                elif not opt.startswith("--") and "c" in opt:
                    return bool(rest) and _script_runs_claude(rest[0])
            return False
        if exe not in _EXEC_WRAPPERS:
            return False
        words = _after_wrapper(exe, words[1:])
    return False


def _child_env(argv: Sequence[str], env: Optional[dict]) -> dict:
    """No local child inherits API credentials, whatever it is: a check script may call
    `claude` itself. Anything recognisably running `claude` gets the full subscription
    allowlist (F2). A wrapper this cannot see through still loses every API variable."""
    merged = {**os.environ, **(env or {})}
    if _runs_claude(argv):
        return subscription_env(merged)
    return {k: v for k, v in merged.items() if not is_api_env_var(k)}


class LocalHost(ExecutionHost):
    kind = "local"
    tested = True

    def run(self, argv, *, cwd=None, timeout=60, stdin=None, env=None):
        try:
            p = subprocess.run(list(argv), cwd=cwd, input=stdin, capture_output=True,
                               timeout=timeout, env=_child_env(argv, env))
        except subprocess.TimeoutExpired:
            return 124, "", f"timeout after {timeout}s"
        except FileNotFoundError as e:
            return 127, "", str(e)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")


class SshHost(ExecutionHost):
    kind = "ssh"
    tested = True   # the argv/quoting contract, via the stand-in ssh in the tests

    def __init__(self, destination: str, *, ssh_bin: str = "ssh", port: Optional[int] = None,
                 identity_file: Optional[str] = None, connect_timeout: int = 10):
        if not destination or destination.startswith("-") or any(c.isspace() for c in destination):
            raise HostError(f"refusing ssh destination {destination!r}")
        self.destination = destination
        self.ssh_bin = ssh_bin
        self.port = port
        self.identity_file = identity_file
        self.connect_timeout = connect_timeout

    def argv(self, remote_argv: Sequence[str], cwd: Optional[str] = None) -> list:
        cmd = shlex.join(list(remote_argv))
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        out = [self.ssh_bin, "-o", "BatchMode=yes", "-o", "ForwardAgent=no",
               "-o", f"ConnectTimeout={self.connect_timeout}", "-T"]
        if self.port:
            out += ["-p", str(int(self.port))]
        if self.identity_file:
            out += ["-i", self.identity_file, "-o", "IdentitiesOnly=yes"]
        return out + ["--", self.destination, cmd]

    def run(self, argv, *, cwd=None, timeout=60, stdin=None, env=None):
        if env:
            argv = ["env", *[f"{k}={v}" for k, v in sorted(env.items())], *argv]
        return LocalHost().run(self.argv(argv, cwd), timeout=timeout, stdin=stdin)


class WindowsBridgeHost(ExecutionHost):
    """TODO (stated, not claimed): core.windows_bridge already enrols a Windows device
    and leases signed commands to it, but it exposes agent verbs (list/status/read/send),
    not argv execution or tmux. A Windows executor therefore needs its own adapter over
    those verbs (no tmux on Windows), not this host. Every call refuses."""
    kind = "windows_bridge"
    tested = False

    def __init__(self, device_id: str):
        self.device_id = device_id

    def run(self, argv, *, cwd=None, timeout=60, stdin=None, env=None):
        raise HostError("windows_bridge host is not implemented; see class docstring")
