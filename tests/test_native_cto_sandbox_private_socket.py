"""Sandbox harness routing over TWO tmux servers, both private to this test:
server A (TMUX_TMPDIR, stands in for the default server) holds the controller stand-in;
server B (`tmux -S sock`, the private worker server) holds the worker stand-in.

Proves: the loop crosses servers (request -> controller on A, advice -> worker on B),
default-server discovery never sees the worker, sends outside the two panes are refused
before any tmux call, and the preflight refuses a polluted private server."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

from core import agent_control as ac
from core import cto_bridge as cb
from core import native_owner_os as no
from core.control_plane import cto
from core.control_plane.api import _c

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sbx", ROOT / "tools" / "native_cto_sandbox_run.py")
sbx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbx)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

W_SESS = "owneros-sandbox-worker-20261027"
WORKER, CTL = f"{W_SESS}:0.0", "owner-os-supervisor-clean:0.0"


def tm(*args, env):
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True, timeout=15)


@pytest.fixture
def servers(tmp_path, monkeypatch):
    a_dir = tmp_path / "srvA"
    a_dir.mkdir()
    sock_b = str(tmp_path / "worker.sock")
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TMUX_TMPDIR"] = str(a_dir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(a_dir))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    for k in ("NATIVE_GOALS_ENABLED", "OPENAI_API_KEY", "OWNER_OS_CTO_DENY_AGENTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NATIVE_OWNER_OS_AGENTS", W_SESS)
    monkeypatch.setenv("OWNER_OS_CTO_PROVIDER", "claude_session")
    monkeypatch.setenv("OWNER_OS_CTO_CONTROLLER", CTL)
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    panes = {}
    for name, sock in (("owner-os-supervisor-clean", None), (W_SESS, sock_b)):
        d = tmp_path / name
        d.mkdir()
        (d / "notes.txt").write_text("step 1 done\n")
        cmd = (f"cd {d} && exec -a claude {sys.executable} "
               f"{ROOT / 'tests' / 'stub_claude_agent.py'} {d / 'notes.txt'} {d / 'stub.log'}")
        args = (["-S", sock] if sock else []) + ["new-session", "-d", "-s", name, "-x", "200",
                                                 "-y", "50", "bash", "-c", cmd]
        r = tm(*args, env=env)
        assert r.returncode == 0, r.stderr
        panes[name] = d
    try:
        assert tm("list-sessions", "-F", "#{session_name}", env=env).stdout.split() == [
            "owner-os-supervisor-clean"]
        assert sbx.private_sessions(sock_b) == [W_SESS]
        yield {"env": env, "sock": sock_b, "ctl": panes["owner-os-supervisor-clean"],
               "worker": panes[W_SESS]}
    finally:
        tm("-S", sock_b, "kill-server", env=env)
        tm("kill-server", env=env)


LISTING = {"source": "claude_agents_json", "sessions": [
    {"name": n, "kind": "interactive", "cwd": f"/w/{n}", "session_id": n, "state": "idle",
     "pid": i + 1, "tmux_target": t} for i, (n, t) in enumerate(
        (("sandbox-worker", WORKER), ("controller", CTL)))]}


def wait_for(pred, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.25)
    return False


def kinds(d):
    p = d / "stub.log"
    return [json.loads(x)["kind"] for x in p.read_text().splitlines()] if p.exists() else []


def test_default_server_discovery_never_sees_the_private_worker(servers):
    targets = {a["target"] for a in ac.agent_list()["agents"]}     # unrouted = production view
    assert CTL in targets and WORKER not in targets


def test_loop_crosses_servers_and_stays_inside_the_two_panes(servers, monkeypatch):
    real = ac._tmux
    monkeypatch.setattr(ac, "_tmux", sbx.route_tmux(real, W_SESS, servers["sock"]))
    monkeypatch.setattr(ac, "agent_send", sbx.guard_send({WORKER, CTL}, ac.agent_send))
    assert {WORKER, CTL} <= {a["target"] for a in ac.agent_list()["agents"]}
    no.run_once(listing=LISTING)
    eid = cto.emit("sandbox_harness", "milestone_review", project_id="owneros-sandbox",
                   agent_id=WORKER, severity="info", owner_action_required=False,
                   payload={"question": "next step?"}, push=False,
                   dedup_window_secs=0)["event_id"]
    no.run_once(listing=LISTING)                                  # request -> server A
    assert wait_for(lambda: "request" in kinds(servers["ctl"]))
    no.run_once(listing=LISTING)                                  # advice -> server B
    assert wait_for(lambda: "step 7 reviewed" in (servers["worker"] / "notes.txt").read_text())
    no.run_once(listing=LISTING)
    time.sleep(1.0)
    assert kinds(servers["ctl"]) == ["request"] and kinds(servers["worker"]) == ["advice"]
    assert (servers["ctl"] / "notes.txt").read_text() == "step 1 done\n"
    conn, _ = _c(None)
    cb._schema(conn)
    assert conn.execute("SELECT status FROM cto_decision").fetchall() == [("decided",)]
    conn.close()
    import sqlite3
    c = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    assert c.execute("SELECT idempotency_key,target FROM deliveries ORDER BY rowid").fetchall() \
        == [(f"ctoreq:cp:{eid}:1", CTL), (f"cto:cp:{eid}", WORKER)]
    c.close()
    # negative: anything else is refused before tmux is touched
    for other in ("vtb:0.0", "bitwhite-backend-v3:0.0", "payorch-ha-fresh:0.0",
                  f"{W_SESS}:1.0"):
        with pytest.raises(sbx.Refused):
            ac.agent_send(other, "x", idempotency_key="neg")


def test_hook_spawned_supervisor_is_disabled_by_the_worker_env(servers):
    """Inside the worker pane $TMUX points at the PRIVATE server; a hook-spawned
    native_supervise_once would see the worker. The worker env disables it, and the
    runtime env loader never overrides an explicit value."""
    live = pathlib.Path("/root/ai-dev-runtime")       # the code a worker hook really spawns
    if not (live / "hooks" / "owneros_hook.py").exists():
        pytest.skip("live checkout with hooks not present")
    env = {**servers["env"], "NATIVE_SUPERVISOR_ENABLED": "0",
           "NATIVE_SUPERVISOR_TARGETS": "none"}
    code = ("import sys;sys.path.insert(0,%r);sys.path.insert(0,%r);"
            "from owneros_hook import _load_runtime_env;_load_runtime_env();"
            "from core import native_supervisor as ns;"
            "print(ns.ENABLED, ns._TARGETS_RAW)") % (str(live), str(live / "hooks"))
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.split() == ["False", "none"], out.stderr[-500:]
    # and without the override the same loader would arm it against every target
    env.pop("NATIVE_SUPERVISOR_ENABLED")
    env.pop("NATIVE_SUPERVISOR_TARGETS")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.split()[0] == "True", out.stderr[-500:]


def test_preflight_refuses_a_polluted_private_server(servers):
    tm("-S", servers["sock"], "new-session", "-d", "-s", "intruder", "sleep", "60",
       env=servers["env"])
    assert sorted(sbx.private_sessions(servers["sock"])) == ["intruder", W_SESS]
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "native_cto_sandbox_run.py"),
                        "--worker", WORKER, "--controller", CTL, "--workdir",
                        str(servers["worker"]), "--state-dir", str(servers["worker"] / "st"),
                        "--worker-socket", servers["sock"], "--preflight-only"],
                       env=servers["env"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "must hold exactly" in r.stdout, r.stdout + r.stderr
