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
import re
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


# ── installer transport rule ────────────────────────────────────────────────
# install.ps1 refuses plain HTTP with one principled exception: a Tailscale
# address, where WireGuard already provides the encryption and peer
# authentication TLS would add. That exception must not leak: an attacker-
# chosen host outside the tailnet ranges has to stay refused. The regex is the
# security-relevant part, so it is pinned here rather than trusted by eye.

def _installer_tailnet_regex():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent
            / "clients" / "windows" / "install.ps1").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "isTailnet = $true" in ln and "match" in ln)
    return re.compile(line.split("'")[1].replace("\\\\", "\\"))


@pytest.mark.parametrize("host", [
    "100.64.0.1", "100.108.182.33", "100.127.255.254", "100.70.1.1", "100.119.9.9",
])
def test_installer_accepts_real_tailnet_addresses(host):
    assert _installer_tailnet_regex().match(host)


@pytest.mark.parametrize("host", [
    "100.63.255.254",   # just below the CGNAT range
    "100.128.0.1",      # just above it
    "100.5.5.5",
    "10.0.0.1",
    "1100.64.0.1",      # must not match on a prefix
    "evil.com",
])
def test_installer_refuses_everything_outside_the_tailnet_range(host):
    assert not _installer_tailnet_regex().match(host)


def test_installer_still_refuses_plain_http_generally():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parent.parent
            / "clients" / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "Refusing to install against a non-HTTPS, non-Tailscale server" in text
    assert "$Server -notmatch '^https://'" in text


# ── install.ps1 must parse on Windows PowerShell 5.1 ────────────────────────
# The owner's PC failed with "unexpected token '}'" (115, 157) and "string
# missing terminator" on a file that was valid UTF-8. Cause: PowerShell 5.1
# reads a BOM-less script as ANSI, and byte 0x94 — present in BOTH the em dash
# (E2 80 94) and the box-drawing rule (E2 94 80) used for section headers —
# decodes to U+201D, which PowerShell honours as a string delimiter. A comment
# separator was enough to break the script.

import ps_lint  # noqa: E402  (tests/ is on sys.path under pytest)


def _installer_path():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "clients" / "windows" / "install.ps1")


def test_installer_parses_clean():
    assert ps_lint.check(str(_installer_path())) == []


def test_installer_is_pure_ascii_with_a_bom():
    raw = _installer_path().read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "missing UTF-8 BOM"
    assert all(b < 128 for b in raw[3:]), "non-ASCII byte would be mis-decoded as ANSI"


@pytest.mark.parametrize("encoding", ["cp1251", "cp1252", "cp850"])
def test_installer_still_parses_when_read_as_ansi(encoding):
    """The real test: the PC does not decode this file as UTF-8. It must parse
    correctly even when read through a single-byte codepage."""
    body = _installer_path().read_bytes()[3:]
    assert ps_lint.scan(body.decode(encoding, "replace")) == []


def test_the_linter_would_have_caught_the_original_defect():
    """A checker that cannot fail is worthless — pin that it rejects the exact
    construct that broke, rather than trusting it because it currently passes."""
    broken = 'Write-Host "through start/stop — there is no remote shell."\n'
    assert ps_lint.scan(broken.encode("utf-8").decode("cp1251", "replace"))
    assert ps_lint.scan('$a = "unterminated\n')
    assert ps_lint.scan('if ($x) { echo 1 }}\n')
    assert ps_lint.scan('Write-Host "ok"\n') == []


# ── ACL hardening must be localization-independent ─────────────────────────
# On the owner's PC icacls failed with "No mapping between account names and
# security IDs was done": the script named SYSTEM and Administrators in
# ENGLISH, and those groups carry localized names on a localized Windows. The
# failure came AFTER /inheritance:r was processed, so the directory holding the
# device secret could be left with inheritance stripped and no grants applied —
# a security defect, not a cosmetic warning.

def _installer_text():
    return _installer_path().read_text(encoding="utf-8-sig")


def test_acl_grants_are_addressed_by_well_known_sid():
    text = _installer_text()
    icacls = [ln for ln in text.splitlines() if "icacls" in ln and "/grant" in ln]
    assert icacls, "no icacls grant line found"
    line = icacls[0]
    assert "*S-1-5-18:" in line, "Local System must be granted by SID"
    assert "*S-1-5-32-544:" in line, "Administrators must be granted by SID"
    # The English names must not be used as identities anywhere in the grant.
    assert '"SYSTEM:' not in line and '"Administrators:' not in line


def test_the_current_user_is_taken_from_the_token_not_composed_from_env():
    """USERDOMAIN\\USERNAME breaks for Microsoft accounts and AzureAD logins as
    well as for localized systems; the token's SID never does."""
    text = _installer_text()
    assert "WindowsIdentity]::GetCurrent()).User.Value" in text
    assert '$env:USERDOMAIN\\$env:USERNAME' not in text


def test_a_failed_acl_hardening_aborts_instead_of_storing_the_secret():
    text = _installer_text()
    idx = text.index("icacls $InstallDir /inheritance:r")
    after = text[idx:idx + 1600]
    assert "$LASTEXITCODE -ne 0" in after, "icacls exit code must be checked"
    assert "refusing to continue" in after.lower()
    # The check has to come BEFORE enrollment writes the secret.
    assert text.index("refusing to continue") < text.index("Enrolling this device")


def test_no_english_account_names_survive_anywhere_in_the_acl_step():
    text = _installer_text()
    idx = text.index("Restricting permissions")
    block = text[idx:idx + 1200]
    for name in ('"Administrators:', '"SYSTEM:', '"Users:', '"Everyone:'):
        assert name not in block, f"localized-name identity {name} still present"


# ── one visible agent, never two ───────────────────────────────────────────
# The owner wants to WATCH Claude work in the GAIKA folder. The bridge runs it
# headlessly, so "watch" is a live transcript tail (one process, fully visible)
# rather than a second interactive Claude. And if the owner does start Claude by
# hand in an enrolled folder, the bridge must refuse to start a second one.

def test_a_foreign_claude_in_the_workspace_blocks_a_second_one(cfg, workspace, monkeypatch):
    monkeypatch.setattr(agent, "foreign_claude_in", lambda p: 4321)
    with pytest.raises(agent.AgentError, match="outside Owner OS"):
        agent.Agent(cfg).execute({"action": "agent.send",
                                  "workspace_id": "gaika-basket",
                                  "params": {"text": "hello"}})


def test_no_foreign_process_means_normal_operation(cfg, fake_claude, monkeypatch):
    monkeypatch.setattr(agent, "foreign_claude_in", lambda p: None)
    out = agent.Agent(cfg).execute({"action": "agent.send",
                                    "workspace_id": "gaika-basket",
                                    "params": {"text": "hello"}})
    assert out["reply"].startswith("fake reply to:")


def test_detection_failure_never_blocks_a_send(cfg, fake_claude, monkeypatch):
    """Best-effort by design: an unreadable process table must not produce a
    false refusal that silences the bridge."""
    def boom(_p):
        raise OSError("no process table")
    monkeypatch.setattr(agent, "_proc_cwd_windows", boom)
    assert agent.foreign_claude_in("/definitely/not/a/real/path") is None


def test_watch_is_a_subcommand_that_needs_an_enrolled_workspace(cfg, capsys):
    assert agent.main(["--config", cfg.path, "watch", "--id", "not-enrolled"]) == 2
    err = capsys.readouterr().err
    assert "not enrolled" in err


def test_watch_reads_the_same_transcript_the_runner_writes(cfg, fake_claude):
    a = agent.Agent(cfg)
    a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
               "params": {"text": "visible please"}})
    import pathlib
    log = pathlib.Path(cfg.path).parent / "state" / "gaika-basket.log"
    assert log.exists()
    assert "visible please" in log.read_text(encoding="utf-8")


# ── reading the owner's own visible Claude session ─────────────────────────
# The owner keeps a Claude open in the GAIKA folder (session "gaika-windows").
# Windows has no tmux-style input injection, so the bridge cannot type into that
# console — but the conversation is on disk, so Owner OS can still SEE it.
# Reporting the workspace as "idle" while a visible session works in it would be
# a lie; the bridge reports it, reads it, and refuses to start a second Claude.

@pytest.fixture()
def external_session(tmp_path, workspace, monkeypatch):
    """Claude Code's own on-disk transcript for the enrolled folder.

    The real encoding of the directory name has varied between Claude versions,
    so the agent matches on a normalized slug of the path. The fixture therefore
    uses one plausible encoding (separators -> '-') and the lookup must still
    find it."""
    home = tmp_path / "claudehome"
    encoded = str(workspace).replace("\\", "-").replace("/", "-").replace(":", "-")
    proj = home / "projects" / encoded
    proj.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "message": {"content": "check the cart badge"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking at manifest.json now"}]}}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return {"session_id": sid, "dir": proj}


def test_an_owner_started_session_is_discovered(cfg, workspace, external_session):
    found = agent.external_session_file(str(workspace))
    assert found and found.endswith(".jsonl")
    out = agent.read_external_session(str(workspace), lines=20)
    assert out["external_session"] is True
    assert out["session_id"] == external_session["session_id"]
    assert "check the cart badge" in out["output"]
    assert "Looking at manifest.json" in out["output"]


def test_status_reports_the_visible_session_instead_of_claiming_idle(
        cfg, workspace, external_session, monkeypatch):
    monkeypatch.setattr(agent, "foreign_claude_in", lambda p: 9999)
    st = agent.Agent(cfg).execute({"action": "agent.status",
                                   "workspace_id": "gaika-basket"})
    assert st["state"] == "external_session"
    assert st["controllable"] is False
    assert st["pid"] == 9999
    assert st["session_id"] == external_session["session_id"]


def test_read_returns_the_owners_conversation_not_an_empty_buffer(
        cfg, workspace, external_session, monkeypatch):
    monkeypatch.setattr(agent, "foreign_claude_in", lambda p: 9999)
    out = agent.Agent(cfg).execute({"action": "agent.read",
                                    "workspace_id": "gaika-basket",
                                    "params": {"lines": 20}})
    assert out["source"] == "external_claude_session"
    assert "check the cart badge" in out["output"]


def test_no_external_session_means_the_normal_agent_path(cfg, fake_claude, monkeypatch):
    monkeypatch.setattr(agent, "foreign_claude_in", lambda p: None)
    a = agent.Agent(cfg)
    a.execute({"action": "agent.send", "workspace_id": "gaika-basket",
               "params": {"text": "hi"}})
    out = a.execute({"action": "agent.read", "workspace_id": "gaika-basket",
                     "params": {"lines": 10}})
    assert out["source"] == "owner_os_agent"
    assert "hi" in out["output"]


# ── workspace.inspect on the device ────────────────────────────────────────

def test_inspect_reports_git_identity_and_content(cfg, workspace):
    import subprocess as sp
    sp.run(["git", "init", "-q"], cwd=str(workspace), check=True)
    sp.run(["git", "config", "user.email", "t@example.com"], cwd=str(workspace), check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=str(workspace), check=True)
    sp.run(["git", "add", "-A"], cwd=str(workspace), check=True)
    sp.run(["git", "commit", "-qm", "seed"], cwd=str(workspace), check=True)
    (workspace / "dirty.txt").write_text("uncommitted")

    out = agent.Agent(cfg).execute({"action": "workspace.inspect",
                                    "workspace_id": "gaika-basket",
                                    "params": {"max_files": 100}})
    assert out["is_git_repo"] is True
    assert out["head"] and len(out["head"]) == 40
    assert out["commit_count"] == "1"
    assert out["dirty_count"] == 1
    assert "manifest.json" in out["files"]
    assert len(out["files"]["manifest.json"]) == 16      # short sha256


def test_inspect_works_on_a_folder_with_no_git_history(cfg, workspace):
    out = agent.Agent(cfg).execute({"action": "workspace.inspect",
                                    "workspace_id": "gaika-basket", "params": {}})
    assert out["exists"] is True
    assert out["is_git_repo"] is False
    assert out["file_count"] >= 1


def test_inspect_honours_the_file_cap(cfg, workspace):
    for i in range(30):
        (workspace / f"f{i}.txt").write_text(str(i))
    out = agent.Agent(cfg).execute({"action": "workspace.inspect",
                                    "workspace_id": "gaika-basket",
                                    "params": {"max_files": 5}})
    assert len(out["files"]) == 5
    assert out["files_skipped"] >= 25
    assert out["file_count"] == len(out["files"]) + out["files_skipped"]
