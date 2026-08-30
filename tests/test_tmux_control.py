"""Guard for the tmux control plane — the 2026-08-30 socket-loss incident.

The incident in one line: `/tmp/tmux-0` was deleted by a generic "nothing modified in
48 h" /tmp cleaner (a unix socket's mtime is stamped at bind() and never moves), the
tmux server survived, already-attached clients kept working, every new connect() failed
for 100 minutes, and managed-agent health reported ok the whole time.

These tests pin the two halves of the fix: reachability is classified honestly, and the
only repair that exists refuses everything except the exact repairable case.
"""
import os
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import tmux_control as tc  # noqa: E402


# ── recorded kernel output ───────────────────────────────────────────────────
# Real /proc/net/unix shape. Flags 00010000 = SO_ACCEPTCON (listening); the 0x0
# rows are ordinary connected client sockets on the same path and must not count.
_HEADER = "Num       RefCount Protocol Flags    Type St Inode Path\n"
_LISTEN = "ffff000000000001: 00000002 00000000 00010000 0001 01 111111 {path}\n"
_LISTEN2 = "ffff000000000002: 00000002 00000000 00010000 0001 01 222222 {path}\n"
_CONNECTED = "ffff000000000003: 00000003 00000000 00000000 0001 03 333333 {path}\n"
_OTHER = "ffff000000000004: 00000002 00000000 00010000 0001 01 444444 /run/other.sock\n"

PATH = "/tmp/tmux-0/default"


def _net(*rows):
    return _HEADER + "".join(r.format(path=PATH) for r in rows)


def _run_ok(args):
    return (0, "session-a\nsession-b\n", "")


def _run_socket_gone(args):
    return (1, "", f"error connecting to {PATH} (No such file or directory)")


def _run_no_server(args):
    return (1, "", f"no server running on {PATH}")


# ── parse_listeners ──────────────────────────────────────────────────────────
def test_parse_listeners_counts_only_listening_sockets_on_this_path():
    rows = tc.parse_listeners(_net(_LISTEN, _CONNECTED, _OTHER), PATH)
    assert [r["inode"] for r in rows] == ["111111"]


def test_parse_listeners_sees_an_orphaned_server_still_bound_to_the_path():
    """The kernel keeps a socket's original name after unlink, which is the only way to
    see the second server the incident produced."""
    rows = tc.parse_listeners(_net(_LISTEN, _LISTEN2, _CONNECTED), PATH)
    assert len(rows) == 2


# ── probe ────────────────────────────────────────────────────────────────────
def test_probe_healthy_on_a_real_round_trip():
    p = tc.probe(run=_run_ok, path=PATH, resolve_pids=False,
                 net_unix=lambda: _net(_LISTEN))
    assert p["reachable"] and p["healthy"] and p["reason"] == "ok"
    assert p["split_brain"] is False


def test_probe_classifies_a_deleted_socket_as_socket_missing():
    p = tc.probe(run=_run_socket_gone, path=PATH, resolve_pids=False,
                 net_unix=lambda: _net(_LISTEN))
    assert p["reachable"] is False and p["reason"] == "socket_missing"
    assert p["healthy"] is False


def test_probe_treats_no_server_running_as_socket_missing_when_the_file_is_gone(tmp_path):
    """tmux says 'no server running' for a missing socket too. Only the filesystem can
    tell them apart, and the difference decides whether starting a server is safe."""
    gone = str(tmp_path / "tmux-0" / "default")
    p = tc.probe(run=_run_no_server, path=gone, resolve_pids=False,
                 net_unix=lambda: "")
    assert p["reason"] == "socket_missing"


def test_probe_reports_no_server_when_the_socket_file_is_actually_there(tmp_path):
    present = tmp_path / "default"
    present.write_text("")
    p = tc.probe(run=_run_no_server, path=str(present), resolve_pids=False,
                 net_unix=lambda: "")
    assert p["reason"] == "no_server"


def test_probe_flags_split_brain_and_refuses_to_call_it_healthy():
    """Two servers on one path: reachable, but part of the fleet is invisible. This is
    the state the incident actually left behind (a duplicate live agent on a project
    that already had one), so 'reachable' alone must never mean healthy."""
    p = tc.probe(run=_run_ok, path=PATH, resolve_pids=False,
                 net_unix=lambda: _net(_LISTEN, _LISTEN2))
    assert p["reachable"] is True
    assert p["split_brain"] is True
    assert p["healthy"] is False
    assert p["reason"] == "split_brain"


def test_probe_classifies_a_missing_binary_and_a_hung_server_separately():
    assert tc.probe(run=lambda a: (127, "", "tmux is not installed"), path=PATH,
                    resolve_pids=False, net_unix=lambda: "")["reason"] == "tmux_missing"
    assert tc.probe(run=lambda a: (124, "", "tmux timed out"), path=PATH,
                    resolve_pids=False, net_unix=lambda: "")["reason"] == "timeout"


# ── health surface ───────────────────────────────────────────────────────────
def test_health_status_is_never_ok_while_the_plane_is_unreachable(monkeypatch):
    monkeypatch.setattr(tc, "probe", lambda: {"reachable": False, "healthy": False,
                                              "reason": "socket_missing",
                                              "socket_path": PATH, "listeners": 1})
    h = tc.health()
    assert h["status"] == "unreachable"
    assert "UNKNOWN" in h["warning"]


# ── repair: every precondition is a refusal ──────────────────────────────────
def _repair(monkeypatch, *, probe, kill=None, run=None, conn=None):
    monkeypatch.setattr(tc, "_log", lambda *a, **k: None)
    return tc.repair(probe_fn=lambda: probe, kill_fn=kill or (lambda p, s: None),
                     run=run or _run_ok, sleep=lambda s: None)


def test_repair_refuses_when_the_plane_is_already_reachable(monkeypatch):
    r = _repair(monkeypatch, probe={"reachable": True, "healthy": True, "reason": "ok",
                                    "socket_path": PATH, "listeners": 1,
                                    "listener_pids": [10]})
    assert r["repaired"] is False and r["reason"] == "already_reachable"


def test_repair_never_starts_a_server_when_none_survived(monkeypatch):
    """The whole reason the incident produced a duplicate agent: a client that could not
    reach the plane started a new server. This repair may never do that."""
    r = _repair(monkeypatch, probe={"reachable": False, "healthy": False,
                                    "reason": "socket_missing", "socket_path": PATH,
                                    "listeners": 0, "listener_pids": []})
    assert r["repaired"] is False and r["reason"] == "no_surviving_server"


def test_repair_refuses_a_split_plane_because_the_fix_would_kill_live_agents(monkeypatch):
    r = _repair(monkeypatch, probe={"reachable": False, "healthy": False,
                                    "reason": "socket_missing", "socket_path": PATH,
                                    "listeners": 2, "listener_pids": [10, 11]})
    assert r["repaired"] is False and r["reason"] == "multiple_servers_bound"


def test_repair_refuses_a_failure_class_it_does_not_understand(monkeypatch):
    """A hung server must not be signalled on a guess."""
    r = _repair(monkeypatch, probe={"reachable": False, "healthy": False,
                                    "reason": "timeout", "socket_path": PATH,
                                    "listeners": 1, "listener_pids": [10]})
    assert r["repaired"] is False and r["reason"].startswith("not_repairable:timeout")


def test_repair_refuses_to_signal_a_pid_that_is_not_a_tmux_server(monkeypatch):
    """SIGUSR1's default disposition is TERMINATE. Signalling a recycled pid, or a tmux
    *client*, kills it."""
    monkeypatch.setattr(tc, "is_tmux_server", lambda pid, **k: False)
    monkeypatch.setattr(tc, "_proc_cmdline", lambda pid: "/usr/bin/python3 something")
    r = _repair(monkeypatch, probe={"reachable": False, "healthy": False,
                                    "reason": "socket_missing", "socket_path": PATH,
                                    "listeners": 1, "listener_pids": [4242]})
    assert r["repaired"] is False and r["reason"] == "pid_is_not_a_tmux_server"


def test_repair_signals_the_surviving_server_and_proves_it_is_the_same_one(monkeypatch, tmp_path):
    """The success path. `reachable again` is not sufficient proof: if a race started a
    NEW server the path would answer while every original session sat orphaned behind
    it, so the repaired socket must lead back to the pid we signalled."""
    monkeypatch.setattr(tc, "is_tmux_server", lambda pid, **k: True)
    sock = str(tmp_path / "tmux-0" / "default")
    down = {"reachable": False, "healthy": False, "reason": "socket_missing",
            "socket_path": sock, "listeners": 1, "listener_pids": [777]}
    up = {"reachable": True, "healthy": True, "reason": "ok", "socket_path": sock,
          "listeners": 1, "listener_pids": [777]}
    seen = {"signals": []}
    states = [down, up]

    def probe_fn():
        return states.pop(0) if len(states) > 1 else states[0]

    monkeypatch.setattr(tc, "_log", lambda *a, **k: None)
    r = tc.repair(probe_fn=probe_fn, run=lambda a: (0, "777", ""),
                  kill_fn=lambda p, s: seen["signals"].append((p, s)),
                  sleep=lambda s: None)
    assert r["repaired"] is True
    assert seen["signals"] == [(777, __import__("signal").SIGUSR1)]
    assert r["sessions_preserved"] is True and r["serving_pid"] == "777"
    # the socket directory the cleaner took is recreated, owner-only, and nothing else
    assert os.path.isdir(os.path.dirname(sock))
    assert oct(os.stat(os.path.dirname(sock)).st_mode)[-3:] == "700"


def test_repair_fails_loudly_when_the_socket_answers_from_a_different_server(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "is_tmux_server", lambda pid, **k: True)
    monkeypatch.setattr(tc, "_log", lambda *a, **k: None)
    sock = str(tmp_path / "tmux-0" / "default")
    up = {"reachable": True, "healthy": True, "reason": "ok", "socket_path": sock,
          "listeners": 1, "listener_pids": [777]}
    down = {"reachable": False, "healthy": False, "reason": "socket_missing",
            "socket_path": sock, "listeners": 1, "listener_pids": [777]}
    states = [down, up]
    r = tc.repair(probe_fn=lambda: states.pop(0) if len(states) > 1 else states[0],
                  run=lambda a: (0, "999", ""),          # a DIFFERENT server answered
                  kill_fn=lambda p, s: None, sleep=lambda s: None)
    assert r["repaired"] is False and r["reason"] == "server_identity_changed"


def test_repair_dry_run_changes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(tc, "is_tmux_server", lambda pid, **k: True)
    monkeypatch.setattr(tc, "_log", lambda *a, **k: None)
    sock = str(tmp_path / "tmux-0" / "default")
    killed = []
    r = tc.repair(dry_run=True, probe_fn=lambda: {
        "reachable": False, "healthy": False, "reason": "socket_missing",
        "socket_path": sock, "listeners": 1, "listener_pids": [777]},
        run=_run_ok, kill_fn=lambda p, s: killed.append(p), sleep=lambda s: None)
    assert r["repaired"] is False and r["reason"] == "dry_run"
    assert killed == [] and not os.path.isdir(os.path.dirname(sock))


# ── guard ────────────────────────────────────────────────────────────────────
def test_guard_emits_an_owner_action_event_when_it_cannot_repair(monkeypatch):
    """The blackout produced log lines and nothing else. A control plane that is down
    must be a durable, wake-capable event."""
    emitted = {}

    def fake_emit(source, etype, **kw):
        emitted.update({"source": source, "type": etype, **kw})
        return {"event_id": 4242}

    r = tc.guard(auto_repair=False, emit_fn=fake_emit,
                 probe_fn=lambda: {"reachable": False, "healthy": False,
                                   "reason": "socket_missing", "socket_path": PATH,
                                   "listeners": 0, "listener_pids": []})
    assert r["event_id"] == 4242
    assert emitted["type"] == "agent_control_plane_unreachable"
    assert emitted["severity"] == "critical" and emitted["owner_action_required"] is True


def test_guard_reports_a_self_heal_without_waking_anyone(monkeypatch):
    emitted = {}

    def fake_emit(source, etype, **kw):
        emitted.update({"type": etype, **kw})
        return {"event_id": 7}

    r = tc.guard(emit_fn=fake_emit, auto_repair=True,
                 probe_fn=lambda: {"reachable": False, "healthy": False,
                                   "reason": "socket_missing", "socket_path": PATH,
                                   "listeners": 1, "listener_pids": [5]},
                 repair_fn=lambda: {"repaired": True, "reason": "socket_rebound_by_sigusr1",
                                    "pid": 5, "probe": {"reachable": True, "healthy": True,
                                                        "reason": "ok"}})
    assert emitted["type"] == "agent_control_plane_recovered"
    assert emitted["owner_action_required"] is False and emitted["push"] is False
    assert r["repair"]["repaired"] is True


def test_guard_emits_split_brain_as_owner_action_and_never_repairs_it():
    emitted = {}

    def fake_emit(source, etype, **kw):
        emitted.update({"type": etype, **kw})
        return {"event_id": 9}

    tried = []
    tc.guard(emit_fn=fake_emit, auto_repair=True,
             repair_fn=lambda: tried.append(1),
             probe_fn=lambda: {"reachable": True, "healthy": False, "split_brain": True,
                               "reason": "split_brain", "socket_path": PATH,
                               "listeners": 2, "listener_pids": [1, 2]})
    assert emitted["type"] == "agent_control_plane_split"
    assert emitted["owner_action_required"] is True
    assert tried == []


def test_guard_is_silent_and_cheap_when_the_plane_is_healthy():
    calls = []
    r = tc.guard(emit_fn=lambda *a, **k: calls.append(1),
                 probe_fn=lambda: {"reachable": True, "healthy": True, "reason": "ok"})
    assert r["event_id"] is None and calls == []


def test_guard_survives_an_emitter_that_is_itself_down():
    """Detection must never die on reporting — the notification channel has been dead
    for this whole database's life."""
    def boom(*a, **k):
        raise RuntimeError("outbox unavailable")

    r = tc.guard(auto_repair=False, emit_fn=boom,
                 probe_fn=lambda: {"reachable": False, "healthy": False,
                                   "reason": "socket_missing", "socket_path": PATH,
                                   "listeners": 0})
    assert "emit_error" in r
