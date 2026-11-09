"""What a dropped agent can see: AGENT_SANDBOX_KEEP.

A uid alone hid little on this host: /opt is world-readable, payment code and .env files
included. With the setting, every drop (the pane, the engine's git and checks) runs in a
private mount namespace where /opt, /home, /tmp and the rest of SANDBOX_HIDE are empty,
except the kept paths. These run the real unshare, mount and setpriv as root; the mounts
live and die inside the test's own namespace.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile

import pytest

from core import agent_privilege as priv

real = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("setpriv")
                          or not shutil.which("unshare"), reason="needs root, setpriv, unshare")


@pytest.fixture
def box(monkeypatch):
    base = tempfile.mkdtemp(prefix="agent-sbx-", dir="/tmp")
    os.chmod(base, 0o755)
    n = pwd.getpwnam("nobody")
    home, keep = os.path.join(base, "home"), os.path.join(base, "projects")
    os.makedirs(os.path.join(keep, "p1"))
    os.makedirs(home)
    for d in (home, keep, os.path.join(keep, "p1")):
        os.chown(d, n.pw_uid, n.pw_gid)
    with open(os.path.join(keep, "p1", "app.py"), "w") as f:
        f.write("x = 1\n")
    t = priv.Target("nobody", n.pw_uid, n.pw_gid, home)
    monkeypatch.setenv("AGENT_RUN_AS", "nobody")
    monkeypatch.setenv("AGENT_CONTROL_DB", os.path.join(base, "agent_control.db"))
    monkeypatch.setenv("AGENT_SANDBOX_KEEP", keep)
    monkeypatch.setattr(priv, "_lookup", lambda name: t)
    yield t, base, keep
    shutil.rmtree(base, ignore_errors=True)


def _run(t, script, cwd="/"):
    return subprocess.run(priv.host_argv(t, ["/bin/sh", "-c", script]), cwd=cwd,
                          env=priv.child_env(t), capture_output=True, text=True, timeout=30)


# ── the setting itself ──────────────────────────────────────────────────────────────
def test_off_by_default_adds_nothing(box, monkeypatch):
    t, _, _ = box
    monkeypatch.delenv("AGENT_SANDBOX_KEEP")
    assert priv.sandbox_prefix(t) == []
    assert priv.host_argv(t, ["true"])[0].endswith("setpriv")


def test_on_every_drop_enters_the_namespace_first(box):
    t, _, keep = box
    for argv in (priv.host_argv(t, ["true"]), priv.pane_prefix(t)):
        assert argv[0].endswith("unshare") and "--mount" in argv
        assert argv.index("--") < argv.index(priv._setpriv_bin())
    assert keep in priv.sandbox_prefix(t) and t.home in priv.sandbox_prefix(t)


@pytest.mark.parametrize("bad", ["/", "/opt", "/home", "/tmp", "relative", "/a b",
                                 "/x/../opt", "/does/not/exist", "/opt/x;rm"])
def test_unsafe_keep_paths_refuse(box, monkeypatch, bad):
    t, _, _ = box
    monkeypatch.setenv("AGENT_SANDBOX_KEEP", bad)
    with pytest.raises(priv.AgentPrivilegeError):
        priv.host_argv(t, ["true"])


def test_a_symlinked_keep_path_refuses(box, monkeypatch):
    t, base, keep = box
    link = os.path.join(base, "link")
    os.symlink(keep, link)
    monkeypatch.setenv("AGENT_SANDBOX_KEEP", link)
    with pytest.raises(priv.AgentPrivilegeError, match="sandbox_keep_unsafe"):
        priv.host_argv(t, ["true"])


def test_no_unshare_refuses_rather_than_running_unsandboxed(box, monkeypatch):
    t, _, _ = box
    real_which = shutil.which
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n, path=None: None if n == "unshare" else real_which(n, path=path))
    with pytest.raises(priv.AgentPrivilegeError, match="sandbox_unavailable"):
        priv.pane_prefix(t)


# ── what the agent actually sees ────────────────────────────────────────────────────
@real
def test_the_agent_sees_its_projects_and_home_and_nothing_else_of_opt(box):
    t, base, keep = box
    assert os.listdir("/opt")                       # the host's /opt is not empty
    r = _run(t, "id -u; ls -A /opt | tr '\\n' ' '; echo; ls -A /home | tr '\\n' ' '; echo;"
                f" cat {keep}/p1/app.py; echo y > {keep}/p1/new.py && echo wrote;"
                " ls -A /root 2>&1 | head -1; ls -A /var/www 2>/dev/null | wc -l")
    out = r.stdout.splitlines()
    assert r.returncode == 0, r.stderr
    assert out[0] == str(t.uid)
    assert out[1].strip() == "" or out[1].split() == [os.path.relpath(keep, "/opt").split("/")[0]]
    assert "x = 1" in out and "wrote" in out
    assert out[-1] == "0"
    assert os.path.exists(os.path.join(keep, "p1", "new.py"))   # the write is real


@real
def test_payment_like_dirs_under_opt_are_invisible(box, monkeypatch):
    t, _, keep = box
    r = _run(t, "test -e /opt/payment-orchestrator && echo SEEN || echo HIDDEN;"
                " find / -xdev -path /proc -prune -o -name '.env' -print 2>/dev/null"
                " | grep -c '^/opt/' || true")
    assert r.stdout.split()[0] == "HIDDEN"
    assert r.stdout.split()[-1] == "0"


@real
def test_tmp_is_private_and_the_host_is_untouched(box):
    t, base, keep = box
    marker = tempfile.mkstemp(prefix="host-only-", dir="/tmp")[1]
    os.chmod(marker, 0o644)
    try:
        r = _run(t, f"test -e {marker} && echo SEEN || echo HIDDEN; touch /tmp/sbx-mark;"
                    " echo ok")
        assert r.stdout.split()[:2] == ["HIDDEN", "ok"], r.stderr
        assert not os.path.exists("/tmp/sbx-mark")
        mounts = open("/proc/self/mounts").read()
        assert "agent-sandbox" not in mounts
    finally:
        os.unlink(marker)


@real
def test_the_working_directory_survives_the_new_mounts(box):
    t, _, keep = box
    r = _run(t, "pwd; ls", cwd=os.path.join(keep, "p1"))
    assert r.stdout.splitlines()[0] == os.path.join(keep, "p1"), r.stderr
    assert "app.py" in r.stdout


@real
def test_the_final_process_is_the_agent_not_root(box):
    """pane_problem looks at the pane's pid: the namespace is left by exec, not a fork."""
    t, _, keep = box
    r = _run(t, "grep '^Uid:' /proc/$$/status; grep -c . /proc/$$/cmdline")
    assert r.stdout.split()[1:5] == [str(t.uid)] * 4, r.stdout


# ── this instance's own tmux server ─────────────────────────────────────────────────
def _argv_of(monkeypatch, args, sock=None, scope=True):
    from core import agent_control as ac
    from core import tmux_launch
    seen = []
    if sock is None:
        monkeypatch.delenv("AGENT_TMUX_SOCKET", raising=False)
    else:
        monkeypatch.setenv("AGENT_TMUX_SOCKET", sock)
    monkeypatch.setattr(ac, "_tmux_bin", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(tmux_launch, "detach_prefix",
                        lambda: ["systemd-run", "--scope"] if scope else [])

    class P:
        returncode, stdout, stderr = 0, b"", b""
    monkeypatch.setattr(ac.subprocess, "run", lambda argv, **k: seen.append(argv) or P())
    ac._tmux(args)
    return seen[0]


def test_no_socket_leaves_tmux_argv_alone(monkeypatch):
    assert _argv_of(monkeypatch, ["list-panes", "-a"]) == ["/usr/bin/tmux", "list-panes", "-a"]


def test_the_socket_goes_right_after_the_binary(monkeypatch):
    assert _argv_of(monkeypatch, ["list-panes", "-a"], "owneros-fc") == \
        ["/usr/bin/tmux", "-L", "owneros-fc", "list-panes", "-a"]


def test_a_new_server_still_gets_its_own_scope(monkeypatch):
    argv = _argv_of(monkeypatch, ["new-session", "-d", "-s", "fc-x"], "owneros-fc")
    assert argv[:2] == ["systemd-run", "--scope"]
    assert argv[2:6] == ["/usr/bin/tmux", "-L", "owneros-fc", "new-session"]


@pytest.mark.parametrize("bad", ["Fc", "a b", "-x", "a/b", "x" * 40, "../s"])
def test_a_bad_socket_name_refuses(monkeypatch, bad):
    from core import agent_control as ac
    with pytest.raises(ac.AgentControlError):
        _argv_of(monkeypatch, ["list-panes"], bad)


@real
def test_a_pane_on_the_own_server_is_invisible_to_the_default_one(monkeypatch):
    from core import agent_control as ac
    name = f"sbx-{os.getpid()}"
    monkeypatch.setenv("AGENT_TMUX_SOCKET", "owneros-sbx-test")
    try:
        assert ac._tmux(["new-session", "-d", "-s", name, "sleep 30"])[0] == 0
        assert ac._tmux(["has-session", "-t", f"={name}"])[0] == 0
        default = subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                                 capture_output=True)
        assert default.returncode != 0
    finally:
        ac._tmux(["kill-server"])


@real
def test_other_users_processes_and_their_arguments_are_invisible(box):
    t, _, _ = box
    sleeper = subprocess.Popen(["sleep", "61"])
    try:
        r = _run(t, f"test -e /proc/{sleeper.pid}/cmdline && echo SEEN || echo HIDDEN;"
                    " test -r /proc/1/cmdline && echo PID1 || echo NOPID1;"
                    " ls /proc | grep -c '^[0-9]'")
        seen, pid1, count = r.stdout.split()
        assert (seen, pid1) == ("HIDDEN", "NOPID1"), r.stderr
        assert int(count) < 10
        assert os.path.exists(f"/proc/{sleeper.pid}/cmdline")      # the host still sees it
    finally:
        sleeper.kill()


@real
def test_hidden_dirs_cannot_be_opened_and_tmp_is_writable(box):
    t, base, keep = box
    r = _run(t, "for d in /root /srv /var/www /opt; do test -r $d && echo $d:R || echo $d:-; done;"
                " ls /opt >/dev/null 2>&1 && echo LS || echo NOLS;"
                " echo hi > /tmp/a && cat /tmp/a; echo hi > /var/tmp/b && echo VT;"
                f" test -w {keep}/p1 && echo KEEPW")
    out = r.stdout.split()
    assert "/root:-" in out and "/srv:-" in out, r.stdout + r.stderr
    assert "hi" in out and "VT" in out and "KEEPW" in out


def test_hidden_modes():
    keep = ["/opt/fc-projects", "/home/fcagent"]
    assert priv._hidden_mode("/tmp", keep) == "1777"
    assert priv._hidden_mode("/var/tmp", keep) == "1777"
    assert priv._hidden_mode("/opt", keep) == "0711"
    assert priv._hidden_mode("/home", keep) == "0711"
    assert priv._hidden_mode("/root", keep) == "0700"
    assert priv._hidden_mode("/var/www", keep) == "0700"
    assert priv._hidden_mode("/op", ["/opt/x"]) == "0700"


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_an_own_server_that_does_not_exist_yet_is_an_empty_inventory(monkeypatch):
    """The sandboxed worker's first agent: before it, the instance's tmux server does not
    exist, and its inventory must read as empty, not as an error (2026-11-09: every tick
    failed and no agent was ever started)."""
    from core import agent_control as ac
    monkeypatch.setenv("AGENT_TMUX_SOCKET", f"owneros-never-{os.getpid()}")
    inv = ac.agent_list()
    assert inv["agents"] == [] and inv["tmux_running"] is False


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
def test_the_default_server_keeps_its_error_on_a_broken_socket(monkeypatch):
    from core import agent_control as ac
    monkeypatch.delenv("AGENT_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: (
        1, "", "error connecting to /tmp/tmux-0/default (No such file or directory)"))
    with pytest.raises(ac.AgentControlError):
        ac.agent_list()


def test_sandboxed_argv_refuses_without_a_keep_list(box):
    """The API runs an agent-side read with an explicit keep list; none means no sandbox,
    and that is a refusal, never a plain setpriv."""
    t, _, keep = box
    with pytest.raises(priv.AgentPrivilegeError, match="sandbox_keep_unset"):
        priv.sandboxed_argv(t, "", ["true"])
    argv = priv.sandboxed_argv(t, keep, ["true"])
    assert argv[0].endswith("unshare") and keep in argv and argv[-1] == "true"
