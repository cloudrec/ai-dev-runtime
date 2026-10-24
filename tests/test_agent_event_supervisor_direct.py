"""Agent event supervisor, DIRECT continuation path (`AGENT_EVENT_SUPERVISOR_CONTINUE_PATH`
unset / `direct`): CONTINUE is actuated server-side into the pane and never goes through the
wake bridge, the companion, ChatGPT or the browser; OWNER_NEEDED stays notification-only;
exactly one continuation per episode, also across restart and replay."""
from __future__ import annotations

import pytest

from core import agent_event_supervisor as aes
from core import wake_bridge as wb
from core.control_plane import cto
from core.control_plane.api import _c
from test_agent_event_supervisor import CWD, T, FakePorts, drive, ev, snap, state


class NoWakePorts(FakePorts):
    """Any touch of the wake path is a test failure, not just a recorded call."""

    def wake(self, ev, conn):
        raise AssertionError("direct path must not request a wake")

    def wake_status(self, event_id, conn):
        raise AssertionError("direct path must not consult wake delivery state")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "1")
    monkeypatch.delenv("AGENT_EVENT_SUPERVISOR_CONTINUE_PATH", raising=False)
    monkeypatch.delenv("AGENT_EVENT_SUPERVISOR_TARGETS", raising=False)
    for name, val in (("VERIFY_SETTLE_SECS", 60), ("WAKE_CONFIRM_TIMEOUT_SECS", 600),
                      ("WAKE_COOLDOWN_SECS", 300), ("CONTINUE_COOLDOWN_SECS", 300),
                      ("MAX_CONTINUATIONS", 2), ("CONTINUATION_WINDOW_SECS", 3600),
                      ("STALL_SECS", 1800), ("INTERVAL", 60)):
        monkeypatch.setattr(aes, name, val)
    yield


@pytest.mark.parametrize("val,direct", [(None, True), ("", True), ("direct", True),
                                        ("DIRECT", True), ("wake_gated", False),
                                        ("drect", False)])
def test_continue_path_defaults_to_direct_and_unknown_values_are_the_stricter_path(
        monkeypatch, val, direct):
    if val is None:
        monkeypatch.delenv("AGENT_EVENT_SUPERVISOR_CONTINUE_PATH", raising=False)
    else:
        monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_CONTINUE_PATH", val)
    assert aes.direct_mode() is direct


def test_continue_never_touches_wake_and_sends_exactly_once():
    p = NoWakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    s = state()
    assert s["state"] == "VERIFYING" and s["next_action"] == "verify_then_continue_direct"
    assert s["wake"]["decision"] == "not_requested"
    assert s["wake"]["reason"] == "direct_continuation_path"
    aes.run_once(p, now=1030)                    # settle window still applies
    assert p.sends == []
    out = aes.run_once(p, now=1070)
    assert p.sends == [(T, CWD, aes.CONTINUATION_TEXT)]
    assert out["wakes"] == 0 and out["continued"] == 1
    s = state()
    assert s["state"] == "WORKING" and s["last_verified_result"] == "continuation_verified"
    drive(p, 1300, 1600, 2000)
    assert len(p.sends) == 1


@pytest.mark.parametrize("bridge_state", ["ambiguous", "pending", "not_sent"])
def test_wake_bridge_state_can_neither_block_nor_unlock_a_continuation(bridge_state):
    p = FakePorts()
    p.wake_state = bridge_state                  # e.g. the companion cannot find a composer
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    assert p.wakes == [] and len(p.sends) == 1


@pytest.mark.parametrize("etype,tail", [
    ("owner_decision_required", None),
    ("agent_waiting_input", "Ready. Need the Stripe API key to charge the card.\n\n❯ "),
])
def test_owner_needed_is_notification_only_zero_continuation(etype, tail):
    p = NoWakePorts()
    if tail:
        p.snap = snap(tail)
    p.events = [ev(1, etype)]
    drive(p, 1001, 1070, 1500, 3000)
    assert p.sends == []
    assert state()["state"] == "OWNER_NEEDED"
    assert len(p.owner_emits) == 1              # one durable owner notification, no repeats


def test_restart_with_fresh_process_does_not_continue_again():
    p = NoWakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    p2 = NoWakePorts()
    p2.events = list(p.events)
    drive(p2, 1200, 1500, 2000)
    assert len(p.sends) == 1 and p2.sends == []


def test_replayed_event_after_cursor_loss_is_deduped_by_the_ledger():
    p = NoWakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    conn, _ = _c(None)
    aes._conn(conn)
    conn.execute("DELETE FROM aes_cursor")       # the bus is re-read from the start
    conn.commit()
    conn.close()
    drive(p, 1200, 1500, 2000)
    assert len(p.sends) == 1


def test_crash_between_latch_and_result_never_resends():
    p = NoWakePorts()
    p.continue_raises = True                     # dies mid-send: outcome unknown
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    p2 = NoWakePorts()
    p2.events = list(p.events)
    drive(p2, 1200, 1500, 2000)
    assert len(p.sends) == 1 and p2.sends == []
    assert state()["state"] == "OWNER_NEEDED"


def test_someone_typing_into_the_pane_still_blocks_the_direct_continuation():
    p = NoWakePorts()
    p.foreign = True                             # e.g. an assistant acted on its own wake
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1500)
    assert p.sends == []


def test_out_of_scope_real_agent_is_never_continued(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_TARGETS", "aes-canary")
    p = NoWakePorts()
    p.eligible_sessions = {"payorch-ha-fresh", "proj"}
    p.events = [ev(1, "work_stopped_incomplete", target="payorch-ha-fresh:0.0"),
                ev(2, "work_stopped_incomplete", target=T)]
    drive(p, 1001, 1070, 1500)
    assert p.sends == []
    assert aes.status()["agents"] == []


def test_live_port_continues_via_agent_control_not_the_wake_bridge(monkeypatch):
    from core import agent_continuation_watchdog as cw
    calls = []

    def fake_deliver(ctrl, **kw):
        calls.append(kw)
        return {"verify": {"ok": True, "submitted": True, "prompt_consumed": True}}

    def boom(*a, **k):
        raise AssertionError("wake bridge used on the continuation path")

    monkeypatch.setattr(cw, "Controller", lambda: object())
    monkeypatch.setattr(cw, "deliver_and_verify", fake_deliver)
    for name in ("should_wake", "record", "pending_wake", "claim_send", "mark_submitted",
                 "record_delivery"):
        monkeypatch.setattr(wb, name, boom)
    res = aes.Ports().continue_same_session("aes-canary:0.0", "/w", "continue")
    assert res["ok"] is True
    assert calls == [{"target": "aes-canary:0.0", "cwd": "/w", "action": "deliver",
                      "step_text": "continue", "expected_pending": "continue"}]


class PaneOnly(aes.Ports):
    """Real stores and real live Ports for events / leases / owner emission; only the pane
    is faked (no live agent in a test)."""

    def __init__(self, tails):
        self.tails, self.sends = tails, []

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
        self.sends.append(target)
        return {"ok": True}

    def wake(self, ev, conn):
        raise AssertionError("direct path must not request a wake")

    def wake_status(self, event_id, conn):
        raise AssertionError("direct path must not consult wake delivery state")


def test_integration_real_stores_continue_once_without_any_wake_delivery(monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "0")
    monkeypatch.setattr(aes, "VERIFY_SETTLE_SECS", 0)
    wb.bind_chat("https://chatgpt.com/c/integration-test-chat")
    good, gated = "worker:0.0", "payer:0.0"
    pane = PaneOnly({good: "Implemented part 1 of 3.\n❯ ",
                     gated: "Ready. Need the Stripe API key to charge the card.\n❯ "})
    aes.run_once(pane)                           # cursor on the empty bus
    cto.emit("work_evidence", "work_stopped_incomplete", project_id="proj", agent_id=good,
             severity="high", push=False)
    cto.emit("agent_watch", "agent_waiting_input", project_id="pay", agent_id=gated,
             severity="high", push=False)
    # the companion never delivers anything (the live composer outage)
    for _ in range(5):
        aes.run_once(pane)
    assert pane.sends == [good]
    st = {a["target"]: a for a in aes.status()["agents"]}
    assert st[good]["state"] == "WORKING" and st[gated]["state"] == "OWNER_NEEDED"
    conn, _ = _c(None)
    # `wake_submitted` only comes into existence on a bridge submission: absent = none
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='wake_submitted'").fetchone()
    submitted = conn.execute("SELECT COUNT(*) FROM wake_submitted").fetchone()[0] if has else 0
    own = conn.execute("SELECT COUNT(*) FROM event WHERE type=?",
                       (aes.EVENT_OWNER_NEEDED,)).fetchone()[0]
    conn.close()
    assert submitted == 0 and own == 1
    pane2 = PaneOnly(pane.tails)                 # restart: nothing is redone
    for _ in range(3):
        aes.run_once(pane2)
    assert pane2.sends == []
