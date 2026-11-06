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
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import agent_auth_mode as aam
from core import agent_control as ac
from core import agent_privilege as priv
from core import job_executor
from core import os_task_queue
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
    monkeypatch.setenv("AGENT_CONTROL_DB", os.path.join(base, "agent_control.db"))
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
    monkeypatch.setattr(priv.shutil, "which", lambda *a, **k: None)
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
    start = argv.index(priv._setpriv_bin())
    assert argv[start:][:8] == [*priv._setpriv(t), "--reset-env"]
    assert start < argv.index("claude")


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
    assert r["ok"] and r["cmd"].startswith(priv._setpriv_bin() + " ")
    assert "--reset-env" in r["cmd"] and r["cmd"].endswith("claude --continue")


def test_no_new_session_outside_the_two_gated_sites():
    """Anything else starting a pane would run the agent as root."""
    found = set()
    for path in [*ROOT.glob("core/**/*.py"), *ROOT.glob("tools/*.py"), *ROOT.glob("api/*.py")]:
        if re.search(r'"(new-session|respawn-pane|new-window|split-window|run-shell|pipe-pane)"',
                     path.read_text(errors="replace")):
            found.add(path.relative_to(ROOT).as_posix())
    assert found == {"core/agent_control.py", "core/session_recovery.py", "core/tmux_launch.py"}


# ── root tooling stays out of trees the agent can change ────────────────────
@pytest.fixture
def agent_repo(sandbox):
    t, project, secret = sandbox
    LocalHost().run(["git", "init", "-q", project])
    marker = os.path.join(project, "ran-as-root")
    hook = os.path.join(project, ".git", "hooks", "post-checkout")
    LocalHost().write_file(hook, f'#!/bin/sh\ntouch "{marker}"\n')
    LocalHost().run(["chmod", "+x", hook])
    return t, project, marker


@real
def test_git_write_refuses_an_agent_tree(agent_repo):
    from core import git_write
    _, project, marker = agent_repo
    with pytest.raises(git_write.GitWriteError, match="root_tooling_disabled"):
        git_write._run(project, ["checkout", "-b", "x"], check=False)
    assert not os.path.exists(marker)


@real
def test_deliver_refuses_an_agent_tree(agent_repo):
    from core import deliver
    _, project, _ = agent_repo
    assert "root_tooling_disabled" in deliver.deliver(project, "b")["error"]
    assert "root_tooling_disabled" in deliver.rollback(project, "b", "abc")["error"]


@real
def test_release_refuses_an_agent_tree(agent_repo, monkeypatch):
    from core import release_controller as rc
    _, project, _ = agent_repo
    monkeypatch.setattr(rc, "_require", lambda _id: {})
    with pytest.raises(rc.ReleaseError, match="root_tooling_disabled"):
        rc._git(project, ["status"])
    with pytest.raises(rc.ReleaseError, match="root_tooling_disabled"):
        rc.run_tests(project, "rc-1")


@real
def test_validator_refuses_an_agent_tree(agent_repo):
    from core.validator import Validator
    _, project, _ = agent_repo
    with pytest.raises(priv.AgentPrivilegeError, match="root_tooling_disabled"):
        Validator(project)


@real
def test_a_job_on_an_agent_tree_fails_before_the_pipeline(agent_repo, monkeypatch):
    _, project, _ = agent_repo
    updates = []
    monkeypatch.setattr(job_executor.job_store, "get_job",
                        lambda _id: {"project_path": project, "status": "queued"})
    monkeypatch.setattr(job_executor.job_store, "update_job",
                        lambda _id, **kw: updates.append(kw))
    monkeypatch.setattr(job_executor.job_store, "touch_heartbeat", lambda _id: None)
    monkeypatch.setattr(job_executor.job_store, "append_log", lambda *a, **k: None)
    monkeypatch.setattr(job_executor, "_run_pipeline", lambda *a: pytest.fail("pipeline ran"))
    job_executor.execute("job-1")
    assert any("root_tooling_disabled" in str(u) for u in updates)


@real
def test_root_tooling_is_off_for_a_root_tree_too(sandbox):
    """No per-tree rule: ownership, a .git file and subdirectories each beat one."""
    base = tempfile.mkdtemp(prefix="agent-uid-root-", dir="/tmp")
    try:
        with pytest.raises(priv.AgentPrivilegeError, match="root_tooling_disabled"):
            priv.require_root_work(base)
    finally:
        os.rmdir(base)


@real
def test_slash_run_refuses_before_touching_the_tree(sandbox, monkeypatch):
    from runtime import engine
    monkeypatch.setattr(engine, "build_context", lambda p: pytest.fail("touched the tree"))
    with pytest.raises(priv.AgentPrivilegeError):
        engine.RuntimeEngine().run("x", "/tmp")


@real
def test_a_transcript_swapped_after_the_listing_is_not_followed(sandbox, monkeypatch):
    from core import os_task_queue
    _, project, secret = sandbox
    with open(secret, "w") as fh:
        fh.write('{"type": "user", "message": {"content": "%s"}}\n' % SECRET)
    link = os.path.join(project, "swapped.jsonl")
    os.symlink(secret, link)
    monkeypatch.setattr(priv, "transcripts", lambda cwd: [link])
    assert SECRET not in json.dumps(os_task_queue.transcript_messages(project))


# ── root writes nothing through the agent's tree ────────────────────────────
@real
def test_a_planted_handoff_link_does_not_reach_a_root_file(sandbox):
    from core import agent_context_budget as acb
    _, project, secret = sandbox
    os.symlink(secret, os.path.join(project, acb.HANDOFF_FILENAME))
    path = acb.write_handoff(project, "handoff")
    assert path and open(secret).read() == SECRET and open(path).read() == "handoff"
    assert os.stat(path).st_uid != 0


@real
def test_a_planted_checkpoint_link_does_not_reach_a_root_file(sandbox):
    from core import context_budget as cb
    _, project, secret = sandbox
    os.symlink(secret, os.path.join(project, "CONTEXT_CHECKPOINT.md.tmp"))
    os.symlink(secret, os.path.join(project, "CONTEXT_CHECKPOINT.md"))
    cb.write_project_checkpoint_copy(project, "text")     # refused or replaced, both fine
    assert open(secret).read() == SECRET


@real
def test_a_goal_transcript_fifo_does_not_hang(sandbox):
    from core import native_goals
    _, project, _ = sandbox
    fifo = os.path.join(project, "g.jsonl")
    os.mkfifo(fifo)
    assert native_goals.goal_state(project, "s", path=fifo)["active"] is False


@real
def test_work_evidence_does_not_follow_a_planted_report(sandbox):
    from core import work_evidence
    _, project, secret = sandbox
    link = os.path.join(project, "report.md")
    os.symlink(secret, link)
    assert work_evidence._read(link) is None


# ── the setting is pinned to the instance ───────────────────────────────────
@real
def test_a_process_without_the_variable_obeys_the_pin(sandbox, monkeypatch):
    t, _, _ = sandbox
    priv.pin(t)
    assert stat.S_IMODE(os.stat(priv._pin_path()).st_mode) == 0o600
    monkeypatch.delenv("AGENT_RUN_AS")
    assert priv.target() == t
    rc, out, _ = LocalHost().run(["id", "-u"])
    assert rc == 0 and out.strip() == str(t.uid)


@real
def test_starting_an_agent_pins_the_setting(resume, sandbox):
    t, project, _ = sandbox
    assert ac.agent_resume(project, session_name="uid-p")["resumed"] is True
    assert open(priv._pin_path()).read().strip() == t.name


@real
def test_a_pin_that_cannot_be_written_starts_nothing(resume, sandbox, monkeypatch):
    _, project, _ = sandbox
    def refuse(_t):
        raise OSError("read-only")
    monkeypatch.setattr(priv, "pin", refuse)
    r = ac.agent_resume(project, session_name="uid-q")
    assert r["refused"] == "agent_run_as:pin_failed" and _started(resume) == []


@real
def test_a_different_variable_refuses(sandbox, monkeypatch):
    t, _, _ = sandbox
    priv.pin(t)
    monkeypatch.setenv("AGENT_RUN_AS", "daemon")
    with pytest.raises(priv.AgentPrivilegeError, match="setting_mismatch"):
        priv.target()


@real
@pytest.mark.parametrize("change", ["writable", "symlink", "empty"])
def test_an_unsafe_pin_refuses(sandbox, monkeypatch, change, tmp_path):
    t, _, secret = sandbox
    priv.pin(t)
    path = priv._pin_path()
    if change == "writable":
        os.chmod(path, 0o666)
    elif change == "symlink":
        os.remove(path)
        os.symlink(secret, path)
    else:
        open(path, "w").close()
    monkeypatch.delenv("AGENT_RUN_AS")
    with pytest.raises(priv.AgentPrivilegeError, match="pin_unsafe"):
        priv.target()
    assert LocalHost().run(["id", "-u"])[0] == 126


@real
def test_a_pin_is_per_instance(sandbox, monkeypatch, tmp_path):
    t, _, _ = sandbox
    priv.pin(t)
    monkeypatch.delenv("AGENT_RUN_AS")
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "other.db"))
    assert priv.target() is None


def test_root_work_is_unrestricted_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_RUN_AS", raising=False)
    priv.require_root_work(str(tmp_path))


def test_root_work_refuses_a_bad_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    with pytest.raises(priv.AgentPrivilegeError, match="user_not_found"):
        priv.require_root_work(str(tmp_path))


# ── a pane that is not the target is not driven ─────────────────────────────
def test_pane_problem(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(priv, "target", lambda: priv.Target("nobody", 65534, 65534, "/h"))
    monkeypatch.setattr(priv, "process_uid", lambda pid: {1: 65534, 2: 0}.get(pid))
    assert priv.pane_problem(1) is None
    assert priv.pane_problem(2) == "pane_uid_mismatch"
    assert priv.pane_problem(None) == "pane_uid_mismatch"


def test_process_uid_reads_the_effective_uid():
    assert priv.process_uid(os.getpid()) == os.geteuid()
    assert priv.process_uid(None) is None and priv.process_uid(2 ** 30) is None


def test_send_refuses_a_root_pane(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    typed = []

    def fake(args, stdin=None):
        typed.append(args)
        return 0, "123\n", ""
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: {"pid": pid})
    monkeypatch.setattr(priv, "target", lambda: priv.Target("nobody", 65534, 65534, "/h"))
    monkeypatch.setattr(priv, "process_uid", lambda pid: 0)
    r = ac._deliver("s:0.0", "work", "agent_send", None)
    assert r["refused"] == "agent_run_as:pane_uid_mismatch" and r["delivered"] is False
    assert [a[0] for a in typed] == ["display-message"]


@pytest.fixture
def root_pane(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    sent = []

    def fake(args, stdin=None):
        sent.append(args[0])
        return 0, "123\n", ""
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: {"pid": pid + 1})
    monkeypatch.setattr(priv, "target", lambda: priv.Target("nobody", 65534, 65534, "/h"))
    monkeypatch.setattr(priv, "process_uid", lambda pid: 0)
    return sent


def test_a_root_shell_around_a_dropped_claude_is_refused(root_pane, monkeypatch):
    monkeypatch.setattr(priv, "process_uid", lambda pid: {123: 0, 124: 65534}[pid])
    assert ac._pane_run_as_problem("s:0.0") == "pane_uid_mismatch"


def test_a_permission_prompt_is_not_approved_in_a_root_pane(root_pane):
    with pytest.raises(ac.AgentControlError, match="pane_uid_mismatch"):
        ac.approve_prompt("s:0.0")
    assert "send-keys" not in root_pane


def test_a_clear_is_not_submitted_in_a_root_pane(root_pane, monkeypatch):
    monkeypatch.setattr(ac, "pending_input_text", lambda t: "/clear")
    with pytest.raises(ac.AgentControlError, match="pane_uid_mismatch"):
        ac.submit_clear("s:0.0")
    assert "send-keys" not in root_pane


def test_auto_mode_is_not_pressed_in_a_root_pane(root_pane):
    r = ac.ensure_auto_mode("s:0.0", tail_fn=lambda: "shift+tab to cycle")
    assert r["action"] == "error" and "pane_uid_mismatch" in r["error"]
    assert "send-keys" not in root_pane


def test_control_keys_are_not_sent_to_a_root_pane(root_pane, monkeypatch):
    monkeypatch.setattr(ac, "_pane_is_live_agent", lambda t: {"target": t, "session": "s"})
    monkeypatch.setattr(ac, "automation_refusal", lambda *a, **k: "")
    r = ac.send_control_keys("s:0.0", ["Escape"])
    assert r["sent"] is False and "send-keys" not in root_pane


def test_send_refuses_a_pane_with_no_agent(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: (0, "123\n", ""))
    monkeypatch.setattr(ac, "find_claude_in_pane", lambda pid: None)
    monkeypatch.setattr(priv, "target", lambda: priv.Target("nobody", 65534, 65534, "/h"))
    assert ac._pane_run_as_problem("s:0.0") == "pane_uid_mismatch"


def test_send_is_unchanged_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_AS", raising=False)
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: pytest.fail("no extra tmux call"))
    assert ac._pane_run_as_problem("s:0.0") is None


@real
def test_resume_does_not_reuse_a_root_agent(resume, sandbox, monkeypatch):
    _, project, _ = sandbox
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda _d: {
        "target": "old:0.0", "session": "old", "pid": os.getpid(), "claude_pid": os.getpid(),
        "claude_cwd": project})
    r = ac.agent_resume(project, session_name="uid-f")
    assert r["refused"] == "agent_run_as:pane_uid_mismatch" and _started(resume) == []


# ── real processes ──────────────────────────────────────────────────────────
@real
def test_a_setpriv_in_the_agents_home_is_never_run(sandbox):
    t, project, _ = sandbox
    marker = os.path.join(project, "planted-ran")
    planted = os.path.join(t.home, ".local", "bin", "setpriv")
    with open(planted, "w") as fh:
        fh.write(f'#!/bin/sh\ntouch "{marker}"\n')
    os.chmod(planted, 0o755)
    assert os.path.isabs(priv._setpriv(t)[0])
    assert priv._setpriv_bin().startswith(("/usr/bin/", "/bin/"))
    LocalHost().run(["true"])
    priv.can_use_dir(t, project)
    job_executor._run_step(project, "true")
    assert not os.path.exists(marker)


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
    from core import os_task_queue
    with open(good, "w") as fh:
        fh.write('{"type": "user", "message": {"content": "hello"}}\n')
    assert [m for m in os_task_queue.transcript_messages(project)]    # no hang on the FIFO


@real
def test_open_regular_refuses_a_fifo_and_a_symlink(sandbox):
    t, project, secret = sandbox
    fifo = os.path.join(project, "f")
    os.mkfifo(fifo)
    link = os.path.join(project, "l")
    os.symlink(secret, link)
    for path in (fifo, link):
        with pytest.raises(OSError):
            priv.open_regular(path)


def test_open_regular_follows_links_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_RUN_AS", raising=False)
    real_file, link = tmp_path / "a", tmp_path / "b"
    real_file.write_text("x")
    link.symlink_to(real_file)
    with priv.open_regular(str(link)) as fh:
        assert fh.read() == "x"


@real
def test_report_read_refuses_a_fifo(sandbox, monkeypatch):
    _, project, _ = sandbox
    monkeypatch.setattr(ac, "validate_project_dir", lambda p: p)
    reports = os.path.join(project, ac._REPORT_SUBDIRS[0])
    os.makedirs(reports)
    monkeypatch.setattr(ac.os.path, "isfile", lambda p: True)
    fifo = os.path.join(reports, "r.md")
    os.mkfifo(fifo)
    with pytest.raises(ac.AgentControlError, match="not readable"):
        ac.agent_report_read(project, os.path.relpath(fifo, project))


def test_a_vanished_user_is_not_roots_home(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user-here")
    assert priv.claude_home() == "/nonexistent"


@real
def test_resume_does_not_reuse_a_dropped_claude_in_a_root_shell(resume, sandbox, monkeypatch):
    t, project, _ = sandbox
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda _d: {
        "target": "old:0.0", "session": "old", "pid": 1, "claude_pid": 2, "claude_cwd": project})
    monkeypatch.setattr(priv, "process_uid", lambda pid: {1: 0, 2: t.uid}[pid])
    r = ac.agent_resume(project, session_name="uid-g")
    assert r["refused"] == "agent_run_as:pane_uid_mismatch"


@real
@pytest.mark.parametrize("entry", ["*", "{project}", "{parent}/*", "/opt/not-the-agents-{n}"])
def test_preflight_reads_roots_git_trust(sandbox, monkeypatch, tmp_path, capsys, entry):
    import importlib.util
    t, project, _ = sandbox
    spec = importlib.util.spec_from_file_location("agent_uid_preflight",
                                                  ROOT / "tools" / "agent_uid_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    value = entry.format(project=project, parent=os.path.dirname(project), n=os.getpid())
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[safe]\n\tdirectory = {value}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    trusted = mod._root_trusts(t)
    assert trusted == ([] if entry.startswith("/opt/") else [value])
    mod.main([])
    capsys.readouterr()


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
    assert closed["checks"]["no_new_privs_and_no_caps"] is True
    assert closed["checks"][f"cannot_reach:{secret}"] is True
    assert closed["checks"]["claude_runs"] is True and closed["checks"]["project_usable"] is True
    assert SECRET not in json.dumps(closed)
    os.chmod(secret, 0o644)
    assert mod.main(["--deny", secret]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][f"cannot_reach:{secret}"] is False
    os.chmod(secret, 0o600)
    missing = secret + "-typo"
    assert mod.main(["--deny", missing]) == 1
    assert json.loads(capsys.readouterr().out)["checks"][f"deny_path_exists:{missing}"] is False


# ── the pin is written by the first dropped work, whatever that work is ─────
@real
def test_the_first_dropped_command_pins_the_setting(sandbox, monkeypatch):
    t, _, _ = sandbox
    assert LocalHost().run(["id", "-u"])[0] == 0
    monkeypatch.delenv("AGENT_RUN_AS")
    assert priv.target() == t


@real
def test_a_pane_started_after_a_host_command_is_dropped_without_the_variable(
        resume, sandbox, monkeypatch):
    t, project, _ = sandbox
    LocalHost().run(["true"])
    monkeypatch.delenv("AGENT_RUN_AS")
    assert ac.agent_resume(project, session_name="uid-r")["resumed"] is True
    assert f"--reuid={t.uid}" in _started(resume)[0]


@real
def test_no_dropped_work_runs_when_the_pin_cannot_be_written(sandbox, monkeypatch):
    def refuse(_t):
        raise OSError("read-only")
    monkeypatch.setattr(priv, "pin", refuse)
    with pytest.raises(priv.AgentPrivilegeError, match="pin_failed"):
        priv.target()
    assert LocalHost().run(["id", "-u"])[0] == 126


@real
def test_concurrent_first_use_leaves_one_pin(sandbox):
    t, _, _ = sandbox
    with ThreadPoolExecutor(16) as pool:
        got = list(pool.map(lambda _: priv.target(), range(64)))
    assert got == [t] * 64
    d = os.path.dirname(priv._pin_path())
    assert [f for f in os.listdir(d) if "run_as" in f] == [os.path.basename(priv._pin_path())]


@real
def test_a_pin_is_not_replaced_by_another_name(sandbox):
    t, _, _ = sandbox
    priv.pin(t)
    with pytest.raises(OSError):
        priv.pin(priv.Target("daemon", 1, 1, "/"))
    assert open(priv._pin_path()).read().strip() == t.name


@real
def test_preflight_checks_without_pinning(sandbox, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_uid_preflight",
                                                  ROOT / "tools" / "agent_uid_preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    monkeypatch.setattr(preflight, "_as_agent", lambda *a: 1)
    assert preflight.main([]) == 1
    assert '"run_as": "nobody"' in capsys.readouterr().out
    assert not os.path.lexists(priv._pin_path())


# ── the task file is written by the agent, not by root ──────────────────────
def _task():
    return {"id": "abc123", "created_at": "now", "target": "t:0.0", "text": "do it"}


@real
def test_the_task_file_belongs_to_the_agent(sandbox):
    t, project, _ = sandbox
    path = os_task_queue._write_task_file(_task(), project)
    st = os.lstat(path)
    assert path == os.path.join(project, ".owner-os-tasks", "task_abc123.md")
    assert (st.st_uid, stat.S_ISREG(st.st_mode)) == (t.uid, True)
    assert os.lstat(os.path.dirname(path)).st_uid == t.uid
    assert "do it" in open(path).read()


@real
def test_a_planted_task_dir_link_gets_no_root_write(sandbox):
    t, project, secret = sandbox
    closed = os.path.join(os.path.dirname(project), "closed")
    os.mkdir(closed, 0o700)
    os.symlink(closed, os.path.join(project, ".owner-os-tasks"))
    with pytest.raises(OSError):
        os_task_queue._write_task_file(_task(), project)
    assert os.listdir(closed) == []


@real
def test_a_broken_setting_writes_no_task_file(sandbox, monkeypatch):
    _, project, _ = sandbox
    monkeypatch.setenv("AGENT_RUN_AS", "no-such-user")
    monkeypatch.setattr(priv, "_lookup", lambda name: (_ for _ in ()).throw(
        priv.AgentPrivilegeError("user_not_found")))
    with pytest.raises(OSError):
        os_task_queue._write_task_file(_task(), project)
    assert os.listdir(project) == []


# ── root git stays out of an instance with agents under their own uid ───────
def _root_git_sites(tree):
    from core import agent_context_budget, agent_context_recovery, agent_event_supervisor
    from core import context_budget, git_push_policy, project_state, project_supervisor
    from core import work_evidence
    from pathlib import Path
    return [
        (lambda: work_evidence._git_head(tree), ""),
        (lambda: work_evidence._git_new_commits(tree, "abc"), []),
        (lambda: project_supervisor._git(tree, ["log"]), ""),
        (lambda: project_state.git_head(tree), ""),
        (lambda: context_budget._git_head(tree), "unknown"),
        (lambda: agent_context_budget._git(tree, "status"), ""),
        (lambda: agent_event_supervisor.Ports.artifacts(None, tree), {}),
        (lambda: agent_context_recovery._git_head(Path(tree)), None),
        (lambda: git_push_policy.evaluate_push("git push", tree, {})["allowed"], False),
    ]


@real
def test_no_root_git_site_spawns_under_the_setting(sandbox, monkeypatch):
    _, project, _ = sandbox
    os.mkdir(os.path.join(project, ".git"))
    spawned = []
    sites = _root_git_sites(project)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    for call, unknown in sites:
        assert call() == unknown
    assert spawned == []


@real
def test_a_bad_setting_keeps_root_git_off_too(sandbox, monkeypatch):
    _, project, _ = sandbox
    os.mkdir(os.path.join(project, ".git"))
    spawned = []
    sites = _root_git_sites(project)
    monkeypatch.setenv("AGENT_RUN_AS", "root")
    monkeypatch.setattr(priv, "_lookup", lambda name: priv.Target("root", 0, 0, "/root"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    for call, _unknown in sites:
        call()
    assert spawned == []


@real
def test_a_config_planted_in_a_shared_tree_runs_nothing_as_root(sandbox):
    from core import agent_context_budget
    _, project, _ = sandbox
    tree = os.path.join(os.path.dirname(project), "shared")
    os.mkdir(tree)
    subprocess.run(["git", "init", "-q", tree], check=True, capture_output=True)
    os.chmod(tree, 0o777)
    marker = os.path.join(os.path.dirname(project), "ran-as-root")
    hook = os.path.join(tree, "hook.sh")
    with open(hook, "w") as fh:
        fh.write(f'#!/bin/sh\ntouch "{marker}"\n')
    os.chmod(hook, 0o755)
    subprocess.run(["git", "-C", tree, "config", "core.fsmonitor", hook], check=True)
    agent_context_budget.git_facts(tree)
    assert not os.path.exists(marker)


def test_a_new_root_git_site_has_to_be_guarded():
    """Fullcycle runs through the host, as the agent. The rest are behind runtime.engine.run,
    which calls require_root_work before any of them."""
    behind_a_caller = {"core/git_bridge.py", "core/backup_engine.py",
                       "runtime/context_builder.py", "core/fullcycle/engine.py"}
    found = set()
    for base in ("core", "runtime"):
        for path in (ROOT / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r'\[\s*"git"\s*,|which\("git"\)', text) and "require_root_work" not in text:
                found.add(str(path.relative_to(ROOT)))
    assert found <= behind_a_caller, found - behind_a_caller


# ── the tree an agent gets has to be its own ────────────────────────────────
@real
def test_a_shared_root_tree_is_not_usable(sandbox):
    t, project, _ = sandbox
    tree = os.path.join(os.path.dirname(project), "shared")
    os.mkdir(tree)
    os.chmod(tree, 0o777)
    assert priv.can_use_dir(t, tree) is False


@real
def test_an_agent_tree_is_usable_with_and_without_git(sandbox):
    t, project, _ = sandbox
    assert priv.can_use_dir(t, project) is True
    git = os.path.join(project, ".git")
    os.mkdir(git)
    os.chown(git, t.uid, t.gid)
    assert priv.can_use_dir(t, project) is True


@real
@pytest.mark.parametrize("kind", ["root_owned", "gitfile", "symlink"])
def test_a_git_dir_that_is_not_the_agents_own_is_not_usable(sandbox, kind):
    t, project, _ = sandbox
    git = os.path.join(project, ".git")
    if kind == "root_owned":
        os.mkdir(git)
    elif kind == "gitfile":
        with open(git, "w") as fh:
            fh.write("gitdir: /elsewhere\n")
        os.chown(git, t.uid, t.gid)
    else:
        os.symlink("/tmp", git)
        os.chown(git, t.uid, t.gid, follow_symlinks=False)
    assert priv.can_use_dir(t, project) is False


@real
def test_resume_refuses_a_shared_root_tree(resume, sandbox):
    _, project, _ = sandbox
    tree = os.path.join(os.path.dirname(project), "shared")
    os.mkdir(tree)
    os.chmod(tree, 0o777)
    r = ac.agent_resume(tree, session_name="uid-s")
    assert r["refused"] == "agent_run_as:project_not_usable" and _started(resume) == []


def _recover(monkeypatch, tree):
    from core import agent_creation_policy as acp
    from core import session_recovery as sr
    import core.continuation_governor as cg
    monkeypatch.setattr(sr, "control_probe",
                        lambda: {"healthy": True, "reachable": True, "reason": "ok"})
    monkeypatch.setattr(sr, "pane_state", lambda t: {"missing": True, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda t, n=30: "")
    monkeypatch.setattr(sr, "panes", lambda: [])
    monkeypatch.setattr(sr, "verify_recovered", lambda t, c: {"ok": True, "checks": {}})
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": True, "task_id": "t", "reason": "active_task"})
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(acp, "authorize", lambda *a, **k: {"ok": True, "reason": "test"})
    monkeypatch.setattr(acp, "reserve", lambda *a, **k: None)
    reg = {"sessions": {"p:0.0": {"target": "p:0.0", "session": "p", "cwd": tree,
                                  "conversation_id": "c" * 8, "enabled": True}},
           "limits": {"max_recoveries_per_target": 3, "window_secs": 60,
                      "backoff_base_secs": 0}}
    calls = []
    res = sr.recover("p:0.0", registry=reg, sleep=lambda s: None, live_cwds=[],
                     run_fn=lambda a: calls.append(list(a)) or (0, "", ""))
    return res, calls


@real
def test_recovery_refuses_a_shared_root_tree(sandbox, monkeypatch):
    _, project, _ = sandbox
    tree = os.path.join(os.path.dirname(project), "shared")
    os.mkdir(tree)
    os.chmod(tree, 0o777)
    res, calls = _recover(monkeypatch, tree)
    assert res["reason"] == "agent_run_as:project_not_usable" and calls == []


@real
def test_recovery_still_starts_in_the_agents_own_tree(sandbox, monkeypatch):
    t, project, _ = sandbox
    res, calls = _recover(monkeypatch, project)
    assert "agent_run_as" not in str(res.get("reason"))
    assert len(calls) == 1 and f"--reuid={t.uid}" in " ".join(calls[0])
