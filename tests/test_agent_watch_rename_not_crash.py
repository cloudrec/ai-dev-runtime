"""A rename is not a death.

Event 18172 declared `owner-os-wake-policy-opus:0.0` CRASHED — critical, owner action
required — 18 seconds after event 18170 recorded that exact target as the `renamed_from`
of a live agent in the same cwd. Nothing had died: a tmux session was renamed, and the
control plane said so in the very payload it published.

The two halves of the system disagreed because neither could hear the other.
`core.control_plane.discovery` reconciles a rename by conversation_id and retires the old
registry row; `core.agent_watch` tracks panes by tmux target alone, so the old name just
stopped appearing in its inventory — and two consecutive absences is precisely how it
recognises a crash. Discovery held the only evidence that distinguished "renamed" from
"died", at the only moment it existed, and had no way to say it.

`aw.retire()` is that way, and these tests pin both directions: a renamed target raises
no crash, and a genuinely vanished one still does.
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw
from core import control_plane as cp
from core.control_plane import cto
from core.control_plane import discovery as disc


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


WORKING_TAIL = "✻ Compacting… (esc to interrupt · 32.1k tokens)"

CONFIG = {
    "allowed_roots": ["/opt", "/root/ai-dev-runtime"],
    "sessions": {
        "arbitrage2-opus": {"mode": "auto", "project": "arbitrage2",
                            "root": "/opt/arbitrage2"},
    },
}


class _Emit:
    def __init__(self):
        self.calls = []
        self._n = 0

    def __call__(self, source, etype, **kw):
        self._n += 1
        self.calls.append({"source": source, "type": etype, **kw})
        return {"event_id": self._n}

    def types(self):
        return [c["type"] for c in self.calls]


def _watched(target, cwd="/root/ai-dev-runtime", state="working"):
    return {"target": target, "cwd": cwd, "claude_cwd": cwd,
            "alive": True, "is_agent": True, "state": state}


def _disc_agent(target, cwd, pid=1000):
    return {"target": target, "session": target.split(":")[0], "is_agent": True,
            "alive": True, "claude_cwd": cwd, "pid": pid, "command": "claude"}


def _scan(agents, tails, emit, now=1000.0):
    return aw.scan(agents=agents, read_fn=lambda t: tails[t], emit_fn=emit, now=now)


# ── the exact false positive ────────────────────────────────────────────────

def test_a_renamed_target_does_not_become_a_crash():
    """The shape of event 18172: watched while working, then gone under that name."""
    old, new = "owner-os-wake-policy-opus:0.0", "owner-os-opus-clean:0.0"
    emit = _Emit()
    _scan([_watched(old)], {old: WORKING_TAIL}, emit)

    aw.retire(old, reason=f"renamed to {new}", by="discovery")

    # the pane is now watched under its new name; the old one never returns
    _scan([_watched(new)], {new: WORKING_TAIL}, emit, now=1100.0)
    _scan([_watched(new)], {new: WORKING_TAIL}, emit, now=1200.0)
    assert "agent_process_failed" not in emit.types()


def test_without_retiring_the_same_sequence_is_a_critical_crash():
    """The bug itself, pinned: absence alone is what the watcher had to go on."""
    old, new = "owner-os-wake-policy-opus:0.0", "owner-os-opus-clean:0.0"
    emit = _Emit()
    _scan([_watched(old)], {old: WORKING_TAIL}, emit)
    _scan([_watched(new)], {new: WORKING_TAIL}, emit, now=1100.0)
    _scan([_watched(new)], {new: WORKING_TAIL}, emit, now=1200.0)
    failed = [c for c in emit.calls if c["type"] == "agent_process_failed"]
    assert len(failed) == 1 and failed[0]["agent_id"] == old
    assert failed[0]["severity"] == "critical"


def test_a_genuinely_vanished_pane_is_still_a_crash():
    """The fix must not buy silence by blinding the vanish path."""
    emit = _Emit()
    _scan([_watched("gone:0.0")], {"gone:0.0": WORKING_TAIL}, emit)
    _scan([], {}, emit, now=1100.0)
    r = _scan([], {}, emit, now=1200.0)
    assert [e["class"] for e in r["emitted"]] == ["crashed"]
    assert emit.calls[0]["severity"] == "critical"


# ── retire() itself ─────────────────────────────────────────────────────────

def test_retire_drops_the_watch_state_and_only_for_that_target():
    emit = _Emit()
    _scan([_watched("a:0.0"), _watched("b:0.0")],
          {"a:0.0": WORKING_TAIL, "b:0.0": WORKING_TAIL}, emit)
    aw.retire("a:0.0", reason="renamed to c:0.0")
    conn, _ = aw._conn()
    try:
        rows = {r[0] for r in conn.execute("SELECT target FROM agent_watch_state")}
    finally:
        conn.close()
    assert rows == {"b:0.0"}


def test_retire_lifts_a_suppression_that_no_name_will_answer_to():
    aw.suppress("a:0.0", ttl_secs=3600, reason="maintenance")
    assert aw.is_suppressed("a:0.0") is True
    aw.retire("a:0.0", reason="renamed to c:0.0")
    assert aw.is_suppressed("a:0.0") is False


def test_retire_invalidates_a_crash_alert_already_published():
    """The watcher can reach the vanish branch BEFORE discovery reconciles the rename —
    that is the live ordering that produced 18172. A late retirement must still retract
    the false critical, through the audited overlay, never by deleting the event.

    Emits through the REAL inbox: the retraction is a join against the durable event
    log, so a fake emitter would let this pass while retracting nothing."""
    _scan([_watched("a:0.0")], {"a:0.0": WORKING_TAIL}, cto.emit)
    _scan([], {}, cto.emit, now=1100.0)
    _scan([], {}, cto.emit, now=1200.0)
    assert aw.recent_alerts()[0]["type"] == "agent_process_failed"

    out = aw.retire("a:0.0", reason="renamed to c:0.0")
    assert out["retired_events"], "the false crash was left standing"

    conn, _ = aw._conn()
    try:
        rows = conn.execute("SELECT event_id, reason FROM agent_alert_invalid").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and "renamed to c:0.0" in rows[0][1]
    # retracted from the default view, still reachable for audit
    assert not [a for a in aw.recent_alerts() if a["agent"] == "a:0.0"]
    assert [a for a in aw.recent_alerts(include_invalid=True) if a["agent"] == "a:0.0"]


def test_retire_is_idempotent_and_survives_an_unknown_target():
    assert aw.retire("never-seen:0.0", reason="renamed to x:0.0")["ok"] is True
    assert aw.retire("never-seen:0.0", reason="renamed to x:0.0")["ok"] is True
    assert aw.retire("", reason="x")["ok"] is False


def test_a_recovered_pane_still_retires_its_own_crash_with_its_own_reason():
    """The shared retirement path must not blur the two reasons together."""
    _scan([_watched("a:0.0")], {"a:0.0": WORKING_TAIL}, cto.emit)
    _scan([], {}, cto.emit, now=1100.0)
    _scan([], {}, cto.emit, now=1200.0)
    _scan([_watched("a:0.0")], {"a:0.0": WORKING_TAIL}, cto.emit, now=1300.0)
    conn, _ = aw._conn()
    try:
        rows = conn.execute("SELECT reason FROM agent_alert_invalid").fetchall()
    finally:
        conn.close()
    assert rows and "observed alive" in rows[0][0]


# ── discovery calls it, at the moment it is the only one who knows ──────────

def test_discovery_retires_the_watch_state_when_it_reconciles_a_rename():
    old, new, cwd = "old-name:0.0", "new-name:0.0", "/opt/arbitrage2"
    emit = _Emit()
    _scan([_watched(old, cwd=cwd)], {old: WORKING_TAIL}, emit)

    disc.discover({"agents": [_disc_agent(old, cwd)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")
    disc.discover({"agents": [_disc_agent(new, cwd)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")

    assert cp.get_agent(old)["lifecycle_state"] == disc.DEAD
    conn, _ = aw._conn()
    try:
        rows = {r[0] for r in conn.execute("SELECT target FROM agent_watch_state")}
    finally:
        conn.close()
    assert old not in rows

    # and the watcher, now blind to the old name by design, raises nothing for it
    _scan([_watched(new, cwd=cwd)], {new: WORKING_TAIL}, emit, now=1100.0)
    _scan([_watched(new, cwd=cwd)], {new: WORKING_TAIL}, emit, now=1200.0)
    assert "agent_process_failed" not in emit.types()


def test_discovery_leaves_an_unrelated_watch_state_alone():
    cwd = "/opt/arbitrage2"
    emit = _Emit()
    _scan([_watched("bystander:0.0", cwd=cwd)], {"bystander:0.0": WORKING_TAIL}, emit)
    disc.discover({"agents": [_disc_agent("old-name:0.0", cwd)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")
    disc.discover({"agents": [_disc_agent("new-name:0.0", cwd)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")
    conn, _ = aw._conn()
    try:
        rows = {r[0] for r in conn.execute("SELECT target FROM agent_watch_state")}
    finally:
        conn.close()
    assert "bystander:0.0" in rows


# ── a rename keeps the process; a replacement does not ─────────────────────
# `_conversation_id(cwd)` reads the newest conversation for a DIRECTORY, not for
# a pane, so every agent working in the same cwd carries the same id. A pane
# that genuinely died and a different pane that replaced it in that directory
# match each other by conversation exactly as a rename does. That is what
# happened around event 18172: the old target held pid 3501868 and the pane that
# "renamed" it held pid 3394205 — an older pid, so not the same process at all.
# The registry may still merge them (one row, no duplicate); silencing a crash
# alarm needs the stronger evidence.

def test_a_replacement_in_the_same_cwd_does_not_silence_the_crash():
    old, new, cwd = "old-name:0.0", "new-name:0.0", "/opt/arbitrage2"
    emit = _Emit()
    _scan([_watched(old, cwd=cwd)], {old: WORKING_TAIL}, emit)

    disc.discover({"agents": [_disc_agent(old, cwd, pid=3501868)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")
    disc.discover({"agents": [_disc_agent(new, cwd, pid=3394205)]}, config=CONFIG,
                  conversation_fn=lambda c: "convS")

    # the registry still reconciles to one live row — that behaviour is unchanged
    assert cp.get_agent(old)["lifecycle_state"] == disc.DEAD

    # ...but the watcher was NOT blinded, and still reports the process that stopped
    _scan([_watched(new, cwd=cwd)], {new: WORKING_TAIL}, emit, now=1100.0)
    r = _scan([_watched(new, cwd=cwd)], {new: WORKING_TAIL}, emit, now=1200.0)
    assert [e["class"] for e in r["emitted"]] == ["crashed"]
    assert [c["agent_id"] for c in emit.calls
            if c["type"] == "agent_process_failed"] == [old]


def test_a_missing_pid_is_not_treated_as_continuity():
    """An inventory row with no pid proves nothing. Absent evidence must leave the
    alarm standing, not stand in for it."""
    old, new, cwd = "old-name:0.0", "new-name:0.0", "/opt/arbitrage2"
    emit = _Emit()
    _scan([_watched(old, cwd=cwd)], {old: WORKING_TAIL}, emit)

    a_old = _disc_agent(old, cwd); a_old["pid"] = None
    a_new = _disc_agent(new, cwd); a_new["pid"] = None
    disc.discover({"agents": [a_old]}, config=CONFIG, conversation_fn=lambda c: "convS")
    disc.discover({"agents": [a_new]}, config=CONFIG, conversation_fn=lambda c: "convS")

    conn, _ = aw._conn()
    try:
        rows = {r[0] for r in conn.execute("SELECT target FROM agent_watch_state")}
    finally:
        conn.close()
    assert old in rows
