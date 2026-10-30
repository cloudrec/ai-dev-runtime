"""Executor contract, hosts (local / SSH / Windows TODO) and the live agent_control
adapter, without a live agent. The SSH test runs the real tmux adapter through a
stand-in `ssh` that executes locally: it proves the argv/quoting contract of the
remote mode, not that any remote machine is reachable."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys

import pytest

from core.fullcycle import executor as ex
from core.fullcycle.hosts import HostError, LocalHost, SshHost, WindowsBridgeHost
from core.fullcycle.tmux_adapter import AgentControlAdapter, parse_result

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures", "fullcycle_demo"))

ENV = ex.TaskEnvelope(task_id="p:a", attempt=2, project_id="p", workdir="/w", goal="g",
                      acceptance_criteria=("ok",), envelope_path="/w/.fullcycle/tasks/a-2.json",
                      result_path="/w/.fullcycle/results/a-2.json")


def test_the_prompt_is_one_line_and_names_both_files():
    p = ENV.prompt()
    assert "\n" not in p and ENV.envelope_path in p and ENV.result_path in p
    assert json.loads(ENV.to_json())["result_format"]["attempt"] == 2


def test_a_result_for_another_attempt_never_closes_this_one():
    h = {"task_id": "p:a", "attempt": 2}
    ok = json.dumps({"task_id": "p:a", "attempt": 2, "status": "done", "summary": "s"})
    assert parse_result(ok, h)["status"] == "done"
    assert parse_result(ok.replace('"attempt": 2', '"attempt": 1'), h) is None
    assert parse_result('{"task_id": "p:a", "attempt": 2, "status": "maybe"}', h) is None
    assert parse_result("not json", h) is None


def test_profiles_claude_tested_qwen_codex_explicit_todo():
    assert ex.profile("claude").tested
    for name in ("qwen", "codex"):
        with pytest.raises(ex.ExecutorUnavailable, match="not tested"):
            ex.profile(name)


def test_ssh_argv_is_batch_mode_without_agent_forwarding_and_quoted():
    h = SshHost("dev@build-1", port=2222, identity_file="/k")
    argv = h.argv(["sh", "-c", "echo 'a b'; rm x"], cwd="/srv/my proj")
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o",
                        "ConnectTimeout=10"]
    assert argv[-2:] == ["dev@build-1",
                         "cd '/srv/my proj' && sh -c 'echo '\"'\"'a b'\"'\"'; rm x'"]
    for bad in ("-oProxyCommand=x", "a b", ""):
        with pytest.raises(HostError):
            SshHost(bad)


def test_windows_bridge_host_refuses_instead_of_pretending():
    assert WindowsBridgeHost("dev1").tested is False
    with pytest.raises(HostError):
        WindowsBridgeHost("dev1").run(["echo"])


@pytest.fixture
def fake_ssh(tmp_path):
    """`ssh [opts] -- DEST CMD` → runs CMD locally with sh -c; records each call."""
    calls = tmp_path / "ssh_calls.log"
    p = tmp_path / "ssh"
    p.write_text("#!/bin/sh\nwhile [ \"$1\" != \"--\" ]; do shift; done; shift\n"
                 f"printf '%s :: %s\\n' \"$1\" \"$2\" >> {calls}\nshift\nexec sh -c \"$1\"\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), calls


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_the_tmux_adapter_runs_over_the_ssh_contract(tmp_path, fake_ssh, monkeypatch):
    import harness as h
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    ssh_bin, calls = fake_ssh
    host = SshHost("dev@remote-builder", ssh_bin=ssh_bin)
    root = h.make_project(str(tmp_path))
    a, log = h.make_adapter(str(tmp_path), root, {}, host=host)
    try:
        env = ex.TaskEnvelope(task_id="tinycalc:add", attempt=1, project_id="tinycalc",
                              workdir=root, goal="add", acceptance_criteria=("ok",),
                              envelope_path=f"{root}/.fullcycle/tasks/add-1.json",
                              result_path=f"{root}/.fullcycle/results/add-1.json")
        r = a.submit(env, "k1")
        assert r["ok"] and not r["already"]
        assert h.wait_for(lambda: a.poll(r["handle"]).state == ex.DONE, 15)
        assert a.collect(r["handle"])["status"] == "done"
        assert a.submit(env, "k1")["already"] is True            # idempotent per key
        assert len(h.agent_log(log)) == 1
        lines = open(calls).read().splitlines()
        assert lines and all(x.startswith("dev@remote-builder :: ") for x in lines)
        assert any(" :: tmux -S " in x for x in lines)           # tmux itself went via ssh
    finally:
        a.kill()


class _FakeAC:
    def __init__(self, send_result=None, status=None, raise_on_send=None):
        self.send_result, self.status = send_result or {}, status or {}
        self.raise_on_send, self.sent = raise_on_send, []

    def agent_send(self, target, text, idempotency_key=None, actor=None, source=None):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append((target, text, idempotency_key, actor))
        return self.send_result

    def agent_status(self, target, actor=None):
        return self.status


def _ac_env(tmp_path):
    return ex.TaskEnvelope(task_id="p:a", attempt=1, project_id="p", workdir=str(tmp_path),
                           goal="g", acceptance_criteria=("ok",),
                           envelope_path=str(tmp_path / "e.json"),
                           result_path=str(tmp_path / "r.json"))


def test_agent_control_adapter_passes_policy_refusals_through(tmp_path):
    ac = _FakeAC({"delivered": False, "refused": "hard_denied_target"})
    r = AgentControlAdapter("payorch:0.0", ac=ac).submit(_ac_env(tmp_path), "k")
    assert r == {**r, "ok": False, "reason": "refused:hard_denied_target"}
    ac = _FakeAC(raise_on_send=RuntimeError("automation_target_not_allowlisted"))
    r = AgentControlAdapter("x:0.0", ac=ac).submit(_ac_env(tmp_path), "k")
    assert not r["ok"] and "not_allowlisted" in r["reason"]


def test_agent_control_adapter_duplicate_key_and_crash(tmp_path):
    ac = _FakeAC({"duplicate": True, "delivered": False, "submitted": True})
    r = AgentControlAdapter("x:0.0", ac=ac).submit(_ac_env(tmp_path), "k")
    assert r["ok"] and r["already"]
    ad = AgentControlAdapter("x:0.0", ac=_FakeAC(status={"alive": False}))
    assert ad.poll({"task_id": "p:a", "attempt": 1, "result": str(tmp_path / "r.json")}) \
        .state == ex.CRASHED
    assert ad.restart({})["ok"] is False                        # never owns a live agent
