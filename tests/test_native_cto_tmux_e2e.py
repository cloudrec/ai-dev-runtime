"""End-to-end over the REAL agent_control tmux transport, isolated from every real agent:
a private tmux server (own TMUX_TMPDIR, $TMUX unset), private agent_control DB/audit and
control-plane ledger, and a stand-in Claude pane (tests/stub_claude_agent.py).

Two separate panes (independent oversight, no self-review): a CONTROLLER (the CTO session)
and a WORKER. worker event -> controller tick -> request typed into the controller only ->
the controller runs the reply command -> decision -> advice typed into the worker only ->
the worker applies it (file changes) -> the worker's next event carries the outcome.
Only the native session listing is synthetic."""
from __future__ import annotations

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
CTL = "cto-controller:0.0"
WORKER = "worker-a:0.0"

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def _private_tmux(*args, env):
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True, timeout=15)


@pytest.fixture
def stub_pane(tmp_path, monkeypatch):
    sock_dir = tmp_path / "tmux"
    sock_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TMUX_TMPDIR"] = str(sock_dir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(sock_dir))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    for k in ("NATIVE_GOALS_ENABLED", "OPENAI_API_KEY", "OWNER_OS_CTO_DENY_AGENTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NATIVE_OWNER_OS_AGENTS", "worker-a")
    monkeypatch.setenv("OWNER_OS_CTO_PROVIDER", "claude_session")
    monkeypatch.setenv("OWNER_OS_CTO_CONTROLLER", CTL)
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    panes = {}
    for name in ("cto-controller", "worker-a"):
        d = tmp_path / name
        d.mkdir()
        notes, log = d / "notes.txt", d / "stub.log"
        notes.write_text("step 6 done\n")
        cmd = (f"cd {d} && exec -a claude {sys.executable} "
               f"{ROOT / 'tests' / 'stub_claude_agent.py'} {notes} {log}")
        r = _private_tmux("new-session", "-d", "-s", name, "-x", "200", "-y", "50",
                          "bash", "-c", cmd, env=env)
        assert r.returncode == 0, r.stderr
        panes[name] = {"notes": notes, "log": log}
    try:
        # isolation guard: this server must hold our two stubs and nothing else
        ls = _private_tmux("list-sessions", "-F", "#{session_name}", env=env)
        assert sorted(ls.stdout.split()) == ["cto-controller", "worker-a"], ls.stdout
        assert str(sock_dir) in _private_tmux("display", "-p", "#{socket_path}",
                                              env=env).stdout
        deadline = time.time() + 10
        while time.time() < deadline and len([
                a for a in ac.agent_list()["agents"]
                if a["target"] in (CTL, WORKER) and a.get("alive")]) < 2:
            time.sleep(0.2)
        yield panes
    finally:
        _private_tmux("kill-server", env=env)


LISTING = {"source": "claude_agents_json", "sessions": [
    {"name": n, "kind": "interactive", "cwd": f"/w/{n}", "session_id": n, "state": "idle",
     "pid": i + 1, "tmux_target": t} for i, (n, t) in enumerate(
        (("cto-controller", CTL), ("worker-a", WORKER)))]}


def wait_for(pred, secs=20):
    deadline = time.time() + secs
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.25)
    return False


def stub_log(pane):
    return [json.loads(x) for x in pane["log"].read_text().splitlines()] \
        if pane["log"].exists() else []


def q(sql):
    conn, _ = _c(None)
    cb._schema(conn)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def kinds(pane):
    return [e["kind"] for e in stub_log(pane)]


def test_worker_event_controller_reply_worker_result_over_real_tmux(stub_pane):
    ctl, worker = stub_pane["cto-controller"], stub_pane["worker-a"]
    no.run_once(listing=LISTING)                               # cursor -> bus head
    eid = cto.emit("work_evidence", "milestone_review", project_id="owner-os",
                   agent_id=WORKER, severity="info", owner_action_required=False,
                   payload={"question": "checklist done: what next?"}, push=False,
                   dedup_window_secs=0)["event_id"]
    out = no.run_once(listing=LISTING)                          # request -> controller
    assert [d["result"] for d in out["delivered"]] == ["sent"], out
    assert wait_for(lambda: "request" in kinds(ctl))
    assert stub_log(ctl)[0]["reply_rc"] == 0, stub_log(ctl)
    assert q("SELECT status FROM cto_decision") == [("decided",)]
    out = no.run_once(listing=LISTING)                          # advice -> worker
    assert [d["result"] for d in out["delivered"]] == ["sent"], out
    assert wait_for(lambda: "step 7 reviewed" in worker["notes"].read_text())
    assert worker["notes"].read_text() == "step 6 done\nstep 7 reviewed\n"
    assert ctl["notes"].read_text() == "step 6 done\n"          # controller never "works"
    no.run_once(listing=LISTING)
    no.run_once(listing=LISTING)
    time.sleep(1.5)
    assert kinds(ctl) == ["request"] and kinds(worker) == ["advice"]   # exactly once, apart
    sent = sqlite_rows(os.environ["AGENT_CONTROL_DB"],
                       "SELECT idempotency_key,target FROM deliveries ORDER BY rowid")
    assert sent == [(f"ctoreq:cp:{eid}:1", CTL), (f"cto:cp:{eid}", WORKER)]
    # feedback / auto-continue: the worker's next event -> new request with the outcome
    cto.emit("work_evidence", "milestone_review", project_id="owner-os", agent_id=WORKER,
             severity="info", owner_action_required=False,
             payload={"question": "step 7 reviewed"}, push=False, dedup_window_secs=0)
    no.run_once(listing=LISTING)
    assert wait_for(lambda: kinds(ctl).count("request") == 2)
    ctx = json.loads(q("SELECT context FROM cto_decision ORDER BY latched_ts DESC")[0][0])
    assert ctx["previous_advice"][0]["delivery"] == "sent"
    # the controller's own events are never reviewed
    cto.emit("work_evidence", "repeated_failure", project_id="owner-os", agent_id=CTL,
             severity="info", owner_action_required=False, payload={"question": "x"},
             push=False, dedup_window_secs=0)
    no.run_once(listing=LISTING)
    time.sleep(1.0)
    assert kinds(ctl).count("request") == 2


def sqlite_rows(path, sql):
    import sqlite3
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()
