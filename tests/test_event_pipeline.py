"""Same-chat pinger producer — end-to-end significant-event path (payment + arbitrage2).

Proves the full owner-notification contract per event class (completed / waiting_owner /
failure / dead / blocker): correlated CTO event id, agent+project+factual summary, delivery
attempt with one retry, receipt evidence ONLY on a proven proactive send (honest cto_inbox
floor otherwise — never fabricated), dedupe, and a persistent CTO-inbox + legacy
commander_events record. Plus the false-idle invariant: a live shell/tool run is never idle.
"""
from __future__ import annotations

import pytest

from core.control_plane import event_pipeline as ep
from core.control_plane import cto
from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    # no proactive channel configured (== live G4/G5): deliver() fails closed to the inbox.
    for v in ("CONTROL_PLANE_SAMECHAT_WAKE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "WATCHDOG_TELEGRAM_ENABLED"):
        monkeypatch.delenv(v, raising=False)
    yield


def _commander_rows(agent=None):
    rows = ac.list_commander_events(limit=100)
    return [r for r in rows if agent is None or r["agent"] == agent]


# ── the four significant classes carry the full contract ─────────────────────
def test_completed_event_full_contract_payment():
    r = ep.publish_significant_event(agent="payment:0.0", project="payment-orchestrator",
                                     kind="completed", tail="  ", observed_state="idle",
                                     evidence={"summary": "run finished, 0 errors"})
    assert r["ok"] and r["event_id"] > 0 and r["correlation_id"] == "sig:payment:0.0:completed"
    assert r["agent"] == "payment:0.0" and r["project"] == "payment-orchestrator"
    assert "payment:0.0" in r["summary"] and "finished" in r["summary"]
    assert r["inbox_recorded"] is True and r["notification_id"] > 0
    # no proactive channel live → honest floor, NEVER a fabricated receipt
    assert r["delivered"] is False and r["receipt"] is None and r["delivery_floor"] == "cto_inbox"
    assert r["commander_event_recorded"] is True          # mirrored to the live owner surface
    # durable CTO inbox holds the correlated event
    brief = cto.cto_brief_since("test")
    ids = [e["event_id"] for e in brief["events"]]
    assert r["event_id"] in ids
    assert _commander_rows("payment:0.0")[0]["event_type"] == "agent_completed"


def test_waiting_owner_is_high_and_owner_actionable_arbitrage2():
    r = ep.publish_significant_event(agent="arbitrage2-opus:0.0", project="arbitrage2",
                                     kind="waiting_owner",
                                     evidence={"summary": "needs approval to change strategy"})
    assert r["ok"] and r["severity"] == "high" and r["owner_action_required"] is True
    assert r["notification_id"] > 0                        # pushed to the outbox
    assert r["delivered"] is False and r["delivery_floor"] == "cto_inbox"
    assert _commander_rows("arbitrage2-opus:0.0")[0]["event_type"] == "agent_waiting_input"


def test_failure_is_critical_and_mirrored_as_process_failed():
    r = ep.publish_significant_event(agent="payment:0.0", project="payment-orchestrator",
                                     kind="failure", evidence={"last_line": "Traceback ..."})
    assert r["ok"] and r["severity"] == "critical" and r["owner_action_required"] is True
    assert _commander_rows("payment:0.0")[0]["event_type"] == "agent_process_failed"


def test_dead_event_high_owner_action():
    r = ep.publish_significant_event(agent="arbitrage2-opus:0.0", project="arbitrage2", kind="dead")
    assert r["ok"] and r["severity"] == "high" and r["owner_action_required"] is True
    assert _commander_rows("arbitrage2-opus:0.0")[0]["event_type"] == "agent_process_failed"


def test_blocker_event_external():
    r = ep.publish_significant_event(agent="arbitrage2-opus:0.0", project="arbitrage2",
                                     kind="blocker", evidence={"summary": "RPC endpoint down"})
    assert r["ok"] and r["severity"] == "high"
    assert _commander_rows("arbitrage2-opus:0.0")[0]["event_type"] == "agent_externally_blocked"


# ── FALSE-IDLE INVARIANT: a live shell/tool run is never idle/completed ───────
def test_false_idle_completed_suppressed_by_spinner_marker():
    r = ep.publish_significant_event(agent="payment:0.0", project="payment-orchestrator",
                                     kind="completed", tail="Pouncing… (8s · thinking)")
    assert r["ok"] is False and r["reason"] == "false_idle_suppressed"
    assert r["false_idle_corrected"] is True
    # a correction event is recorded; NO completed event, NO commander completion row
    briefs = cto.cto_brief_since("t")["events"]
    assert any(e["type"] == "false_idle_corrected" for e in briefs)
    assert all(e["type"] != "completed" for e in briefs)
    assert _commander_rows("payment:0.0") == []


def test_false_idle_completed_suppressed_by_working_state():
    r = ep.publish_significant_event(agent="payment:0.0", kind="completed", observed_state="working")
    assert r["ok"] is False and r["false_idle_corrected"] is True


def test_false_idle_shell_running_never_idle():
    # a live shell/tool run in the pane → must never be reported completed/idle
    r = ep.publish_significant_event(agent="arbitrage2-opus:0.0", kind="completed",
                                     observed_state="shell_running",
                                     tail="user@host:~$ python run.py   (· 1 shell ·)")
    assert r["ok"] is False and r["reason"] == "false_idle_suppressed"
    assert _commander_rows("arbitrage2-opus:0.0") == []


def test_genuine_quiet_idle_completed_allowed():
    r = ep.publish_significant_event(agent="payment:0.0", kind="completed",
                                     tail="done. Type your message.", observed_state="idle")
    assert r["ok"] is True and r["event_id"] > 0


# ── dedupe, retry-once, receipt honesty ──────────────────────────────────────
def test_dedupe_collapses_repeat_within_window():
    a = ep.publish_significant_event(agent="payment:0.0", kind="waiting_owner")
    b = ep.publish_significant_event(agent="payment:0.0", kind="waiting_owner")
    assert a["ok"] and b["ok"]
    assert a["event_id"] == b["event_id"]           # SAME durable CTO inbox record (deduped)
    assert b["commander_event_recorded"] is False   # legacy owner-surface log deduped too
    assert len(_commander_rows("payment:0.0")) == 1


def test_retry_once_on_delivery_failure_then_success():
    from core.control_plane import api

    class FailThenSend:
        def __init__(self):
            self.calls = 0

        def __call__(self, nid, *, severity, conn=None):
            self.calls += 1
            if self.calls == 1:
                api.mark_notification(nid, "failed", conn=conn)
                return {"delivered": False, "tier": None,
                        "attempts": [{"tier": "same_chat_wake", "result": "unavailable"}],
                        "blocker": "first attempt failed"}
            api.mark_notification(nid, "sent", receipt="same_chat_wake:42", conn=conn)
            return {"delivered": True, "tier": "same_chat_wake",
                    "attempts": [{"tier": "same_chat_wake", "result": "sent",
                                  "receipt": "same_chat_wake:42"}]}

    fn = FailThenSend()
    r = ep.publish_significant_event(agent="payment:0.0", kind="waiting_owner", deliver_fn=fn)
    assert fn.calls == 2 and r["retried"] is True
    assert r["delivered"] is True and r["receipt"] == "same_chat_wake:42"
    assert r["delivery_floor"] is None and len(r["attempts"]) >= 2


def test_no_proactive_channel_is_honest_floor_never_fabricated():
    r = ep.publish_significant_event(agent="arbitrage2-opus:0.0", kind="waiting_owner")
    # default deliver() with no channel → delivered False, no receipt, durable floor, blocker set
    assert r["delivered"] is False and r["receipt"] is None
    assert r["delivery_floor"] == "cto_inbox" and r["blocker"]
    assert r["retried"] is True                     # it retried once before failing closed


def test_unknown_kind_rejected():
    r = ep.publish_significant_event(agent="payment:0.0", kind="not_a_kind")
    assert r["ok"] is False and r["reason"] == "unknown_kind"
