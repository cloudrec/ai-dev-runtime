"""Owner OS Windows Agent — the device half of the Windows bridge (task 220).

Runs on the owner's Windows PC and gives Owner OS the same verbs it already has
for the tmux agents on the server — status / read / send / start / stop — for
Claude Code sessions in explicitly enrolled local folders such as
C:\\Users\\...\\gaika-basket-extension.

Shape of the thing
------------------
* **Outbound only.** This process opens every connection. Nothing listens on the
  Windows machine, so no port is exposed to the network and no firewall rule is
  needed. Control flows in as the RESPONSE to a long-poll the device itself
  started.
* **Stdlib only.** urllib/hmac/json/subprocess. No pip install on the owner's
  machine beyond Python itself, and the whole file can be tested on Linux.
* **Workspaces are local truth.** A command from the server names a workspace
  ID; this file resolves that ID against the local config written by
  `add-workspace`. The server never sends, and cannot send, a path. A workspace
  must be enrolled here, deliberately, before it is reachable at all.
* **No shell, ever.** Claude is invoked as an argv list with the prompt handed
  over on STDIN. On Windows `claude` is a `.cmd` shim, and arguments to a `.cmd`
  are re-parsed by cmd.exe (the "BatBadBut" class of bug) — a prompt on the
  command line would be an injection waiting to happen. On stdin it is data.
* **Headless sessions.** Each workspace keeps a Claude session id. `send` runs
  `claude -p --resume <id>` so a conversation continues across commands instead
  of restarting, and `read` returns the transcript this agent captured. No TUI
  scraping, no keystroke injection.

Usage (see install.ps1, which wraps all of it):
    python owner_os_agent.py enroll --server https://owneros.example --code OOS-...
    python owner_os_agent.py add-workspace --id gaika-basket --path "C:\\...\\ext"
    python owner_os_agent.py run
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

AGENT_VERSION = "0.1.0"
USER_AGENT = f"owner-os-windows-agent/{AGENT_VERSION}"

# Must match core.windows_bridge on the server.
MAX_TEXT_BYTES = 16384
MAX_LINES = 2000
DEFAULT_LINES = 200
POLL_WAIT_SECS = 25          # long-poll hold time asked of the server
MAX_BUFFER_BYTES = 512 * 1024   # per-workspace transcript ring buffer
CLAUDE_TIMEOUT_SECS = int(os.getenv("OOS_CLAUDE_TIMEOUT_SECS", "1800"))
HTTP_TIMEOUT_SECS = POLL_WAIT_SECS + 20

_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AgentError(Exception):
    """A refusal this agent can explain — never a stack trace to the server."""


# ── config ──────────────────────────────────────────────────────────────────

def default_config_path() -> str:
    """%ProgramData%\\OwnerOS\\agent.json on Windows, ~/.owner-os/agent.json
    elsewhere (which is what the tests use)."""
    override = os.getenv("OOS_CONFIG")
    if override:
        return override
    if os.name == "nt":
        base = os.getenv("ProgramData") or os.path.expanduser("~")
        return os.path.join(base, "OwnerOS", "agent.json")
    return os.path.join(os.path.expanduser("~"), ".owner-os", "agent.json")


class Config:
    """The device's identity + its enrolled workspaces. Holds the device secret,
    so it is written 0600 and never printed."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, Any] = {"server": "", "device_id": "", "secret": "",
                                     "workspaces": [], "claude_cmd": ""}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass                      # Windows ACLs are set by install.ps1
        os.replace(tmp, self.path)

    # -- accessors -----------------------------------------------------------
    @property
    def server(self) -> str:
        return (self.data.get("server") or "").rstrip("/")

    @property
    def device_id(self) -> str:
        return self.data.get("device_id") or ""

    @property
    def secret(self) -> str:
        return self.data.get("secret") or ""

    @property
    def workspaces(self) -> list:
        return self.data.get("workspaces") or []

    def workspace(self, workspace_id: str) -> Optional[dict]:
        for w in self.workspaces:
            if w.get("id") == workspace_id:
                return w
        return None

    def enrolled(self) -> bool:
        return bool(self.server and self.device_id and self.secret)

    def redacted(self) -> dict:
        d = dict(self.data)
        d["secret"] = "***set***" if d.get("secret") else ""
        return d


# ── transport ───────────────────────────────────────────────────────────────

def _sign(secret: str, device_id: str, ts: str, nonce: str, path: str,
          body: bytes) -> str:
    """Identical canonical string to core.windows_bridge.canonical_request."""
    body_hash = hashlib.sha256(body or b"").hexdigest()
    canonical = "\n".join(["oos-win-v1", device_id, ts, nonce, path, body_hash])
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _api_path(server: str, route: str) -> str:
    """The path component the signature covers — it must match what the server
    sees in request.url.path, prefix included."""
    from urllib.parse import urlsplit
    base = urlsplit(server).path.rstrip("/")
    return f"{base}/api/v1{route}"


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post(self, route: str, payload: dict, *, signed: bool = True,
              timeout: float = HTTP_TIMEOUT_SECS) -> dict:
        if not self.cfg.server:
            raise AgentError("not enrolled: no server configured")
        body = json.dumps(payload).encode()
        path = _api_path(self.cfg.server, route)
        url = self.cfg.server + f"/api/v1{route}"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if signed:
            if not self.cfg.enrolled():
                raise AgentError("not enrolled: run `enroll` first")
            ts = str(int(time.time()))
            nonce = secrets.token_urlsafe(16)
            headers.update({
                "X-OOS-Device": self.cfg.device_id,
                "X-OOS-Timestamp": ts,
                "X-OOS-Nonce": nonce,
                "X-OOS-Signature": _sign(self.cfg.secret, self.cfg.device_id, ts,
                                         nonce, path, body),
            })
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"")[:300].decode("utf-8", "replace")
            raise AgentError(f"HTTP {e.code} from {route}: {detail}")
        except urllib.error.URLError as e:
            raise AgentError(f"cannot reach {self.cfg.server}: {e.reason}")

    # -- verbs ---------------------------------------------------------------
    def enroll(self, code: str, device_name: str) -> dict:
        return self._post("/windows/enroll", {
            "code": code, "device_name": device_name,
            "os_version": f"{platform.system()} {platform.release()}",
            "agent_version": AGENT_VERSION}, signed=False, timeout=30)

    def poll(self, workspaces: list, wait: float = POLL_WAIT_SECS) -> dict:
        return self._post("/windows/poll", {"workspaces": workspaces, "wait": wait,
                                            "agent_version": AGENT_VERSION})

    def result(self, command_id: str, ok: bool, result: Any = None,
               error: str = "") -> dict:
        return self._post("/windows/result", {"command_id": command_id, "ok": ok,
                                              "result": result, "error": error},
                          timeout=30)

    def rotate(self) -> dict:
        return self._post("/windows/rotate", {}, timeout=30)


# ── Claude session runner ───────────────────────────────────────────────────

def find_claude(cfg: Config) -> str:
    """Resolve the Claude Code executable ONCE, to a full path. On Windows this
    is claude.cmd; running it by full path with an argv list (and the prompt on
    stdin) is what keeps cmd.exe out of the data path."""
    explicit = cfg.data.get("claude_cmd") or os.getenv("OOS_CLAUDE_CMD")
    if explicit:
        if not os.path.exists(explicit) and not shutil.which(explicit):
            raise AgentError(f"configured claude_cmd not found: {explicit}")
        return explicit if os.path.exists(explicit) else shutil.which(explicit)
    found = shutil.which("claude")
    if not found:
        raise AgentError("claude CLI not found on PATH (install Claude Code first)")
    return found


class WorkspaceRunner:
    """One enrolled folder's Claude session: its transcript, its session id and
    at most one running Claude process. Serialized by a lock, so two commands
    can never drive the same folder at once."""

    def __init__(self, workspace_id: str, path: str, state_dir: str, claude_cmd: str):
        self.workspace_id = workspace_id
        self.path = path
        self.state_dir = state_dir
        self.claude_cmd = claude_cmd
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.buffer = bytearray()
        self.last_activity = ""
        self.last_error = ""
        os.makedirs(state_dir, exist_ok=True)
        self.session_file = os.path.join(state_dir, f"{workspace_id}.session")
        self.transcript_file = os.path.join(state_dir, f"{workspace_id}.log")

    # -- session id ----------------------------------------------------------
    @property
    def session_id(self) -> str:
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @session_id.setter
    def session_id(self, value: str) -> None:
        with open(self.session_file, "w", encoding="utf-8") as f:
            f.write((value or "").strip())

    # -- transcript ----------------------------------------------------------
    def _append(self, text: str) -> None:
        self.buffer.extend(text.encode("utf-8", "replace"))
        if len(self.buffer) > MAX_BUFFER_BYTES:
            del self.buffer[:len(self.buffer) - MAX_BUFFER_BYTES]
        self.last_activity = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass                       # a full disk must not kill the agent

    def tail(self, lines: int = DEFAULT_LINES) -> str:
        lines = max(1, min(int(lines or DEFAULT_LINES), MAX_LINES))
        text = self.buffer.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # -- verbs ---------------------------------------------------------------
    def status(self) -> dict:
        return {"workspace_id": self.workspace_id, "path": self.path,
                "state": "working" if self.running else
                         ("error" if self.last_error else "idle"),
                "running": self.running,
                "pid": self.proc.pid if self.running and self.proc else None,
                "session_id": self.session_id,
                "last_activity": self.last_activity,
                "last_error": self.last_error[:300],
                "transcript_bytes": len(self.buffer)}

    def read(self, lines: int = DEFAULT_LINES) -> dict:
        return {**self.status(), "lines": lines, "output": self.tail(lines)}

    def send(self, text: str, *, resume: bool = True) -> dict:
        """One Claude turn, headless. `resume=False` starts a new session; the
        default continues the one this workspace already has, which is what
        makes 'send' feel like talking to a live agent rather than to a series
        of strangers."""
        if not text.strip():
            raise AgentError("empty prompt")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise AgentError(f"prompt exceeds {MAX_TEXT_BYTES} bytes")
        if not self.lock.acquire(blocking=False):
            raise AgentError("workspace busy: a Claude turn is already running")
        try:
            if self.running:
                raise AgentError("workspace busy: a Claude turn is already running")
            argv = [self.claude_cmd, "-p", "--output-format", "json"]
            sid = self.session_id
            if resume and sid:
                argv += ["--resume", sid]
            # The prompt goes on stdin, NEVER in argv — see the module docstring.
            self._append(f"\n>>> [{time.strftime('%H:%M:%S')}] {text}\n")
            self.proc = subprocess.Popen(
                argv, cwd=self.path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False, text=True,
                encoding="utf-8", errors="replace")
            try:
                out, err = self.proc.communicate(text, timeout=CLAUDE_TIMEOUT_SECS)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                out, err = self.proc.communicate()
                self.last_error = f"claude timed out after {CLAUDE_TIMEOUT_SECS}s"
                self._append(f"\n[{self.last_error}]\n")
                raise AgentError(self.last_error)
            code = self.proc.returncode
            self.proc = None
            envelope = {}
            try:
                envelope = json.loads(out) if (out or "").strip() else {}
            except json.JSONDecodeError:
                envelope = {}
            if isinstance(envelope, dict) and envelope.get("session_id"):
                self.session_id = str(envelope["session_id"])
            reply = ""
            if isinstance(envelope, dict):
                reply = str(envelope.get("result") or "")
            if not reply:
                reply = (out or "").strip()
            self._append(reply + "\n")
            if code != 0:
                self.last_error = (err or "claude exited non-zero")[:300]
                self._append(f"\n[claude exit {code}: {self.last_error}]\n")
                raise AgentError(f"claude exited {code}: {self.last_error}")
            self.last_error = ""
            return {"workspace_id": self.workspace_id, "session_id": self.session_id,
                    "reply": reply[:MAX_TEXT_BYTES], "exit_code": code,
                    "resumed": bool(resume and sid)}
        finally:
            self.lock.release()

    def stop(self) -> dict:
        """Terminate the running turn. Idempotent: stopping an idle workspace is
        a no-op, not an error."""
        if not self.running:
            return {"workspace_id": self.workspace_id, "stopped": False,
                    "reason": "no running claude turn"}
        proc = self.proc
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.proc = None
        self._append("\n[stopped by owner]\n")
        return {"workspace_id": self.workspace_id, "stopped": True}


# ── the agent loop ──────────────────────────────────────────────────────────

class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = Client(cfg)
        self.state_dir = os.path.join(os.path.dirname(cfg.path), "state")
        self.runners: dict[str, WorkspaceRunner] = {}
        self._claude_cmd = ""

    def claude_cmd(self) -> str:
        if not self._claude_cmd:
            self._claude_cmd = find_claude(self.cfg)
        return self._claude_cmd

    def runner(self, workspace_id: str) -> WorkspaceRunner:
        """Resolve a workspace ID against LOCAL config. This is the whole
        anti-traversal story: the server names an id, and an id that is not in
        this file resolves to nothing at all."""
        if not _WORKSPACE_RE.match(workspace_id or ""):
            raise AgentError(f"bad workspace id {workspace_id!r}")
        entry = self.cfg.workspace(workspace_id)
        if not entry:
            raise AgentError(f"workspace {workspace_id!r} is not enrolled on this device")
        path = entry.get("path") or ""
        if not os.path.isdir(path):
            raise AgentError(f"workspace {workspace_id!r} path no longer exists")
        if workspace_id not in self.runners:
            self.runners[workspace_id] = WorkspaceRunner(
                workspace_id, path, self.state_dir, self.claude_cmd())
        return self.runners[workspace_id]

    def workspace_report(self) -> list:
        out = []
        for w in self.cfg.workspaces:
            wid = w.get("id") or ""
            state = "unknown"
            session_id = ""
            if wid in self.runners:
                st = self.runners[wid].status()
                state, session_id = st["state"], st["session_id"]
            elif not os.path.isdir(w.get("path") or ""):
                state = "error"
            else:
                state = "idle"
            out.append({"workspace_id": wid, "label": w.get("label") or "",
                        "path_hint": w.get("path") or "", "state": state,
                        "session_id": session_id})
        return out

    def execute(self, cmd: dict) -> dict:
        """Dispatch ONE allowlisted command. The mapping below is exhaustive —
        an action this agent does not know is refused, never guessed at."""
        action = cmd.get("action") or ""
        params = cmd.get("params") or {}
        workspace_id = cmd.get("workspace_id") or ""
        if action == "workspace.list":
            return {"workspaces": self.workspace_report()}
        runner = self.runner(workspace_id)
        if action == "agent.status":
            return runner.status()
        if action == "agent.read":
            return runner.read(params.get("lines") or DEFAULT_LINES)
        if action == "agent.send":
            return runner.send(str(params.get("text") or ""), resume=True)
        if action == "agent.start":
            default_prompt = ("You are being started by Owner OS. "
                              "Summarise this workspace briefly.")
            text = str(params.get("text") or "").strip() or default_prompt
            return runner.send(text, resume=False)
        if action == "agent.stop":
            if not params.get("confirm"):
                raise AgentError("agent.stop requires confirm=true")
            return runner.stop()
        raise AgentError(f"unsupported action {action!r}")

    def handle(self, cmd: dict) -> None:
        command_id = cmd.get("command_id") or ""
        try:
            result = self.execute(cmd)
            self.client.result(command_id, True, result)
        except AgentError as e:
            self.client.result(command_id, False, None, str(e)[:300])
        except Exception as e:  # noqa: BLE001 — one bad command never kills the loop
            self.client.result(command_id, False, None,
                               f"{type(e).__name__}: {str(e)[:200]}")

    def run(self, *, once: bool = False, log=print) -> None:
        """Poll until stopped. Every failure is a backoff, never an exit: an
        agent that dies on a network blip is an agent the owner has to go and
        restart by hand on a machine he is not sitting at."""
        if not self.cfg.enrolled():
            raise AgentError("not enrolled: run `enroll` first")
        backoff = 1.0
        log(f"owner-os agent {AGENT_VERSION} -> {self.cfg.server} "
            f"as {self.cfg.device_id} ({len(self.cfg.workspaces)} workspace(s))")
        while True:
            try:
                answer = self.client.poll(self.workspace_report())
                backoff = 1.0
                for cmd in answer.get("commands") or []:
                    log(f"command {cmd.get('action')} "
                        f"{cmd.get('workspace_id') or '-'} {cmd.get('command_id')}")
                    self.handle(cmd)
            except AgentError as e:
                log(f"poll failed: {e}; retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except KeyboardInterrupt:
                log("stopped")
                return
            if once:
                return


# ── CLI ─────────────────────────────────────────────────────────────────────

def cmd_enroll(args) -> int:
    cfg = Config(args.config or default_config_path())
    cfg.data["server"] = (args.server or cfg.server).rstrip("/")
    if not cfg.data["server"]:
        raise AgentError("--server is required for the first enrollment")
    name = args.name or platform.node() or "windows-pc"
    out = Client(cfg).enroll(args.code, name)
    cfg.data["device_id"] = out["device_id"]
    cfg.data["secret"] = out["secret"]
    cfg.save()
    print(f"enrolled as {out['device_id']} (config: {cfg.path})")
    return 0


def cmd_add_workspace(args) -> int:
    cfg = Config(args.config or default_config_path())
    wid = (args.id or "").strip().lower()
    if not _WORKSPACE_RE.match(wid):
        raise AgentError("--id must be [a-z0-9][a-z0-9_-]{0,63}")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(args.path)))
    if not os.path.isdir(path):
        raise AgentError(f"not a directory: {path}")
    workspaces = [w for w in cfg.workspaces if w.get("id") != wid]
    workspaces.append({"id": wid, "path": path, "label": args.label or ""})
    cfg.data["workspaces"] = workspaces
    cfg.save()
    print(f"workspace {wid} -> {path}")
    return 0


def cmd_remove_workspace(args) -> int:
    cfg = Config(args.config or default_config_path())
    before = len(cfg.workspaces)
    cfg.data["workspaces"] = [w for w in cfg.workspaces if w.get("id") != args.id]
    cfg.save()
    print(f"removed {before - len(cfg.workspaces)} workspace(s)")
    return 0


def cmd_status(args) -> int:
    cfg = Config(args.config or default_config_path())
    print(json.dumps({"config": cfg.path, **cfg.redacted(),
                      "enrolled": cfg.enrolled()}, indent=2))
    return 0


def cmd_rotate(args) -> int:
    cfg = Config(args.config or default_config_path())
    out = Client(cfg).rotate()
    cfg.data["secret"] = out["secret"]
    cfg.save()
    print("device secret rotated")
    return 0


def cmd_run(args) -> int:
    cfg = Config(args.config or default_config_path())
    Agent(cfg).run(once=args.once)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="owner_os_agent",
                                description="Owner OS Windows agent")
    p.add_argument("--config", default="", help="config file (default: ProgramData)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="exchange a one-time code for a device identity")
    e.add_argument("--server", default="", help="https://owner-os.example")
    e.add_argument("--code", required=True)
    e.add_argument("--name", default="")
    e.set_defaults(func=cmd_enroll)

    a = sub.add_parser("add-workspace", help="enroll a local folder (explicit, local-only)")
    a.add_argument("--id", required=True)
    a.add_argument("--path", required=True)
    a.add_argument("--label", default="")
    a.set_defaults(func=cmd_add_workspace)

    r = sub.add_parser("remove-workspace")
    r.add_argument("--id", required=True)
    r.set_defaults(func=cmd_remove_workspace)

    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)
    ro = sub.add_parser("rotate", help="rotate this device's secret")
    ro.set_defaults(func=cmd_rotate)

    run = sub.add_parser("run", help="connect and serve commands (outbound only)")
    run.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    run.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AgentError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
