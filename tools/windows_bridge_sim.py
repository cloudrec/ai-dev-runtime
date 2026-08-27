"""End-to-end simulation of the Windows bridge, on one machine.

Proves the whole path with nothing stubbed in the middle: a real HTTP server
carrying the real /api/v1 routes, the real Windows client from
clients/windows/owner_os_agent.py talking to it over a socket, and a fake
`claude` executable standing in for Claude Code so the run costs nothing and
needs no provider.

    venv/bin/python tools/windows_bridge_sim.py

Steps, in order — each one prints and each one is asserted:
    1. start the API on a free localhost port with throwaway databases
    2. owner mints a one-time enrollment code (bearer-authenticated)
    3. the device enrolls with that code and gets its own secret
    4. the owner enrolls a workspace LOCALLY on the device
    5. the device starts its outbound poll loop
    6. owner sends a prompt through /windows/command and gets Claude's reply
    7. owner reads the transcript back
    8. the same command id, replayed, does not run the work twice
    9. an un-enrolled workspace is refused
   10. a revoked device can no longer authenticate

Nothing here touches the live databases, the live service, or the network
beyond 127.0.0.1. tests/test_windows_e2e.py runs exactly this.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "clients", "windows"))

TOKEN = "sim-token-not-a-real-secret"

FAKE_CLAUDE = """#!{python}
import json, os, sys
prompt = sys.stdin.read()
print(json.dumps({{"session_id": "sim-session-1",
                  "result": "handled: " + prompt.strip()[:80]}}))
"""

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, payload: dict, token: str = TOKEN) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def _wait_for_server(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/v1/health", timeout=2).read()
            return
        except urllib.error.HTTPError:
            return                      # answering at all is enough
        except Exception:               # noqa: BLE001 — not up yet
            time.sleep(0.2)
    raise RuntimeError("server did not start in time")


def _start_server(port: int, env: dict):
    """Serve the REAL /api/v1 router on a loopback port, in a thread.

    A thread rather than a second process on purpose: this host runs a dozen
    live services and is short on memory, and an extra interpreter is what got
    an earlier version of this simulation OOM-killed. The env below is applied
    BEFORE api.v1 is imported, because RUNTIME_TOKEN is read into a module
    global at import time."""
    import uvicorn
    os.environ.update(env)
    from fastapi import FastAPI

    from api import v1
    # RUNTIME_TOKEN is captured into a module global at import time, so when
    # this simulation runs inside a pytest process that already imported the
    # API, setting the env var alone would not be enough — the running module
    # has to be pointed at the simulation's token explicitly.
    v1._TOKEN = env["RUNTIME_TOKEN"]
    from core import job_store
    job_store.init_db()          # /health reads it; a fresh temp DB has no tables yet
    app = FastAPI()
    router = v1.router
    app.include_router(router)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def run(verbose: bool = True) -> dict:
    log = (lambda m: print(m, flush=True)) if verbose else (lambda m: None)
    # The simulation repoints database env vars at its own temp directory. That
    # is fine for a standalone run and NOT fine inside a pytest process, whose
    # conftest already pointed them at the test databases — so the previous
    # values are restored in the finally block below.
    saved_env = dict(os.environ)
    saved_token = None
    tmp = tempfile.mkdtemp(prefix="win-bridge-sim-")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    results: dict = {"tmp": tmp, "base": base}

    # -- fake claude + a workspace to point it at ----------------------------
    claude_path = os.path.join(tmp, "claude")
    with open(claude_path, "w", encoding="utf-8") as f:
        f.write(FAKE_CLAUDE.format(python=sys.executable))
    os.chmod(claude_path, 0o755)
    workspace = os.path.join(tmp, "gaika-basket-extension")
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{}")

    env = {
        "RUNTIME_TOKEN": TOKEN,
        "CONTROL_PLANE_DB": os.path.join(tmp, "control_plane.db"),
        "RUNTIME_DB": os.path.join(tmp, "runtime_jobs.db"),
        "AGENT_CONTROL_DB": os.path.join(tmp, "agent_control.db"),
        "PYTHONPATH": REPO,
    }

    log(f"[1] starting API on {base} (databases under {tmp})")
    if "api.v1" in sys.modules:
        saved_token = sys.modules["api.v1"]._TOKEN
    server, _server_thread = _start_server(port, env)
    agent_thread = None
    try:
        _wait_for_server(base)
        log("    server is up")

        log("[2] owner mints a one-time enrollment code")
        code = _post(f"{base}/api/v1/windows/enroll-code",
                     {"label": "owner windows pc"})["code"]
        assert code.startswith("OOS-"), code
        log(f"    code: {code[:8]}… (single use, expires)")

        log("[3] device enrolls")
        import owner_os_agent as agent_mod
        cfg_path = os.path.join(tmp, "device", "agent.json")
        cfg = agent_mod.Config(cfg_path)
        cfg.data["server"] = base
        cfg.data["claude_cmd"] = claude_path
        enrolled = agent_mod.Client(cfg).enroll(code, "SIM-PC")
        cfg.data["device_id"] = enrolled["device_id"]
        cfg.data["secret"] = enrolled["secret"]
        cfg.save()
        device_id = enrolled["device_id"]
        log(f"    device_id: {device_id}")
        results["device_id"] = device_id

        log("[4] owner enrols the workspace locally on the device")
        assert agent_mod.main(["--config", cfg_path, "add-workspace",
                               "--id", "gaika-basket", "--path", workspace]) == 0
        cfg = agent_mod.Config(cfg_path)

        log("[5] device starts its outbound poll loop")
        device_agent = agent_mod.Agent(cfg)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                try:
                    device_agent.run(once=True, log=lambda *_a: None)
                except Exception:  # noqa: BLE001 — the sim's loop must not die
                    time.sleep(0.2)

        agent_thread = threading.Thread(target=loop, daemon=True)
        agent_thread.start()
        # The first poll registers the workspace with the server.
        deadline = time.time() + 30
        while time.time() < deadline:
            devices = json.loads(urllib.request.urlopen(urllib.request.Request(
                f"{base}/api/v1/windows/workspaces?device_id={device_id}",
                headers={"Authorization": f"Bearer {TOKEN}"}), timeout=10).read())
            if devices.get("workspaces"):
                break
            time.sleep(0.3)
        assert devices["workspaces"], "device never reported its workspaces"
        log(f"    workspace registered: {devices['workspaces'][0]['workspace_id']}")

        log("[6] owner sends a prompt to the Windows workspace")
        sent = _post(f"{base}/api/v1/windows/command", {
            "device_id": device_id, "action": "agent.send",
            "workspace_id": "gaika-basket",
            "params": {"text": "add a badge to the cart button"},
            "wait_secs": 60})
        assert sent["ok"] is True, sent
        assert sent["result"]["reply"].startswith("handled:"), sent
        log(f"    claude replied: {sent['result']['reply']}")
        results["reply"] = sent["result"]["reply"]

        log("[7] owner reads the transcript back")
        read = _post(f"{base}/api/v1/windows/command", {
            "device_id": device_id, "action": "agent.read",
            "workspace_id": "gaika-basket", "params": {"lines": 50},
            "wait_secs": 60})
        assert read["ok"] is True, read
        assert "badge to the cart button" in read["result"]["output"], read
        assert read["result"]["session_id"] == "sim-session-1"
        log("    transcript contains the prompt and the reply")

        log("[8] replaying the same command id does not run the work twice")
        cid = "11111111-2222-3333-4444-555555555555"
        first = _post(f"{base}/api/v1/windows/command", {
            "device_id": device_id, "action": "agent.send",
            "workspace_id": "gaika-basket", "params": {"text": "only once"},
            "command_id": cid, "wait_secs": 60})
        again = _post(f"{base}/api/v1/windows/command", {
            "device_id": device_id, "action": "agent.send",
            "workspace_id": "gaika-basket", "params": {"text": "only once"},
            "command_id": cid, "wait_secs": 5})
        assert first["result"] == again["result"], (first, again)
        log("    replay returned the recorded result, not a second run")

        log("[9] an un-enrolled workspace is refused")
        try:
            _post(f"{base}/api/v1/windows/command", {
                "device_id": device_id, "action": "agent.send",
                "workspace_id": "some-other-repo", "params": {"text": "hi"},
                "wait_secs": 5})
            raise AssertionError("un-enrolled workspace was NOT refused")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
            log("    refused with HTTP 400, as designed")

        log("[10] revoking the device ends its access")
        _post(f"{base}/api/v1/windows/devices/{device_id}/revoke", {})
        stop.set()
        try:
            agent_mod.Client(cfg).poll([], wait=0)
            raise AssertionError("a revoked device was still able to poll")
        except agent_mod.AgentError as e:
            assert "401" in str(e), str(e)
            log("    revoked device gets HTTP 401")

        results["ok"] = True
        log("\nSIMULATION PASSED")
        return results
    finally:
        try:
            stop.set()
        except Exception:  # noqa: BLE001
            pass
        if agent_thread:
            agent_thread.join(timeout=5)
        server.should_exit = True
        _server_thread.join(timeout=10)
        os.environ.clear()
        os.environ.update(saved_env)
        if saved_token is not None:
            from api import v1 as _v1
            _v1._TOKEN = saved_token
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    try:
        run()
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"SIMULATION FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
