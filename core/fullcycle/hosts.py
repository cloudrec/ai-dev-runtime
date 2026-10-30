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
import shlex
import subprocess
from abc import ABC, abstractmethod
from typing import Optional, Sequence


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


class LocalHost(ExecutionHost):
    kind = "local"
    tested = True

    def run(self, argv, *, cwd=None, timeout=60, stdin=None, env=None):
        try:
            p = subprocess.run(list(argv), cwd=cwd, input=stdin, capture_output=True,
                               timeout=timeout, env={**os.environ, **env} if env else None)
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
