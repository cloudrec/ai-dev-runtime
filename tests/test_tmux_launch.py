"""A tmux server Owner OS starts must not live in Owner OS's own service cgroup — the
2026-09-22 incident.

The incident in one line: `tmux new-session -d` run from inside ai-runtime.service forks
the server into the service's cgroup, so when needrestart restarted the daemon after an
unattended upgrade (06:20:47) systemd SIGTERMed the server and every managed pane ended
in the same second; the replacement server, spawned the same way, landed in
`/system.slice/ai-runtime.service` again.

These tests pin both halves: `new-session` is launched through `systemd-run --scope`
exactly when the caller is inside a service (nothing else is touched), and health names
a live server that still shares the daemon's cgroup.
"""
import subprocess
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import agent_control as ac  # noqa: E402
from core import session_recovery as sr  # noqa: E402
from core import tmux_control as tc  # noqa: E402
from core import tmux_launch as tl  # noqa: E402

V2_SERVICE = "0::/system.slice/ai-runtime.service\n"
V2_SCOPE = "0::/system.slice/owner-os-tmux-1790050000-123.scope\n"
V2_SSH = "0::/user.slice/user-0.slice/session-42.scope\n"
V1_SERVICE = ("12:pids:/system.slice/ai-runtime.service\n"
              "1:name=systemd:/system.slice/ai-runtime.service\n"
              "0::/system.slice/ai-runtime.service\n")
V1_MIXED = ("12:pids:/system.slice/ai-runtime.service\n"
            "1:name=systemd:/user.slice/user-0.slice/session-42.scope\n")


# ── which cgroup am I in ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text,unit", [
    (V2_SERVICE, "ai-runtime.service"),
    (V1_SERVICE, "ai-runtime.service"),
    (V2_SCOPE, None),          # already a scope: a restart does not reach it
    (V2_SSH, None),            # an ssh session: same
    (V1_MIXED, None),          # only the name=systemd hierarchy names the unit
    ("", None),
    ("garbage\n", None),
])
def test_service_unit_is_read_only_from_the_systemd_hierarchy(text, unit):
    assert tl.service_unit_from_cgroup(text) == unit


def test_own_service_unit_reads_the_file_and_tolerates_its_absence(tmp_path):
    f = tmp_path / "cgroup"
    f.write_text(V2_SERVICE)
    assert tl.own_service_unit(str(f)) == "ai-runtime.service"
    assert tl.own_service_unit(str(tmp_path / "missing")) is None


# ── the prefix ───────────────────────────────────────────────────────────────

def _cg(tmp_path, text):
    f = tmp_path / "cgroup"
    f.write_text(text)
    return str(f)


def test_inside_a_service_new_session_goes_through_a_transient_scope(tmp_path):
    prefix = tl.detach_prefix(cgroup_file=_cg(tmp_path, V2_SERVICE),
                              which=lambda n: "/usr/bin/systemd-run", unit_name="u1")
    assert prefix[:2] == ["/usr/bin/systemd-run", "--scope"]
    assert "--collect" in prefix          # the scope unloads itself once empty
    assert "--quiet" in prefix            # nothing extra on stderr for callers to misread
    assert prefix[prefix.index("--unit") + 1] == "u1"


def test_outside_a_service_nothing_is_prefixed(tmp_path):
    for text in (V2_SCOPE, V2_SSH, ""):
        assert tl.detach_prefix(cgroup_file=_cg(tmp_path, text),
                                which=lambda n: "/usr/bin/systemd-run") == []


def test_without_systemd_run_the_command_is_left_exactly_as_it_was(tmp_path):
    assert tl.detach_prefix(cgroup_file=_cg(tmp_path, V2_SERVICE), which=lambda n: None) == []


def test_generated_scope_names_are_unique_per_call(tmp_path):
    cg = _cg(tmp_path, V2_SERVICE)
    a = tl.detach_prefix(cgroup_file=cg, which=lambda n: "/x")
    b = tl.detach_prefix(cgroup_file=cg, which=lambda n: "/x")
    name = lambda p: p[p.index("--unit") + 1]  # noqa: E731
    assert name(a).startswith("owner-os-tmux-") and name(a) != name(b)


def test_only_new_session_is_wrapped():
    prefix = lambda: ["/usr/bin/systemd-run", "--scope"]  # noqa: E731
    assert tl.wrap_argv(["tmux", "new-session", "-d", "-s", "x", "claude"], prefix=prefix) == \
        ["/usr/bin/systemd-run", "--scope", "tmux", "new-session", "-d", "-s", "x", "claude"]
    for argv in (["tmux", "list-panes", "-a"], ["tmux", "send-keys", "-t", "x", "hi"],
                 ["tmux", "respawn-pane", "-k", "-t", "x:0.0", "claude"], ["tmux"]):
        assert tl.wrap_argv(argv, prefix=prefix) == argv


# ── both spawn seams use it ──────────────────────────────────────────────────

class _Proc:
    returncode, stdout, stderr = 0, b"", b""


def _capture(monkeypatch):
    seen = []

    def fake_run(argv, *a, **k):
        seen.append(list(argv))
        return _Proc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_agent_control_starts_sessions_outside_its_own_cgroup(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(ac, "_tmux_bin", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(tl, "detach_prefix", lambda: ["/usr/bin/systemd-run", "--scope"])
    ac._tmux(["new-session", "-d", "-s", "s", "-c", "/opt/x", "claude"])
    ac._tmux(["list-panes", "-a"])
    assert seen[0][:3] == ["/usr/bin/systemd-run", "--scope", "/usr/bin/tmux"]
    assert seen[1] == ["/usr/bin/tmux", "list-panes", "-a"]


def test_session_recovery_recreates_sessions_outside_its_own_cgroup(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(tl, "detach_prefix", lambda: ["/usr/bin/systemd-run", "--scope"])
    sr._tmux(["new-session", "-d", "-s", "s", "-c", "/opt/x", "claude --resume abc"])
    sr._tmux(["respawn-pane", "-k", "-t", "s:0.0", "-c", "/opt/x", "claude"])
    assert seen[0][:3] == ["/usr/bin/systemd-run", "--scope", "tmux"]
    assert seen[1][0] == "tmux" and "systemd-run" not in seen[1]


# ── health names a fleet that is one daemon restart from dying ───────────────

def test_health_flags_a_server_that_shares_the_daemons_service(monkeypatch):
    monkeypatch.setattr(tc, "probe", lambda: {"reachable": True, "healthy": True,
                                              "split_brain": False, "reason": "ok"})
    monkeypatch.setattr(tc, "_tmux", lambda a: (0, "131368\n", ""))
    monkeypatch.setattr(tl, "unit_of_pid", lambda pid: "ai-runtime.service")
    monkeypatch.setattr(tl, "own_service_unit", lambda: "ai-runtime.service")
    h = tc.health()
    assert h["status"] == "fleet_in_daemon_cgroup"
    assert h["server_pid"] == 131368 and h["shared_cgroup"] is True
    assert "131368" in h["warning"] and "restart" in h["warning"]


def test_health_stays_green_when_the_server_has_its_own_scope(monkeypatch):
    monkeypatch.setattr(tc, "probe", lambda: {"reachable": True, "healthy": True,
                                              "split_brain": False, "reason": "ok"})
    monkeypatch.setattr(tc, "_tmux", lambda a: (0, "4242\n", ""))
    monkeypatch.setattr(tl, "unit_of_pid", lambda pid: None)       # a .scope, not a service
    monkeypatch.setattr(tl, "own_service_unit", lambda: "ai-runtime.service")
    h = tc.health()
    assert h["status"] == "ok" and h["shared_cgroup"] is False and "warning" not in h


def test_risk_check_never_guesses_a_pid_it_could_not_read():
    for reply in ((1, "", "no server running"), (0, "not-a-pid\n", ""), (0, "", "")):
        r = tc.server_cgroup_risk(run=lambda a, reply=reply: reply)
        assert r == {"server_pid": None, "server_unit": None, "own_unit": None,
                     "shared_cgroup": False}


def test_unreachable_health_does_not_ask_the_server_for_its_pid(monkeypatch):
    monkeypatch.setattr(tc, "probe", lambda: {"reachable": False, "healthy": False,
                                              "split_brain": False, "reason": "no_server"})
    monkeypatch.setattr(tc, "_tmux", lambda a: pytest.fail("round-trip attempted"))
    assert tc.health()["status"] == "unreachable"
