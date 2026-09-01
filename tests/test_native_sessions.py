"""The runtime's own view of its sessions, and the two inferences it replaces.

Owner OS had two defects that are the same mistake — inferring from outside a fact
the runtime already knows:

  * identity by CWD. `discovery` asked which conversation was newest in a directory,
    which is a per-DIRECTORY answer to a per-PANE question. Two agents in one cwd are
    then indistinguishable, which is how a pane that had genuinely died was labelled
    `renamed_from` of the pane that replaced it (event 18172), and why `8aba07f` had
    to widen an identity set instead of simply knowing it.
  * liveness by EVENT COUNT. `closed_loop_wake` decides an agent is stalled because
    nothing was recorded, but an agent in a long turn emits nothing while it works.
    `8aba07f` and `3d8d4bf` each subtract an exception from that proxy; neither
    replaces it.

`claude agents --json` answers both. These tests inject a listing — never the real
one, which `conftest` hard-disables, because it reports whatever the operator is
actually running.
"""
from __future__ import annotations

import pytest

from core import native_sessions as ns


LISTING = [
    {"kind": "interactive", "pid": 249416, "cwd": "/opt/gaika-extension",
     "sessionId": "cc43ebcf-6474-428f-a3e5-c034ba244e85",
     "name": "gaika-extension-72", "status": "busy"},
    {"kind": "interactive", "pid": 2018774, "cwd": "/opt/capacity",
     "sessionId": "bd058c50-c861-4aa0-bf58-a6ca33d1c99a",
     "name": "capacity-f8", "status": "idle"},
    {"kind": "background", "id": "3deb87db", "cwd": "/root",
     "sessionId": "3deb87db-e9a3-4965-874b-b7339fdd3807",
     "name": "context recovery", "state": "blocked"},
]


@pytest.fixture(autouse=True)
def _native(monkeypatch):
    monkeypatch.setattr(ns, "ENABLED", True)
    monkeypatch.setattr(ns, "_list_raw", lambda: list(LISTING))
    ns.reset_cache()
    yield
    ns.reset_cache()


# ── reading the listing ─────────────────────────────────────────────────────
def test_a_pid_resolves_to_the_runtimes_own_session_id():
    assert ns.session_id_for_pid(249416) == "cc43ebcf-6474-428f-a3e5-c034ba244e85"


def test_two_panes_in_one_directory_get_DIFFERENT_identities():
    """The whole point of asking the runtime: the cwd heuristic cannot do this."""
    monkey = list(LISTING) + [
        {"kind": "interactive", "pid": 999001, "cwd": "/opt/gaika-extension",
         "sessionId": "ffffffff-0000-0000-0000-000000000000", "status": "busy"}]
    ns.reset_cache()
    ns._list_raw = lambda: monkey                      # noqa: SLF001 — injection point
    a = ns.session_id_for_pid(249416)
    b = ns.session_id_for_pid(999001)
    assert a and b and a != b, "same cwd must not mean same identity"


def test_status_is_read_from_either_field_name():
    """Interactive rows carry `status`; background rows carry `state`."""
    assert ns.status_for_session("cc43ebcf-6474-428f-a3e5-c034ba244e85") == "busy"
    assert ns.status_for_session("3deb87db-e9a3-4965-874b-b7339fdd3807") == "blocked"


def test_a_truncated_session_id_still_matches():
    """Owner OS addresses hook-sourced agents as `session:<id[:12]>`, so a caller may
    only hold that much."""
    assert ns.status_for_session("cc43ebcf-647") == "busy"


def test_only_busy_counts_as_working():
    """`idle` is at rest, not progress. `blocked` is the very thing the callers are
    trying to tell apart, so it is never evidence against being stuck."""
    assert ns.is_working(pid=249416) is True
    assert ns.is_working(pid=2018774) is False
    assert ns.is_working("3deb87db-e9a3-4965-874b-b7339fdd3807") is False


# ── fail open, always ───────────────────────────────────────────────────────
def test_an_unreadable_listing_is_no_opinion_not_death(monkeypatch):
    def boom():
        raise OSError("no claude binary")
    monkeypatch.setattr(ns, "_list_raw", boom)
    ns.reset_cache()
    assert ns.sessions() == []
    assert ns.is_working(pid=249416) is False, "unknown is False, and False is not dead"


def test_malformed_output_is_no_opinion(monkeypatch):
    monkeypatch.setattr(ns, "_list_raw", lambda: {"not": "a list"})
    ns.reset_cache()
    assert ns.sessions() == []


def test_an_unknown_pid_or_session_yields_nothing():
    assert ns.session_id_for_pid(1) == ""
    assert ns.session_id_for_pid(None) == ""
    assert ns.status_for_session("") == ""
    assert ns.is_working("no-such-session") is False


def test_the_off_switch_silences_it_entirely(monkeypatch):
    monkeypatch.setattr(ns, "ENABLED", False)
    ns.reset_cache()
    assert ns.sessions() == [] and ns.is_working(pid=249416) is False


# ── cost control ────────────────────────────────────────────────────────────
def test_the_listing_is_cached_within_its_ttl(monkeypatch):
    """Measured 0.80-1.99 s per real call against a 20 s tick, with several callers
    per tick."""
    calls = []
    monkeypatch.setattr(ns, "_list_raw", lambda: calls.append(1) or list(LISTING))
    ns.reset_cache()
    ns.sessions(now=100.0)
    ns.sessions(now=100.0 + ns.TTL_SECS - 1)
    assert len(calls) == 1


def test_the_cache_expires():
    calls = []
    ns._list_raw = lambda: calls.append(1) or list(LISTING)   # noqa: SLF001
    ns.reset_cache()
    ns.sessions(now=100.0)
    ns.sessions(now=100.0 + ns.TTL_SECS + 1)
    assert len(calls) == 2


# ── step 1: discovery identifies a pane by asking, not guessing ─────────────

def _disc_agent(target, cwd, pid):
    return {"target": target, "session": target.split(":")[0], "is_agent": True,
            "alive": True, "claude_cwd": cwd, "pid": pid, "command": "claude"}


DISC_CONFIG = {"allowed_roots": ["/opt"], "sessions": {}}


def test_discovery_prefers_the_runtimes_session_id_over_the_cwd_guess(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import control_plane as cp
    from core.control_plane import discovery as disc

    disc.discover({"agents": [_disc_agent("gaika-opus:0.0", "/opt/gaika-extension",
                                          249416)]},
                  config=DISC_CONFIG,
                  conversation_fn=lambda cwd: "WRONG-cwd-derived-id")
    rec = cp.get_agent("gaika-opus:0.0")
    assert rec["conversation_id"] == "cc43ebcf-6474-428f-a3e5-c034ba244e85"


def test_two_panes_in_one_cwd_are_not_reconciled_as_a_rename(tmp_path, monkeypatch):
    """The event 18172 shape. The cwd heuristic hands both panes one id, so the
    second looks like the first renamed; the runtime gives them their own."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp2.db"))
    listing = list(LISTING) + [
        {"kind": "interactive", "pid": 999001, "cwd": "/opt/gaika-extension",
         "sessionId": "ffffffff-0000-0000-0000-000000000000", "status": "busy"}]
    monkeypatch.setattr(ns, "_list_raw", lambda: listing)
    ns.reset_cache()
    from core import control_plane as cp
    from core.control_plane import discovery as disc

    same_cwd = lambda cwd: "ONE-ID-FOR-THE-WHOLE-DIRECTORY"        # noqa: E731
    disc.discover({"agents": [_disc_agent("first:0.0", "/opt/gaika-extension", 249416)]},
                  config=DISC_CONFIG, conversation_fn=same_cwd)
    disc.discover({"agents": [_disc_agent("second:0.0", "/opt/gaika-extension", 999001)]},
                  config=DISC_CONFIG, conversation_fn=same_cwd)
    a, b = cp.get_agent("first:0.0"), cp.get_agent("second:0.0")
    assert a["conversation_id"] != b["conversation_id"]
    # `first` is legitimately marked dead — it is gone from the inventory. What must
    # NOT happen is the second pane being reconciled as the first one RENAMED, which
    # is what one shared cwd-derived id produces.
    from core.control_plane import cto
    ev = [e for e in cto.cto_brief_since("t")["events"]
          if e["type"] == "new_agent_discovered" and e["agent_id"] == "second:0.0"]
    assert ev and ev[0]["payload"].get("renamed_from") is None


def test_discovery_falls_back_to_the_cwd_guess_when_the_runtime_is_silent(tmp_path,
                                                                          monkeypatch):
    """Behaviour without the native view must be exactly what it was."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp3.db"))
    monkeypatch.setattr(ns, "ENABLED", False)
    ns.reset_cache()
    from core import control_plane as cp
    from core.control_plane import discovery as disc

    disc.discover({"agents": [_disc_agent("x:0.0", "/opt/thing", 4242)]},
                  config=DISC_CONFIG, conversation_fn=lambda cwd: "cwd-derived")
    assert cp.get_agent("x:0.0")["conversation_id"] == "cwd-derived"


# ── step 2: the watchdog asks whether the agent is working ─────────────────

def test_a_busy_agent_resolves_its_watch_instead_of_escalating(tmp_path, monkeypatch):
    """The state Part 53 could not account for: mid-turn, emitting nothing, alive."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp4.db"))
    from core.control_plane import api
    from core import closed_loop_wake as clw

    api.register_agent("gaika-opus:0.0", session="gaika-opus", cwd="/opt/gaika-extension",
                       pid=249416, conversation_id="cc43ebcf-6474-428f-a3e5-c034ba244e85")
    assert clw._resolution_reason(api._c(None)[0], event_id=1,
                                  target="gaika-opus:0.0") == "runtime_reports_agent_working"


def test_a_busy_agent_addressed_by_its_session_name_also_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp5.db"))
    from core.control_plane import api
    from core import closed_loop_wake as clw
    assert clw._resolution_reason(api._c(None)[0], event_id=1,
                                  target="session:cc43ebcf-647") == \
        "runtime_reports_agent_working"


def test_an_idle_agent_is_not_claimed_to_be_working(tmp_path, monkeypatch):
    """`idle` is at rest. Resolving on it would silence a genuinely stuck pane."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp6.db"))
    from core.control_plane import api
    from core import closed_loop_wake as clw
    api.register_agent("capacity:0.0", session="capacity", cwd="/opt/capacity",
                       pid=2018774, conversation_id="bd058c50-c861-4aa0-bf58-a6ca33d1c99a")
    assert clw._resolution_reason(api._c(None)[0], event_id=1,
                                  target="capacity:0.0") != "runtime_reports_agent_working"


def test_a_silent_runtime_never_resolves_a_watch(tmp_path, monkeypatch):
    """Fail open in the only direction that matters: no answer is not an answer."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp7.db"))
    monkeypatch.setattr(ns, "ENABLED", False)
    ns.reset_cache()
    from core.control_plane import api
    from core import closed_loop_wake as clw
    api.register_agent("gaika-opus:0.0", session="gaika-opus", cwd="/opt/gaika-extension",
                       pid=249416, conversation_id="cc43ebcf-6474-428f-a3e5-c034ba244e85")
    assert clw._resolution_reason(api._c(None)[0], event_id=1,
                                  target="gaika-opus:0.0") is None
