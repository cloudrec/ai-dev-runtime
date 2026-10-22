"""Disposable end-to-end run of the agent event supervisor against the REAL stores.

Real: control-plane DB (tmp), `cto.emit` (which itself consults the wake bridge), the wake
bridge decision/claim/submit/delivery tables, the supervisor's live `Ports` for events,
wake, wake status, leases and owner-needed emission. Faked: only the tmux pane (inspect +
the same-pane deliver/verify), because there is no live agent in a test.

Proves: one incomplete event -> exactly one verified continuation; an owner-gated event ->
none; neither repeats across further ticks or a process restart.
"""
from __future__ import annotations

import pytest

from core import agent_event_supervisor as aes
from core import wake_bridge as wb
from core.control_plane import cto
from core.control_plane.api import _c

GOOD, GATED = "worker:0.0", "payer:0.0"


class PaneOnlyFake(aes.Ports):
    def __init__(self, tails):
        self.tails = tails
        self.sends = []

    def inspect(self, target):
        return {"found": True, "capture_ok": True, "target": target,
                "session": target.split(":")[0], "alive": True, "is_agent": True,
                "state": "idle", "tail": self.tails[target], "cwd": f"/work/{target[:5]}",
                "conversation_id": f"conv-{target[:5]}", "conv_mtime": "t0",
                "shell_running": False, "pending": ""}

    def artifacts(self, cwd):
        return {"head": "deadbee", "dirty_files": 0}

    def eligible(self, session):
        return True

    def continue_same_session(self, target, cwd, text):
        self.sends.append((target, cwd, text))
        return {"ok": True, "proofs": {"submitted": True, "prompt_consumed": True}}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "0")
    monkeypatch.setattr(aes, "VERIFY_SETTLE_SECS", 0)
    monkeypatch.setattr(aes, "WAKE_CONFIRM_TIMEOUT_SECS", 3600)
    wb.bind_chat("https://chatgpt.com/c/integration-test-chat")
    yield


def _companion_delivers_everything():
    """What tools/wake_companion does for each decided wake: claim, latch, verify, ack."""
    conn, _ = _c(None)
    wb._conn(conn)
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT event_id FROM wake_audit WHERE decision='wake' AND acknowledged=0")]
    conn.close()
    for eid in ids:
        wb.claim_send("companion", eid, actionable=True)
        wb.mark_submitted(eid, source="companion")
        wb.record_delivery("companion", event_id=eid, delivered=True, reason="verified")
        wb.acknowledge(eid)
    return ids


def test_one_incomplete_event_one_continuation_owner_gated_none():
    pane = PaneOnlyFake({GOOD: "Implemented part 1 of 3.\n❯ ",
                         GATED: "Ready. Need the Stripe API key to charge the card.\n❯ "})
    aes.run_once(pane)                                         # initialise cursor on empty bus
    e1 = cto.emit("work_evidence", "work_stopped_incomplete", project_id="proj",
                  agent_id=GOOD, severity="high", push=False)["event_id"]
    e2 = cto.emit("agent_watch", "agent_waiting_input", project_id="pay",
                  agent_id=GATED, severity="high", push=False)["event_id"]

    aes.run_once(pane)                                         # ingest + wake (reused)
    st = {a["target"]: a for a in aes.status()["agents"]}
    assert st[GOOD]["state"] == st[GATED]["state"] == "VERIFYING"
    assert st[GOOD]["wake"]["decision"] == "wake"              # emission already decided it
    assert pane.sends == []                                    # nothing before delivery

    delivered = _companion_delivers_everything()
    assert set(delivered) == {e1, e2}

    for _ in range(5):
        aes.run_once(pane)
    assert pane.sends == [(GOOD, "/work/worke", aes.CONTINUATION_TEXT)]
    st = {a["target"]: a for a in aes.status()["agents"]}
    assert st[GOOD]["state"] == "WORKING"
    assert st[GOOD]["last_verified_result"] == "continuation_verified"
    assert st[GATED]["state"] == "OWNER_NEEDED"
    assert st[GATED]["owner_gate_reason"] == "owner_gate:money_spend"

    # exactly one owner-needed event on the bus, and the supervisor never ingests its own
    conn, _ = _c(None)
    own = conn.execute("SELECT COUNT(*) FROM event WHERE type=?",
                       (aes.EVENT_OWNER_NEEDED,)).fetchone()[0]
    wake_rows = conn.execute("SELECT COUNT(*) FROM wake_audit WHERE decision='wake' AND "
                             "event_id IN (?,?)", (e1, e2)).fetchone()[0]
    conn.close()
    assert own == 1
    assert wake_rows == 2                                      # one wake per event, no dupes

    # restart: fresh Ports, same DB — nothing is redone
    pane2 = PaneOnlyFake(pane.tails)
    for _ in range(3):
        aes.run_once(pane2)
    assert pane2.sends == [] and len(pane.sends) == 1


def test_undelivered_wake_never_continues():
    pane = PaneOnlyFake({GOOD: "Implemented part 1 of 3.\n❯ "})
    aes.run_once(pane)
    eid = cto.emit("work_evidence", "work_stopped_incomplete", project_id="proj",
                   agent_id=GOOD, severity="high", push=False)["event_id"]
    aes.run_once(pane)
    wb.claim_send("companion", eid, actionable=True)
    wb.mark_submitted(eid, source="companion")                 # fired, never confirmed
    wb.record_delivery("companion", event_id=eid, delivered=False,
                       reason="composer_did_not_clear_after_send")
    for _ in range(3):
        aes.run_once(pane)
    assert pane.sends == [] and aes.status(GOOD)["agents"][0]["state"] == "VERIFYING"
    aes.run_once(pane, now=aes.now_ts() + 4000)                # past confirm timeout
    a = aes.status(GOOD)["agents"][0]
    assert a["owner_gate_reason"] == "wake_delivery_ambiguous" and pane.sends == []
