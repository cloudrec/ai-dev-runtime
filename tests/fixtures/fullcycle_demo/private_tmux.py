"""TEST INFRASTRUCTURE ONLY — a tmux pane this adapter starts and owns.

Lives under tests/ on purpose: live code may neither type into panes except through
core.agent_control nor create panes outside the creation guard. To keep it from ever
touching the host's shared tmux server or launching a real agent CLI, it refuses:
  * any socket that is not an absolute `-S` path outside the shared tmux directory;
  * a launch command that is a known agent CLI (claude / qwen / codex).
It runs on any ExecutionHost, so the SSH contract test drives it through a stand-in ssh.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from core.fullcycle.executor import (CRASHED, DONE, WORKING, ExecutorAdapter, Observation,
                                     TaskEnvelope)
from core.fullcycle.hosts import ExecutionHost
from core.fullcycle.tmux_adapter import _permission, parse_result

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
AGENT_CLIS = ("claude", "qwen", "codex")


def _shared_tmux_dir() -> str:
    return os.path.realpath(os.path.join(os.getenv("TMUX_TMPDIR", "/tmp"), f"tmux-{os.getuid()}"))


class PrivateTmuxAdapter(ExecutorAdapter):
    name = "tmux"
    owns_agent = True

    def __init__(self, host: ExecutionHost, *, socket: str, session: str,
                 launch_argv: list, workdir: str, state_dir: str, env: Optional[dict] = None):
        if not socket.startswith("/") or any(c.isspace() for c in socket) \
                or ".." in socket.split("/"):
            raise ValueError(f"private socket must be a plain absolute path: {socket!r}")
        if os.path.realpath(os.path.dirname(socket)).startswith(_shared_tmux_dir()):
            raise ValueError("refusing a socket in the shared tmux directory")
        if os.path.basename(launch_argv[0]) in AGENT_CLIS if launch_argv else False:
            raise ValueError("refusing to launch an agent CLI outside the creation guard")
        self._sock = ["-S", socket]
        if not _NAME_RE.match(session):
            raise ValueError(f"unsafe tmux session {session!r}")
        if not launch_argv:
            raise ValueError("launch_argv required")
        self.host, self.socket, self.session = host, socket, session
        self.launch_argv, self.workdir, self.state_dir = list(launch_argv), workdir, state_dir
        self.env = dict(env or {})
        self.target = f"{session}:0.0"

    def _tmux(self, *args, timeout: float = 15) -> tuple:
        return self.host.run(["tmux", *self._sock, *args], timeout=timeout)

    def _launch(self) -> list:
        if not self.env:
            return self.launch_argv
        return ["env", *[f"{k}={v}" for k, v in sorted(self.env.items())], *self.launch_argv]

    def ensure_session(self) -> dict:
        if self._tmux("has-session", "-t", self.session)[0] == 0:
            return {"ok": True, "created": False}
        rc, _, err = self._tmux("new-session", "-d", "-s", self.session, "-x", "200",
                                "-y", "50", "-c", self.workdir, *self._launch())
        if rc != 0:
            return {"ok": False, "created": False, "reason": err.strip()}
        # keep a dead pane visible so a crash is observable, not a vanished session
        self._tmux("set-option", "-t", self.session, "remain-on-exit", "on")
        return {"ok": True, "created": True}

    def _pane(self) -> Optional[dict]:
        rc, out, _ = self._tmux("list-panes", "-t", self.target, "-F",
                                "#{pane_dead} #{pane_dead_status}")
        if rc != 0 or not out.strip():
            return None
        dead, _, status = out.strip().splitlines()[0].partition(" ")
        return {"dead": dead == "1", "exit_status": status}

    def _tail(self, lines: int = 40) -> str:
        rc, out, _ = self._tmux("capture-pane", "-p", "-t", self.target, "-S", f"-{lines}")
        return out if rc == 0 else ""

    def health(self) -> dict:
        pane = self._pane()
        return {"ok": bool(pane and not pane["dead"]), "pane": pane,
                "host": self.host.kind, "target": self.target}

    def _marker(self, key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return os.path.join(self.state_dir, "sent", safe)

    def submit(self, env: TaskEnvelope, idempotency_key: str) -> dict:
        handle = {"target": self.target, "task_id": env.task_id, "attempt": env.attempt,
                  "envelope": env.envelope_path, "result": env.result_path,
                  "key": idempotency_key}
        marker = self._marker(idempotency_key)
        if (self.host.read_file(marker) or "").strip() == "sent":
            return {"ok": True, "already": True, "reason": "already_sent", "handle": handle}
        # AT-LEAST-ONCE: a crash after the keys went out but before "sent" was written
        # sends the same prompt again; the envelope names task+attempt so an agent (and
        # the fixture agent) can tell. Never at-most-once: a lost prompt is a silent hang.
        pane = self._pane()
        if not pane or pane["dead"]:
            return {"ok": False, "already": False, "reason": "agent_not_running",
                    "handle": handle}
        self.host.write_file(env.envelope_path, env.to_json())
        self.host.write_file(marker, "sending")
        # clear whatever a half-finished earlier send left on the input line
        self._tmux("send-keys", "-t", self.target, "C-u")
        rc1, _, e1 = self._tmux("send-keys", "-t", self.target, "-l", "--", env.prompt())
        rc2, _, e2 = self._tmux("send-keys", "-t", self.target, "Enter") if not rc1 else (1, "", "")
        if rc1 or rc2:
            return {"ok": False, "already": False, "reason": f"send_failed:{(e1 or e2).strip()}",
                    "handle": handle}
        self.host.write_file(marker, "sent")
        return {"ok": True, "already": False, "reason": "sent", "handle": handle}

    def poll(self, handle: dict) -> Observation:
        if parse_result(self.host.read_file(handle["result"]), handle):
            return Observation(DONE)
        pane = self._pane()
        if pane is None or pane["dead"]:
            return Observation(CRASHED, {"pane": pane})
        return _permission(self._tail()) or Observation(WORKING)

    def collect(self, handle: dict) -> Optional[dict]:
        return parse_result(self.host.read_file(handle["result"]), handle)

    def quiescent(self, handle: dict) -> Optional[bool]:
        # the fixture agent handles one envelope at a time from its stdin, so a new
        # one can never run beside the old one; tests override this to model "active"
        return True

    def restart(self, handle: dict) -> dict:
        if self._tmux("has-session", "-t", self.session)[0] != 0:
            return {**self.ensure_session(), "how": "new_session"}
        rc, _, err = self._tmux("respawn-pane", "-k", "-t", self.target, "-c", self.workdir,
                                *self._launch())
        return {"ok": rc == 0, "how": "respawn_pane", "reason": err.strip()}

    def kill(self) -> None:
        self._tmux("kill-server")


