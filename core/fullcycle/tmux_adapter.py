"""Executors that live in tmux panes.

TmuxAgentAdapter — a pane this adapter starts and owns, on its own tmux socket (`-L`),
on any ExecutionHost (local or SSH). It may restart its agent after a crash. Used for
dedicated orchestration agents and for the fixture agent in the tests.

AgentControlAdapter — an EXISTING live agent reached through core.agent_control, so
every send passes the policy already in force there (allowlist / grants / payment
hard-deny, refusal while the agent is mid-turn, idempotency keys, audit). It never
creates, restarts or stops an agent: a crash is reported, and the engine blocks the
task for the owner.

Both use the same envelope/result files, so the engine cannot tell them apart.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from core import permission_resolver as _pr
from core.fullcycle.executor import (CRASHED, DONE, UNKNOWN, WAITING_PERMISSION,
                                     WORKING, ExecutorAdapter, Observation, TaskEnvelope)
from core.fullcycle.hosts import ExecutionHost

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def parse_result(text: Optional[str], handle: dict) -> Optional[dict]:
    """The agent's result, or None when absent, unparsable, or for another attempt
    (a late file from attempt 1 must never close attempt 2)."""
    if not text:
        return None
    try:
        r = json.loads(text)
    except ValueError:
        return None
    if not isinstance(r, dict) or r.get("task_id") != handle.get("task_id") \
            or r.get("attempt") != handle.get("attempt"):
        return None
    if r.get("status") not in ("done", "blocked"):
        return None
    files = r.get("changed_files") or []
    r["changed_files"] = [f for f in files if isinstance(f, str)]
    r["summary"] = str(r.get("summary") or "")[:4000]
    return r


def _permission(tail: str) -> Optional[Observation]:
    if _pr.is_permission_prompt(tail):
        return Observation(WAITING_PERMISSION,
                           {"command": _pr.extract_pending_command(tail) or ""})
    return None


class TmuxAgentAdapter(ExecutorAdapter):
    name = "tmux"
    owns_agent = True

    def __init__(self, host: ExecutionHost, *, socket: str, session: str,
                 launch_argv: list, workdir: str, state_dir: str, env: Optional[dict] = None):
        # a bare name is a socket under the host's tmux dir (-L); an absolute path is
        # used as is (-S), which keeps test and demo servers out of the shared dir
        if socket.startswith("/"):
            if any(c.isspace() for c in socket) or ".." in socket.split("/"):
                raise ValueError(f"unsafe tmux socket path {socket!r}")
            self._sock = ["-S", socket]
        elif _NAME_RE.match(socket):
            self._sock = ["-L", socket]
        else:
            raise ValueError(f"unsafe tmux socket {socket!r}")
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
        if self.host.exists(self._marker(idempotency_key)):
            return {"ok": True, "already": True, "reason": "already_sent", "handle": handle}
        pane = self._pane()
        if not pane or pane["dead"]:
            return {"ok": False, "already": False, "reason": "agent_not_running",
                    "handle": handle}
        self.host.write_file(env.envelope_path, env.to_json())
        rc1, _, e1 = self._tmux("send-keys", "-t", self.target, "-l", "--", env.prompt())
        rc2, _, e2 = self._tmux("send-keys", "-t", self.target, "Enter")
        if rc1 or rc2:
            return {"ok": False, "already": False, "reason": f"send_failed:{(e1 or e2).strip()}",
                    "handle": handle}
        self.host.write_file(self._marker(idempotency_key), env.task_id)
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

    def restart(self, handle: dict) -> dict:
        if self._tmux("has-session", "-t", self.session)[0] != 0:
            return {**self.ensure_session(), "how": "new_session"}
        rc, _, err = self._tmux("respawn-pane", "-k", "-t", self.target, "-c", self.workdir,
                                *self._launch())
        return {"ok": rc == 0, "how": "respawn_pane", "reason": err.strip()}

    def kill(self) -> None:
        self._tmux("kill-server")


class AgentControlAdapter(ExecutorAdapter):
    """An existing live agent; files are on this host."""
    name = "agent_control"
    owns_agent = False

    def __init__(self, target: str, *, actor: str = "fullcycle:engine", host=None, ac=None):
        from core.fullcycle.hosts import LocalHost
        if ac is None:
            from core import agent_control as ac
        self.target, self.actor, self.ac = target, actor, ac
        self.host = host or LocalHost()

    def health(self) -> dict:
        try:
            st = self.ac.agent_status(self.target, actor=self.actor)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        return {"ok": bool(st.get("alive") and st.get("is_agent")), "state": st.get("state")}

    def submit(self, env: TaskEnvelope, idempotency_key: str) -> dict:
        handle = {"target": self.target, "task_id": env.task_id, "attempt": env.attempt,
                  "envelope": env.envelope_path, "result": env.result_path,
                  "key": idempotency_key}
        self.host.write_file(env.envelope_path, env.to_json())
        try:
            r = self.ac.agent_send(self.target, env.prompt(), idempotency_key=idempotency_key,
                                   actor=self.actor, source="fullcycle")
        except Exception as e:  # noqa: BLE001 — AgentControlError carries the refusal
            return {"ok": False, "already": False, "reason": f"refused:{e}", "handle": handle}
        if r.get("duplicate"):
            # agent_control answers a reused key from its delivery record without
            # touching the pane; "delivered" is False there by design
            return {"ok": bool(r.get("submitted")), "already": True,
                    "reason": "already_sent" if r.get("submitted") else "prior_send_failed",
                    "handle": handle}
        if r.get("delivered") or r.get("submitted"):
            return {"ok": True, "already": False, "reason": "sent", "handle": handle}
        return {"ok": False, "already": False,
                "reason": f"refused:{r.get('refused') or r.get('reason') or 'not_delivered'}",
                "handle": handle}

    def poll(self, handle: dict) -> Observation:
        if parse_result(self.host.read_file(handle["result"]), handle):
            return Observation(DONE)
        try:
            st = self.ac.agent_status(self.target, actor=self.actor)
        except Exception as e:  # noqa: BLE001
            return Observation(UNKNOWN, {"reason": str(e)})
        if not st.get("alive") or not st.get("is_agent"):
            return Observation(CRASHED, {"state": st.get("state")})
        return _permission(st.get("recent_activity") or "") or Observation(WORKING)

    def collect(self, handle: dict) -> Optional[dict]:
        return parse_result(self.host.read_file(handle["result"]), handle)
