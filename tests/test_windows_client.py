"""Windows bridge — device half (clients/windows/owner_os_agent.py).

Runs on Linux against a fake `claude` executable, which is the point: the
agent is stdlib-only and platform-independent, so its security properties can
be pinned in CI rather than discovered on the owner's PC.

The properties under test:
  * the prompt reaches Claude on STDIN and never appears in argv (a prompt in
    argv is re-parsed by cmd.exe when `claude` is a .cmd shim — the
    "BatBadBut" injection class);
  * a workspace ID is resolved against LOCAL config, so an id the owner never
    enrolled — traversal-shaped or not — resolves to nothing;
  * the session id is captured and reused, so `send` continues a conversation
    instead of starting a new one every time;
  * the client's signature is byte-identical to what the server computes;
  * refusals are explained, never raised as stack traces at the server.
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "clients", "windows"))

import owner_os_agent as agent  # noqa: E402

from core import windows_bridge as wb  # noqa: E402

FAKE_CLAUDE = """#!{python}
import json, os, sys
record = os.environ["FAKE_CLAUDE_RECORD"]
prompt = sys.stdin.read()
with open(record, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": prompt, "cwd": os.getcwd()}}) + "\\n")
if os.environ.get("FAKE_CLAUDE_FAIL"):
    sys.stderr.write("boom\\n")
    sys.exit(3)
print(json.dumps({{"session_id": os.environ.get("FAKE_CLAUDE_SESSION", "sess-abc"),
                  "result": "fake reply to: " + prompt.strip()[:60]}}))
"""


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    record = tmp_path / "claude_calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("OOS_CLAUDE_CMD", str(path))
    return {"path": str(path), "record": record}


@pytest.fixture()
def workspace(tmp_path):
    d = tmp_path / "gaika-basket-extension"
    d.mkdir()
    (d / "manifest.json").write_text("{}")
    return d


@pytest.fixture()
def cfg(tmp_path, workspace, fake_claude):
    c = agent.Config(str(tmp_path / "cfg" / "agent.json"))
    c.data.update({"server": "https://owner-os.example",
                   "device_id": "win-0123456789abcdef",
                   "secret": "a" * 64,
                   "claude_cmd": fake_claude["path"],
                   "workspaces": [{"id": "gaika-basket", "path": str(workspace),
                                   "label": "GAIKA"}]})
    c.save()
    return c


def _calls(fake_claude) -> list:
    if not fake_claude["record"].exists():
        return []
    return [json.loads(line) for line in
            fake_claude["record"].read_text().splitlines() if line.strip()]


# ── config ──────────────────────────────────────────────────────────────────

def test_config_round_trips_and_is_written_owner_only(cfg):
    again = agent.Config(cfg.path)
    assert again.device_id == "win-0123456789abcdef"
    assert again.enrolled() is True
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600


def test_status_output_never_prints_the_secret(cfg, capsys):
    agent.main(["--config", cfg.path, "status"])
    out = capsys.readouterr().out
    assert "a" * 64 not in out
    assert "***set***" in out


def test_add_workspace_requires_an_existing_directory(cfg, tmp_path):
    rc = agent.main(["--config", cfg.path, "add-workspace", "--id", "ghost",
                     "--path", str(tmp_path / "nope")])
    assert rc == 2
    assert agent.Config(cfg.path).workspace("ghost") is None


@pytest.mark.parametrize("bad_id", ["../etc", "with space", "a" * 65, "", "_lead"])
def test_add_workspace_refuses_a_bad_id(cfg, workspace, bad_id):
    assert agent.main(["--config", cfg.path, "add-workspace", "--id", bad_id,
                       "--path", str(workspace)]) == 2


def test_add_workspace_normalises_case_rather_than_refusing_it(cfg, workspace):
    """The installer derives an id from the folder name, which is usually mixed
    case. Lowercasing locally is fine — the canonical id is whatever this file
    ends up holding, and that is what is reported to the server."""
    assert agent.main(["--config", cfg.path, "add-workspace", "--id", "GAIKA-Basket",
                       "--path", str(workspace)]) == 0
    assert agent.Config(cfg.path).workspace("gaika-basket") is not None


def test_add_workspace_is_idempotent_on_the_id(cfg, workspace):
    agent.main(["--config", cfg.path, "add-workspace", "--id", "gaika-basket",
                "--path", str(workspace)])
    assert len([w for w in agent.Config(cfg.path).workspaces
                if w["id"] == "gaika-basket"]) == 1


# ── workspace resolution is the anti-traversal gate ─────────────────────────

def test_a_workspace_the_owner_never_enrolled_is_unreachable(cfg):
    a = agent.Agent(cfg)
    with pytest.raises(agent.AgentError, match="not enrolled"):
        a.runner("some-other-repo")


@pytest.mark.parametrize("bad", ["../../Windows", r"..\..\Users", "C:\\Windows",
                                 "gaika/../..", "GAIKA", ""])
def test_traversal_shaped_ids_never_resolve(cfg, bad):
    with pytest.raises(agent.AgentError):
        agent.Agent(cfg).runner(bad)


def test_a_removed_directory_is_reported_not_guessed_at(cfg, workspace):
    for f in workspace.iterdir():
        f.unlink()
    workspace.rmdir()
    with pytest.raises(agent.AgentError, match="no longer exists"):
        agent.Agent(cfg).runner("gaika-basket")


# ── invoking Claude ─────────────────────────────────────────────────────────

def test_the_prompt_goes_on_stdin_and_never_into_argv(cfg, fake_claude):
    payload = 'refactor "the basket" & del C:\\Windows\\System32 || echo pwned'
    out = agent.Agent(cfg).execute({"action": "agent.send",
                                    "workspace_id": "gaika-basket",
                                    "params": {"text": payload}})
    call = _calls(fake_claude)[0]
    assert call["stdin"] == payload
    assert payload not in " ".join(call["argv"])
    for arg in call["argv"]:
        assert "System32" not in arg and "pwned" not in arg
    assert out["reply"].startswith("fake reply to:")


def test_claude_runs_inside_the_enrolled_directory(cfg, fake_claude, workspace):
    agent.Agent(cfg).execute({"action": "agent.send", "workspace_id": "gaika-basket",
                              "params": {"text": "hello"}})
    assert os.path.realpath(_calls(fake_claude)[0]["cwd"]) == os.path.realpath(str(workspace))


def test_the_session_is_captured_and_resumed_on_the_next_send(cfg, fake_claude):
    a = agent.Agent(cfg)
    a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
               "params": {"text": "first"}})
    second = a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
                        "params": {"text": "second"}})
    first_argv, second_argv = [c["argv"] for c in _calls(fake_claude)]
    assert "--resume" not in first_argv
    assert second_argv[second_argv.index("--resume") + 1] == "sess-abc"
    assert second["resumed"] is True


def test_agent_start_opens_a_new_session_instead_of_resuming(cfg, fake_claude):
    a = agent.Agent(cfg)
    a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
               "params": {"text": "first"}})
    out = a.execute({"action": "agent.start", "workspace_id": "gaika-basket",
                     "params": {"text": "start fresh"}})
    assert out["resumed"] is False
    assert "--resume" not in _calls(fake_claude)[1]["argv"]


def test_agent_start_without_text_still_sends_a_real_prompt(cfg, fake_claude):
    agent.Agent(cfg).execute({"action": "agent.start", "workspace_id": "gaika-basket",
                              "params": {}})
    assert _calls(fake_claude)[0]["stdin"].strip()


def test_a_failing_claude_is_reported_as_a_refusal_with_its_exit_code(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    with pytest.raises(agent.AgentError, match="claude exited 3"):
        agent.Agent(cfg).execute({"action": "agent.send",
                                  "workspace_id": "gaika-basket",
                                  "params": {"text": "hi"}})


def test_read_returns_the_bounded_transcript(cfg, fake_claude):
    a = agent.Agent(cfg)
    a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
               "params": {"text": "hello there"}})
    out = a.execute({"action": "agent.read", "workspace_id": "gaika-basket",
                     "params": {"lines": 5}})
    assert "hello there" in out["output"]
    assert len(out["output"].splitlines()) <= 5
    assert out["session_id"] == "sess-abc"


def test_the_transcript_buffer_cannot_grow_without_bound(cfg):
    r = agent.Agent(cfg).runner("gaika-basket")
    for _ in range(200):
        r._append("x" * 8192 + "\n")
    assert len(r.buffer) <= agent.MAX_BUFFER_BYTES


def test_status_of_an_idle_workspace(cfg):
    st = agent.Agent(cfg).execute({"action": "agent.status",
                                   "workspace_id": "gaika-basket"})
    assert st["running"] is False
    assert st["state"] == "idle"


def test_stopping_an_idle_workspace_is_a_no_op_not_an_error(cfg):
    out = agent.Agent(cfg).execute({"action": "agent.stop",
                                    "workspace_id": "gaika-basket",
                                    "params": {"confirm": True}})
    assert out["stopped"] is False


def test_stop_without_confirmation_is_refused(cfg):
    with pytest.raises(agent.AgentError, match="confirm"):
        agent.Agent(cfg).execute({"action": "agent.stop",
                                  "workspace_id": "gaika-basket",
                                  "params": {"confirm": False}})


def test_an_unsupported_action_is_refused(cfg):
    for action in ("shell.exec", "agent.eval", "", "workspace.delete"):
        with pytest.raises(agent.AgentError, match="unsupported|bad workspace"):
            agent.Agent(cfg).execute({"action": action, "workspace_id": "gaika-basket",
                                      "params": {}})


def test_an_oversized_prompt_is_refused_locally_too(cfg):
    with pytest.raises(agent.AgentError, match="exceeds"):
        agent.Agent(cfg).execute({"action": "agent.send",
                                  "workspace_id": "gaika-basket",
                                  "params": {"text": "x" * (agent.MAX_TEXT_BYTES + 1)}})


def test_workspace_list_reports_local_enrollment_only(cfg):
    out = agent.Agent(cfg).execute({"action": "workspace.list", "params": {}})
    assert [w["workspace_id"] for w in out["workspaces"]] == ["gaika-basket"]


# ── transport ───────────────────────────────────────────────────────────────

def test_the_clients_signature_matches_what_the_server_computes(cfg):
    body = b'{"wait":25}'
    path = agent._api_path(cfg.server, "/windows/poll")
    assert path == "/api/v1/windows/poll"
    assert agent._sign(cfg.secret, cfg.device_id, "1000", "nonce-aaaa1111", path, body) \
        == wb.sign(cfg.secret, cfg.device_id, "1000", "nonce-aaaa1111", path, body)


def test_a_server_behind_a_path_prefix_signs_that_prefix(cfg):
    assert agent._api_path("https://host/owner-os/", "/windows/result") == \
        "/owner-os/api/v1/windows/result"


def test_every_signed_request_carries_a_fresh_nonce(cfg, monkeypatch):
    seen = []

    class _Resp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen.append(dict(req.headers))
        return _Resp()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    c = agent.Client(cfg)
    c.poll([])
    c.poll([])
    nonces = [h["X-oos-nonce"] for h in seen]
    assert len(set(nonces)) == 2
    assert all(h["X-oos-device"] == cfg.device_id for h in seen)
    assert all(len(h["X-oos-signature"]) == 64 for h in seen)


def test_an_unenrolled_config_refuses_to_sign(tmp_path):
    c = agent.Config(str(tmp_path / "empty.json"))
    c.data["server"] = "https://owner-os.example"
    with pytest.raises(agent.AgentError, match="not enrolled"):
        agent.Client(c).poll([])


def test_a_failed_command_is_reported_back_rather_than_crashing_the_loop(cfg, monkeypatch):
    posted = {}
    monkeypatch.setattr(agent.Client, "result",
                        lambda self, cid, ok, result=None, error="":
                        posted.update({"cid": cid, "ok": ok, "error": error}))
    agent.Agent(cfg).handle({"command_id": "cmd-1", "action": "agent.status",
                             "workspace_id": "not-enrolled"})
    assert posted["ok"] is False
    assert "not enrolled" in posted["error"]


def test_the_run_loop_backs_off_instead_of_exiting_when_the_server_is_down(cfg, monkeypatch):
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise agent.AgentError("cannot reach server")

    sleeps = []
    monkeypatch.setattr(agent.Client, "poll", boom)
    monkeypatch.setattr(agent.time, "sleep", lambda s: sleeps.append(s))
    agent.Agent(cfg).run(once=True, log=lambda *_a: None)
    assert calls["n"] == 1
    assert sleeps == [1.0]      # backed off, did not raise
