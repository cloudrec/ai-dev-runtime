"""A crash alert may only clear on durable evidence that THIS session is alive again.

89 critical `agent_process_failed` alerts were open on 2026-09-13, many for panes that
died days earlier and whose work had resumed under a new name. The obvious rule —
retire when another agent shares the conversation id — is a regression, and the live
host proved it: `hostsecure-clean:0.0` runs in `/opt/hostsecure` with no runtime session
id, so the per-directory fallback hands it `08c1a936-…`, the session id of a DIFFERENT
pane. Three panes, one id. The stored history agrees: `mess-ru-go:0.0` and
`mess-ru-final:0.0` each name the other as their successor, so that rule would have two
real crashes silence each other.

These tests pin the evidence actually required, and — just as important — every shape
that must leave the alert alone.
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw
from core.control_plane import api, cto
from core.control_plane.discovery import IDENTITY_CWD_FALLBACK, IDENTITY_RUNTIME

RUNTIME_SID = "08c1a936-7785-4b6d-a604-2eae1f914f94"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def _crash(target, project="p"):
    return cto.emit("agent_watch", "agent_process_failed", project_id=project,
                    agent_id=target, severity="critical", owner_action_required=True,
                    action_taken=f"{target}: pane vanished while working",
                    dedup_key=f"agentwatch:{target}:vanished")["event_id"]


def _open_alerts():
    return {a["agent"] for a in aw.recent_alerts(limit=100)
            if a["type"] == "agent_process_failed"}


def _sources(**mapping):
    """identity_fn stub: target -> (conversation_id, source)."""
    def fn(target):
        conv, src = mapping.get(target, ("", ""))
        return {"conversation_id": conv, "source": src}
    return fn


# ── the one case that clears ────────────────────────────────────────────────
def test_a_resumed_session_with_runtime_proof_on_both_sides_clears():
    _crash("hostsecure-clean:1.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"hostsecure-clean-resumed:0.0"},
        identity_fn=_sources(**{"hostsecure-clean:1.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "hostsecure-clean-resumed:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert len(out) == 1
    assert out[0]["resumed_as"] == "hostsecure-clean-resumed:0.0"
    assert _open_alerts() == set()


def test_the_retirement_says_it_was_resumed_not_that_it_was_false():
    """The pane really did die. Recording a correct alert as a false one would corrupt
    the overlay the audit trail depends on."""
    eid = _crash("a:1.0")
    aw.reconcile_resumed_sessions(
        live_targets={"b:0.0"},
        identity_fn=_sources(**{"a:1.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "b:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    rows = [a for a in aw.recent_alerts(limit=100, include_invalid=True)
            if a.get("event_id") == eid]
    assert rows, "the event row must survive — retirement is an overlay, never a delete"
    import sqlite3, os
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    try:
        reason = c.execute("SELECT reason FROM agent_alert_invalid WHERE event_id=?",
                           (eid,)).fetchone()[0]
    finally:
        c.close()
    assert "resumed" in reason and "b:0.0" in reason
    assert "false" not in reason.lower()


# ── the directory collision: the reason the naive rule is wrong ─────────────
def test_a_live_pane_with_a_cwd_guessed_id_never_clears_anything():
    """`hostsecure-clean:0.0` exactly: alive, same directory, no runtime id of its own,
    and therefore carrying someone else's session id."""
    _crash("hostsecure-clean-resumed:0.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"hostsecure-clean:0.0"},
        identity_fn=_sources(**{
            "hostsecure-clean-resumed:0.0": (RUNTIME_SID, IDENTITY_RUNTIME),
            "hostsecure-clean:0.0": (RUNTIME_SID, IDENTITY_CWD_FALLBACK)}))
    assert out == []
    assert _open_alerts() == {"hostsecure-clean-resumed:0.0"}


def test_a_dead_pane_whose_own_id_was_only_a_guess_never_clears():
    _crash("mess-ru-go:0.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"live:0.0"},
        identity_fn=_sources(**{"mess-ru-go:0.0": (RUNTIME_SID, IDENTITY_CWD_FALLBACK),
                                "live:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert out == [] and _open_alerts() == {"mess-ru-go:0.0"}


def test_the_mutual_pair_cannot_silence_itself():
    """Both dead, each the other's apparent successor. Neither is live, so neither
    qualifies — and if the liveness check were ever dropped, this is what breaks."""
    _crash("mess-ru-go:0.0")
    _crash("mess-ru-final:0.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"unrelated:0.0"},
        identity_fn=_sources(**{"mess-ru-go:0.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "mess-ru-final:0.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "unrelated:0.0": ("other-sid", IDENTITY_RUNTIME)}))
    assert out == []
    assert _open_alerts() == {"mess-ru-go:0.0", "mess-ru-final:0.0"}


def test_two_live_holders_of_one_session_is_ambiguous_not_recovered():
    _crash("a:1.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"b:0.0", "c:0.0"},
        identity_fn=_sources(**{"a:1.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "b:0.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "c:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert out == [] and _open_alerts() == {"a:1.0"}


# ── history is not bulk-closed ──────────────────────────────────────────────
def test_alerts_predating_the_provenance_sidecar_all_survive():
    """No provenance row means unknown, and unknown never clears. This is why the rule
    retires none of the existing 89 on the day it ships."""
    for t in ("old1:0.0", "old2:0.0", "old3:0.0"):
        _crash(t)
    out = aw.reconcile_resumed_sessions(
        live_targets={"new:0.0"},
        identity_fn=_sources(**{"new:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert out == []
    assert _open_alerts() == {"old1:0.0", "old2:0.0", "old3:0.0"}


def test_an_empty_inventory_is_blindness_not_universal_recovery():
    """The failure that would clear everything at once: reading "nobody is alive" as
    "nothing is broken"."""
    _crash("a:1.0")
    out = aw.reconcile_resumed_sessions(
        live_targets=set(),
        identity_fn=_sources(**{"a:1.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert out == [] and _open_alerts() == {"a:1.0"}


def test_an_unavailable_inventory_retires_nothing(monkeypatch):
    _crash("a:1.0")
    from core import agent_control as ac
    monkeypatch.setattr(ac, "agent_list", lambda *a, **kw: (_ for _ in ()).throw(OSError("tmux")))
    assert aw.reconcile_resumed_sessions() == []
    assert _open_alerts() == {"a:1.0"}


def test_a_still_live_target_is_left_to_the_same_target_path():
    """`_reconcile_recovered_crash` owns a pane that came back under its own name;
    doing it here too would retire the same alert twice under two reasons."""
    _crash("a:1.0")
    out = aw.reconcile_resumed_sessions(
        live_targets={"a:1.0", "b:0.0"},
        identity_fn=_sources(**{"a:1.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                                "b:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)}))
    assert out == []


def test_it_is_idempotent_and_does_not_re_retire():
    _crash("a:1.0")
    ids = _sources(**{"a:1.0": (RUNTIME_SID, IDENTITY_RUNTIME),
                      "b:0.0": (RUNTIME_SID, IDENTITY_RUNTIME)})
    first = aw.reconcile_resumed_sessions(live_targets={"b:0.0"}, identity_fn=ids)
    second = aw.reconcile_resumed_sessions(live_targets={"b:0.0"}, identity_fn=ids)
    assert len(first) == 1 and second == []


# ── provenance is actually recorded by discovery ────────────────────────────
def test_discovery_records_which_source_produced_the_id(monkeypatch):
    """Without this the rule can never fire, because both sides need a runtime row."""
    from core.control_plane import discovery as d
    monkeypatch.setattr("core.native_sessions.session_id_for_pid",
                        lambda pid: RUNTIME_SID if pid == 11 else None)
    got_runtime = d._identity_with_source({"pid": 11}, "/opt/x", lambda cwd: "guessed")
    got_guess = d._identity_with_source({"pid": 22}, "/opt/x", lambda cwd: "guessed")
    assert got_runtime == (RUNTIME_SID, IDENTITY_RUNTIME)
    assert got_guess == ("guessed", IDENTITY_CWD_FALLBACK)
    api.record_identity_source("t:0.0", conversation_id=RUNTIME_SID, source=IDENTITY_RUNTIME)
    assert api.identity_source("t:0.0") == {"conversation_id": RUNTIME_SID,
                                            "source": IDENTITY_RUNTIME}


def test_no_pid_and_no_cwd_records_no_source_at_all():
    from core.control_plane import discovery as d
    assert d._identity_with_source({"pid": None}, "", lambda cwd: None) == (None, "")
    assert api.identity_source("never-seen:0.0") == {"conversation_id": "", "source": ""}
