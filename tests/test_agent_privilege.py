"""Reviewed agents under their own uid. The real-process tests run as root and drop to
`nobody` through real setpriv and a private tmux server. No live socket, DB or file."""
from __future__ import annotations

import json
import os
import pathlib
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time

import pytest

from core import agent_auth_mode as aam
from core import agent_control as ac
from core import agent_privilege as priv
from core import job_executor
from core.fullcycle.hosts import LocalHost, SshHost

ROOT = pathlib.Path(__file__).resolve().parents[1]
real = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("setpriv"),
                          reason="needs root and setpriv")
SECRET = "reviewer-secret-test-value"


@pytest.fixture
def sandbox(monkeypatch):
    """A world-reachable tree owned by `nobody`, and a home for it. tmp_path sits under a
    0700 directory that `nobody` could not enter."""
    base = tempfile.mkdtemp(prefix="agent-uid-", dir="/tmp")
    os.chmod(base, 0o755)
    n = pwd.getpwnam("nobody")
    home, project = os.path.join(base, "home"), os.path.join(base, "project")
    for d in (home, project, os.path.join(home, ".local", "bin")):
        os.makedirs(d)
    for path in (os.path.join(home, ".local"), os.path.join(home, ".local", "bin"), home, project):
        os.chown(path, n.pw_uid, n.pw_gid)
    t = priv.Target("nobody", n.pw_uid, n.pw_gid, home)
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(priv, "_lookup", lambda name: t)
    secret = os.path.join(base, "secret")
    with open(secret, "w") as fh:
        fh.write(SECRET)
    os.chmod(secret, 0o600)
    yield t, project, secret
    shutil.rmtree(base, ignore_errors=True)


# ── the setting ─────────────────────────────────────────────────────────────
def test_unset_changes_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_AS", raising=False)
    assert priv.target() is None and priv.claude_home() == "/root"
    assert aam.recovery_command("s-unset", "claude --continue")["cmd"].startswith("env -u")


@pytest.mark.parametrize("value,reason", [("no-such-user-here", "user_not_found"),
                                          ("root", "target_is_root"),
                                          ("../etc", "bad_user_name"), ("a b", "bad_user_name")])
def test_a_bad_setting_refuses(monkeypatch, value, reason):
    monkeypatch.setenv("AGENT_RUN_AS", value)
    with pytest.raises(priv.AgentPrivilegeError, match=reason):
        priv.target()


def test_a_non_root_runtime_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    with pytest.raises(priv.AgentPrivilegeError, match="runtime_not_root"):
        priv.target()


def test_a_group_zero_target_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(priv, "_lookup", lambda n: priv.Target(n, 1000, 0, "/h"))
    with pytest.raises(priv.AgentPrivilegeError, match="target_is_root"):
        priv.target()


def test_missing_setpriv_refuses(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(priv.os, "geteuid", lambda: 0)
    monkeypatch.setattr(priv.shutil, "which", lambda _: None)
    with pytest.raises(priv.AgentPrivilegeError, match="setpriv_missing"):
        priv.target()


# ── a bad setting starts nothing, on any path ───────────────────────────────
def test_host_refuses_rather_than_run_as_root(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    rc, out, err = LocalHost().run(["id", "-u"])
    assert (rc, out) == (126, "") and "user_not_found" in err


def test_recovery_refuses_rather_than_run_as_root(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    r = aam.recovery_command("s-bad", "claude --continue")
    assert r == {"ok": False, "reason": "agent_run_as:user_not_found"}


def test_job_step_refuses_rather_than_run_as_root(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    marker = tmp_path / "ran"
    r = job_executor._run_tests(str(tmp_path), [f"touch {marker}"])
    assert not marker.exists() and r["results"][0]["passed"] is False


def test_ssh_transport_keeps_the_runtime_uid(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    monkeypatch.setattr("core.fullcycle.hosts.subprocess.run", fake)
    assert SshHost("h.test").run(["true"])[0] == 0
    assert seen["argv"][0] == "ssh"


# ── agent_resume ────────────────────────────────────────────────────────────
@pytest.fixture
def resume(monkeypatch, tmp_path):
    """agent_resume up to the tmux call, with the gates that are not under test open."""
    from core import agent_creation_policy as acp
    calls = []
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: (
        calls.append(args) or (1 if args[0] == "has-session" else 0), "", ""))
    monkeypatch.setattr(ac, "agent_list", lambda *a, **k: {"agents": []})
    monkeypatch.setattr(acp, "authorize", lambda *a, **k: {"ok": True, "reason": "test"})
    monkeypatch.setattr(acp, "reserve", lambda *a, **k: None)
    monkeypatch.setattr(ac, "validate_project_dir",
                        lambda p: os.path.realpath(p), raising=False)
    monkeypatch.setenv("AGENT_CONTROL_ALLOWED_ROOTS", "/tmp")
    return calls


def _started(calls):
    return [c for c in calls if c[0] == "new-session"]


@real
def test_resume_wraps_the_pane(resume, sandbox):
    t, project, _ = sandbox
    r = ac.agent_resume(project, session_name="uid-a")
    assert r["resumed"] is True, r
    argv = _started(resume)[0]
    assert argv[argv.index("setpriv"):][:8] == [*priv._setpriv(t), "--reset-env"]
    assert argv.index("setpriv") < argv.index("claude")


def test_resume_refuses_a_bad_setting_before_tmux(resume, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    r = ac.agent_resume(str(tmp_path), session_name="uid-b")
    assert r["refused"] == "agent_run_as:user_not_found" and resume == []


@real
def test_resume_refuses_a_project_the_agent_cannot_use(resume, sandbox):
    base = tempfile.mkdtemp(prefix="agent-uid-closed-", dir="/tmp")     # root 0700
    try:
        r = ac.agent_resume(base, session_name="uid-c")
    finally:
        os.rmdir(base)
    assert r["refused"] == "agent_run_as:project_not_usable" and resume == []


@real
def test_resume_refuses_api_mode(resume, sandbox, monkeypatch):
    _, project, _ = sandbox
    monkeypatch.setattr(aam, "resolve", lambda *a, **k: {
        "ok": True, "mode": aam.API, "model": "m", "argv": ["launcher"], "reason": "t",
        "launcher": "launcher"})
    spent = []
    monkeypatch.setattr(aam, "commit_api_launch", lambda **k: spent.append(k))
    r = ac.agent_resume(project, session_name="uid-d")
    assert r["refused"] == "agent_run_as:api_mode_unsupported" and spent == [] and resume == []


@real
def test_resume_reads_the_agents_settings(resume, sandbox, monkeypatch):
    t, project, _ = sandbox
    seen = {}

    def spy(path, config_dir=None):
        seen["config_dir"] = config_dir
        return []
    monkeypatch.setattr(aam, "settings_api_overrides", spy)
    ac.agent_resume(project, session_name="uid-e")
    assert seen["config_dir"] == os.path.join(t.home, ".claude")


@real
def test_recovery_wraps_the_shape(sandbox):
    r = aam.recovery_command("s-ok", "claude --continue")
    assert r["ok"] and r["cmd"].startswith("setpriv ")
    assert "--reset-env" in r["cmd"] and r["cmd"].endswith("claude --continue")


def test_no_new_session_outside_the_two_gated_sites():
    """Anything else starting a pane would run the agent as root."""
    found = set()
    for path in [*ROOT.glob("core/**/*.py"), *ROOT.glob("tools/*.py"), *ROOT.glob("api/*.py")]:
        if re.search(r'\[\s*"(new-session|respawn-pane)"', path.read_text(errors="replace")):
            found.add(path.relative_to(ROOT).as_posix())
    assert found == {"core/agent_control.py", "core/session_recovery.py"}


# ── real processes ──────────────────────────────────────────────────────────
@real
def test_a_check_runs_as_the_agent_with_a_clean_env(sandbox, monkeypatch):
    t, project, secret = sandbox
    monkeypatch.setenv("RUNTIME_TOKEN", SECRET)
    rc, out, err = LocalHost().run(
        ["sh", "-c", 'id -u; id -G; pwd; echo "$HOME"; env; cat "$1"', "sh", secret],
        cwd=project, env={"CHECK": "kept", "ANTHROPIC_API_KEY": SECRET})
    assert rc != 0 and "Permission denied" in err
    lines = out.splitlines()
    assert lines[:4] == [str(t.uid), str(t.gid), project, t.home]
    assert "CHECK=kept" in out and SECRET not in out + err


@real
def test_file_access_cannot_be_redirected_at_a_root_file(sandbox):
    _, project, secret = sandbox
    h = LocalHost()
    link = os.path.join(project, "result.json")
    os.symlink(secret, link)
    assert h.read_file(link) is None
    h.write_file(link, "replaced")                  # the rename replaces the link itself
    assert not os.path.islink(link) and open(secret).read() == SECRET
    os.remove(link)
    os.symlink(secret, link + ".tmp")               # and the temp name is no way in
    with pytest.raises(Exception):
        h.write_file(link, "through the temp name")
    assert open(secret).read() == SECRET


@real
def test_write_and_read_work_inside_the_project(sandbox):
    _, project, _ = sandbox
    h = LocalHost()
    path = os.path.join(project, ".fullcycle", "tasks", "a-1.json")
    h.write_file(path, "{}")
    assert h.read_file(path) == "{}" and h.exists(path)
    assert os.stat(path).st_uid != 0


@real
def test_a_git_hook_runs_as_the_agent(sandbox):
    t, project, _ = sandbox
    h = LocalHost()
    assert h.run(["git", "init", "-q", project])[0] == 0
    hook = os.path.join(project, ".git", "hooks", "pre-commit")
    h.write_file(hook, f'#!/bin/sh\nid -u > "{project}/hook-uid"\n')
    h.run(["chmod", "+x", hook])
    rc, out, err = h.run(["git", "-C", project, "-c", "user.name=t", "-c", "user.email=t@t.test",
                          "commit", "-q", "--allow-empty", "-m", "x"])
    assert rc == 0, err
    assert open(f"{project}/hook-uid").read().strip() == str(t.uid)


@real
def test_a_job_step_runs_as_the_agent(sandbox, monkeypatch):
    t, project, secret = sandbox
    monkeypatch.setenv("RUNTIME_TOKEN", SECRET)
    ok, out = job_executor._run_step(project, "sh -c 'id -u; env'")
    assert ok and out.splitlines()[0] == str(t.uid) and SECRET not in out
    ok, out = job_executor._run_step(project, f"cat {secret}")
    assert not ok and SECRET not in out


@real
def test_no_way_back_to_root(sandbox):
    _, project, _ = sandbox
    script = os.path.join(project, "setuid-id")
    shutil.copy("/usr/bin/id", script)
    os.chown(script, 0, 0)
    os.chmod(script, 0o4755)                        # a setuid root binary the agent can reach
    rc, out, _ = LocalHost().run([script, "-u"], cwd=project)
    assert rc == 0 and out.strip() != "0"


@real
def test_a_real_pane_is_the_agent_and_gets_nothing_from_the_tmux_server(sandbox, monkeypatch):
    t, project, secret = sandbox
    out = os.path.join(project, "pane.json")
    bin_ = os.path.join(t.home, ".local", "bin", "claude")
    with open(bin_, "w") as fh:
        fh.write(f'''#!/usr/bin/env python3
import json, os, subprocess, sys
try:
    secret = open({secret!r}).read()
except OSError as e:
    secret = type(e).__name__
sock = os.environ.get("TMUX_PROBE", "")
tty = os.ttyname(0)
try:
    os.close(os.open("/dev/tty", os.O_RDWR)); dev_tty = True
except OSError:
    dev_tty = False
json.dump({{"uid": os.geteuid(), "groups": os.getgroups(), "env": dict(os.environ),
           "secret": secret, "cwd": os.getcwd(), "argv": sys.argv[1:], "tty": bool(tty),
           "dev_tty": dev_tty,
           "nnp": [l for l in open("/proc/self/status") if l.startswith(("NoNewPrivs", "CapEff"))],
           }}, open({out!r}, "w"))
''')
    os.chmod(bin_, 0o755)
    os.chown(bin_, t.uid, t.gid)
    tmp = tempfile.mkdtemp(prefix="agent-uid-tmux-", dir="/tmp")
    env = {**os.environ, "TMUX_TMPDIR": tmp, "RUNTIME_TOKEN": SECRET, "ANTHROPIC_API_KEY": SECRET}
    env.pop("TMUX", None)
    argv = aam.subscription_argv(["--marker"])
    try:
        rc = subprocess.run(["tmux", "new-session", "-d", "-s", "uid-real", "-c", project,
                             *priv.pane_prefix(t), *argv], env=env).returncode
        assert rc == 0
        for _ in range(100):
            if os.path.exists(out) and os.path.getsize(out):
                break
            time.sleep(0.05)
        seen = json.load(open(out))
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    assert seen["uid"] == t.uid and seen["groups"] == [] and seen["cwd"] == project
    assert seen["secret"] == "PermissionError" and seen["argv"] == ["--marker"]
    assert SECRET not in json.dumps(seen["env"])
    assert seen["env"]["HOME"] == t.home and "TMUX" not in seen["env"]
    assert "NoNewPrivs:\t1\n" in seen["nnp"] and "CapEff:\t0000000000000000\n" in seen["nnp"]
    assert seen["dev_tty"] is True


# ── transcripts live in the agent's home ────────────────────────────────────
@real
def test_transcripts_skip_what_would_hang_or_redirect_root(sandbox):
    t, project, secret = sandbox
    d = os.path.join(t.home, ".claude", "projects", project.replace("/", "-"))
    os.makedirs(d)
    good = os.path.join(d, "good.jsonl")
    open(good, "w").close()
    os.mkfifo(os.path.join(d, "fifo.jsonl"))
    os.symlink(secret, os.path.join(d, "link.jsonl"))
    assert priv.transcripts(project) == [good]
    assert ac.conversation_recently_active(project) is True


def test_preflight_refuses_without_the_setting(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_uid_preflight",
                                                  ROOT / "tools" / "agent_uid_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.delenv("AGENT_RUN_AS", raising=False)
    assert mod.main([]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


@real
def test_preflight_catches_a_readable_file_and_passes_a_closed_one(sandbox, capsys):
    import importlib.util
    t, project, secret = sandbox
    spec = importlib.util.spec_from_file_location("agent_uid_preflight",
                                                  ROOT / "tools" / "agent_uid_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bin_ = os.path.join(t.home, ".local", "bin", "claude")
    with open(bin_, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(bin_, 0o755)
    mod.main(["--deny", secret, "--project", project])
    closed = json.loads(capsys.readouterr().out)
    assert closed["checks"][f"cannot_reach:{secret}"] is True
    assert closed["checks"]["claude_runs"] is True and closed["checks"]["project_usable"] is True
    assert SECRET not in json.dumps(closed)
    os.chmod(secret, 0o644)
    assert mod.main(["--deny", secret]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][f"cannot_reach:{secret}"] is False
