"""End-to-end over the REAL agent_control tmux transport, isolated from every real agent:
a private tmux server (own TMUX_TMPDIR, $TMUX unset), private agent_control DB/audit and
control-plane ledger, and a stand-in Claude pane (tests/stub_claude_agent.py).

event -> controller tick -> CTO request typed into the canary pane -> the pane runs the
reply command -> decision -> advice typed -> the pane applies it (file changes) -> the next
event carries the outcome. Only the native session listing is synthetic."""
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
TARGET = "aes-canary:0.0"

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
    monkeypatch.setenv("NATIVE_OWNER_OS_AGENTS", "aes-canary")
    monkeypatch.setenv("OWNER_OS_CTO_PROVIDER", "claude_session")
    monkeypatch.setenv("OWNER_OS_CTO_CONTROLLER", TARGET)
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    notes, log = tmp_path / "notes.txt", tmp_path / "stub.log"
    notes.write_text("step 6 done\n")
    cmd = (f"cd {tmp_path} && exec -a claude {sys.executable} "
           f"{ROOT / 'tests' / 'stub_claude_agent.py'} {notes} {log}")
    r = _private_tmux("new-session", "-d", "-s", "aes-canary", "-x", "200", "-y", "50",
                      "bash", "-c", cmd, env=env)
    assert r.returncode == 0, r.stderr
    try:
        # isolation guard: this server must hold our stub and nothing else
        ls = _private_tmux("list-sessions", "-F", "#{session_name}", env=env)
        assert ls.stdout.split() == ["aes-canary"], ls.stdout
        assert str(sock_dir) in _private_tmux("display", "-p", "#{socket_path}",
                                              env=env).stdout
        deadline = time.time() + 10
        while time.time() < deadline and not any(
                a["target"] == TARGET and a.get("alive")
                for a in ac.agent_list()["agents"]):
            time.sleep(0.2)
        yield {"notes": notes, "log": log, "env": env}
    finally:
        _private_tmux("kill-server", env=env)


LISTING = {"source": "claude_agents_json", "sessions": [
    {"name": "aes-canary-work", "kind": "interactive", "cwd": "/w", "session_id": "s",
     "state": "idle", "pid": 1, "tmux_target": TARGET}]}


def wait_for(pred, secs=20):
    deadline = time.time() + secs
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.25)
    return False


def stub_log(sp):
    return [json.loads(x) for x in sp["log"].read_text().splitlines()] \
        if sp["log"].exists() else []


def q(sql):
    conn, _ = _c(None)
    cb._schema(conn)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_event_request_reply_advice_result_and_next_event_over_real_tmux(stub_pane):
    no.run_once(listing=LISTING)                               # cursor -> bus head
    eid = cto.emit("work_evidence", "milestone_review", project_id="owner-os",
                   agent_id=TARGET, severity="info", owner_action_required=False,
                   payload={"question": "checklist done: what next?"}, push=False,
                   dedup_window_secs=0)["event_id"]
    out = no.run_once(listing=LISTING)                          # request typed (real tmux)
    assert [d["result"] for d in out["delivered"]] == ["sent"], out
    assert wait_for(lambda: any(e["kind"] == "request" for e in stub_log(stub_pane)))
    req = [e for e in stub_log(stub_pane) if e["kind"] == "request"][0]
    assert req["reply_rc"] == 0, req
    assert q("SELECT status FROM cto_decision") == [("decided",)]
    out = no.run_once(listing=LISTING)                          # advice typed (real tmux)
    assert [d["result"] for d in out["delivered"]] == ["sent"], out
    assert wait_for(lambda: "step 7 reviewed" in stub_pane["notes"].read_text())
    assert stub_pane["notes"].read_text() == "step 6 done\nstep 7 reviewed\n"
    # exactly-once: more ticks type nothing more
    no.run_once(listing=LISTING)
    no.run_once(listing=LISTING)
    time.sleep(1.5)
    kinds = [e["kind"] for e in stub_log(stub_pane)]
    assert kinds == ["request", "advice"], kinds
    sent = sqlite_rows(os.environ["AGENT_CONTROL_DB"],
                       "SELECT idempotency_key,target FROM deliveries ORDER BY rowid")
    assert sent == [(f"ctoreq:cp:{eid}:1", TARGET), (f"cto:cp:{eid}", TARGET)]
    assert q("SELECT msg_key,state FROM native_outbox ORDER BY created_at") == [
        (f"ctoreq:cp:{eid}:1", "sent"), (f"cto:cp:{eid}", "sent")]
    # auto-continue: the next event's request carries the delivered outcome
    cto.emit("work_evidence", "milestone_review", project_id="owner-os", agent_id=TARGET,
             severity="info", owner_action_required=False,
             payload={"question": "step 7 reviewed"}, push=False, dedup_window_secs=0)
    no.run_once(listing=LISTING)
    assert wait_for(lambda: [e["kind"] for e in stub_log(stub_pane)].count("request") == 2)
    ctx = json.loads(q("SELECT context FROM cto_decision ORDER BY latched_ts DESC")[0][0])
    assert ctx["previous_advice"][0]["delivery"] == "sent"


def sqlite_rows(path, sql):
    import sqlite3
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()
