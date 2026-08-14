"""Delivery is the message arriving, and eligibility is more than severity.

Two defects met here. The bridge called a wake delivered when the composer emptied — a local,
optimistic signal the page gives before the turn is committed — so a send that failed
afterwards was indistinguishable from one that worked, and got acknowledged anyway. And
eligibility was severity-or-owner_action only, which meant `owner_gate_opened` (info
severity, no owner_action flag) could sit there unread: an event whose entire purpose is to
ask the owner something never woke them.
"""
from __future__ import annotations

import importlib.util
import sys

import pytest

from core import wake_bridge as wb

spec = importlib.util.spec_from_file_location(
    "cdp_composer", "/root/ai-dev-runtime/tools/cdp_composer.py")
cdp = importlib.util.module_from_spec(spec)
sys.modules["cdp_composer"] = cdp
spec.loader.exec_module(cdp)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat("https://chatgpt.com/c/delivery-test-chat")
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    yield


class _S:
    """Fake CDP session. `turns` are the successive user-turn counts the page reports."""

    def __init__(self, bools, turns):
        self.bools = list(bools)
        self.turns = list(turns)
        self.inserted = []

    def call(self, method, params=None):
        if method == "Input.insertText":
            self.inserted.append((params or {}).get("text"))
        return {}

    def boolean(self, expression):
        if "readyState" in expression:
            return True
        return self.bools.pop(0) if self.bools else None

    def count(self, selector):
        if selector == cdp.USER_TURN_SEL:
            return self.turns.pop(0) if self.turns else 0
        return 1

    def last_attr(self, selector, attr):
        return ""          # id never changes here; the count alone decides

    def close(self):
        pass


def _wire(monkeypatch, *, bools, turns):
    s = _S(bools, turns)
    monkeypatch.setattr(cdp, "find_target", lambda url: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: s)
    return s


def _decide_wake(event_id, **kw):
    d = wb.should_wake(event_id=event_id, severity=kw.pop("severity", "critical"), **kw)
    wb.record(d, event_id=event_id, severity="critical")
    return d


# ── a failed delivery is persisted, and stays pending ──────────────────────
def test_a_failed_delivery_is_recorded_as_failed(monkeypatch):
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(101)
    r = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                          source="companion", event_id=101)
    assert r["ok"] is False
    row = wb.last_delivery(101)
    assert row is not None
    assert row["delivered"] is False
    assert row["reason"] == "user_turn_not_observed_after_send"


def test_a_failed_delivery_stays_unacknowledged_but_is_not_retried(monkeypatch):
    """This test used to assert the opposite — that an unverified send stays pending so the
    next poll tries again. That contract is what put ~60 duplicate wakes in the owner's chat
    between 2026-08-09 and 2026-08-11, because the verification false-negatived on messages
    that had in fact arrived. The evidence must still record the failure honestly; what may
    not happen is a second submission."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(102)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=102)
    assert wb.last_delivery(102)["delivered"] is False      # honest evidence kept
    assert wb.was_submitted(102) is True                    # but the phrase did leave
    assert wb.pending_wake()["pending"] is False            # so it is never offered again


def test_the_delivery_row_names_the_conversation_it_resolved_to(monkeypatch):
    """Every attempt records WHICH chat it went to, so "did a wake land in a stale chat"
    is answerable from state instead of from memory or log archaeology."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[3, 4])
    _decide_wake(150)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=150)
    row = wb.last_delivery(150)
    assert row["conversation"] == "https://chatgpt.com/c/delivery-test-chat"


def test_a_verified_delivery_is_recorded_as_delivered(monkeypatch):
    _wire(monkeypatch, bools=[True, True, True, True], turns=[3, 4])
    _decide_wake(103)
    r = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                          source="companion", event_id=103)
    assert r["ok"] is True and r["reason"] == "submitted_and_user_turn_appeared"
    row = wb.last_delivery(103)
    assert row["delivered"] is True


def test_only_a_verified_delivery_may_be_acknowledged(monkeypatch):
    """Acknowledgement is the companion's job, but the contract is asserted here: the failed
    event is still unacknowledged, the delivered one is not pending once acknowledged."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[1, 2])
    _decide_wake(104)
    r = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                          source="companion", event_id=104)
    assert r["ok"] is True
    wb.acknowledge(104)
    assert wb.pending_wake()["pending"] is False


def test_health_surfaces_the_last_delivery_and_the_failure_count(monkeypatch):
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(105)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=105)
    h = wb.health()
    assert h["last_delivery_ok"] is False
    assert h["last_delivery_reason"] == "user_turn_not_observed_after_send"
    assert h["deliveries_failed_total"] == 1


# ── dedupe and cooldown survive the change ─────────────────────────────────
def test_a_failed_delivery_does_not_reopen_the_per_event_dedupe(monkeypatch):
    """A retry must go through the SAME pending wake, not mint a second one."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(106, now=1000.0)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=106)
    again = wb.should_wake(event_id=106, severity="critical", now=1000.0 + 10_000)
    assert again["wake"] is False and again["reason"] == "already_woke_for_this_event"


def test_the_global_claim_still_bounds_a_retry(monkeypatch):
    """The claim is spent whether or not delivery succeeded, so a retry waits out the
    cooldown rather than hammering the page."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(107)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=107)
    second = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                               source="companion", event_id=107)
    assert second["ok"] is False and second["reason"].startswith("not_claimed:")


# ── eligibility mapping ────────────────────────────────────────────────────
def test_an_owner_gate_qualifies_even_at_info_severity():
    """The live gap: 35 `owner_gate_opened` events in two days, every one of them info
    severity with owner_action_required=0, none of which could reach the bridge."""
    s = wb.is_significant(event_type="owner_gate_opened", severity="info")
    assert s["significant"] is True and s["reason"] == "significant_event_type"


@pytest.mark.parametrize("etype", [
    "agent_dead", "task_failed", "stage_blocked_external", "session_quarantined",
    "notification_dead_letter", "agent_waiting_owner", "task_completed",
    "work_stopped_incomplete",
])
def test_failure_blocker_waiting_and_completion_all_qualify(etype):
    assert wb.is_significant(event_type=etype, severity="info")["significant"] is True


@pytest.mark.parametrize("etype", [
    "agent_state", "action_verified", "work_partial_completion",
    "work_commits_without_stage_progress", "work_report_published", "owner_gate_answered",
])
def test_routine_progress_chatter_does_not_qualify(etype):
    s = wb.is_significant(event_type=etype, severity="info")
    assert s["significant"] is False and s["reason"] == "routine_event_type"


def test_severity_and_owner_action_remain_independent_authorities():
    """The mapping is additive. Anything that woke before must still wake — including a
    routine type that arrives at critical severity, or carrying an owner-action flag."""
    assert wb.is_significant(event_type="agent_state", severity="critical")["significant"]
    assert wb.is_significant(event_type="agent_state", severity="info",
                             owner_action_required=True)["significant"]
    assert wb.is_significant(event_type="", severity="high")["significant"]


def test_an_unknown_type_at_routine_severity_still_does_not_wake():
    s = wb.is_significant(event_type="something_new", severity="info")
    assert s["significant"] is False and s["reason"] == "severity_below_wake_threshold"


def test_should_wake_uses_the_mapping_and_records_its_reason():
    d = wb.should_wake(event_id=200, severity="info", event_type="owner_gate_opened")
    assert d["wake"] is True
    wb.record(d, event_id=200, severity="info")
    skipped = wb.should_wake(event_id=201, severity="info", event_type="agent_state")
    assert skipped["wake"] is False and skipped["reason"] == "routine_event_type"


def test_the_emit_path_consults_the_bridge_for_an_owner_gate():
    """End to end through cto.emit: the inline severity test used to stop this event before
    the bridge ever saw it."""
    import sqlite3, os
    from core.control_plane import cto
    cto.emit("governor", "owner_gate_opened", agent_id="mess-qa-automation:0.0",
             severity="info", push=False)
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    row = c.execute("SELECT decision,reason FROM wake_audit ORDER BY id DESC LIMIT 1"
                    ).fetchone()
    assert row is not None, "the bridge was never consulted for an owner gate"
    assert row[0] == "wake" and row[1] == "urgent_event_not_yet_signalled"


def test_the_emit_path_still_ignores_routine_events():
    import sqlite3, os
    from core.control_plane import cto
    cto.emit("commander_autopilot", "agent_state", agent_id="mess-qa-automation:0.0",
             severity="info", push=False)
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    # The bridge is only CONSULTED for significant events, so for a routine one the audit
    # table may legitimately not exist at all — absent and empty are the same verdict here.
    n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                  "AND name='wake_audit'").fetchone()[0]
    if n:
        n = c.execute("SELECT COUNT(*) FROM wake_audit").fetchone()[0]
    assert n == 0, "a routine state event must not even reach the bridge"


# ── the duplicate-send defect: ambiguous verification must never resend ────
def test_an_ambiguous_verification_never_offers_the_event_again(monkeypatch):
    """The live failure, 2026-08-09 to 2026-08-11: verification false-negatived on messages
    that HAD arrived, the event stayed unacknowledged, the companion retried, and the owner
    received the same wake twice — 27 of 49 events. Ambiguity now resolves to "assume it
    went"; the CTO inbox still holds the event, so a genuinely lost wake costs latency only."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(300)
    assert wb.pending_wake()["event_id"] == 300
    r = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                          source="companion", event_id=300)
    assert r["ok"] is False and r["reason"] == "user_turn_not_observed_after_send"
    assert wb.was_submitted(300) is True, "the fired phrase must be latched"
    assert wb.pending_wake()["pending"] is False, "a fired event may never be offered again"


def test_the_latch_survives_a_companion_restart(monkeypatch):
    """The latch is a durable row, not process memory — a restart mid-verification was one
    of the ways the same phrase went out twice."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[0] + [0] * 40)
    _decide_wake(301)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=301)
    import importlib
    importlib.reload(wb)                      # stand-in for the process dying and coming back
    assert wb.was_submitted(301) is True
    assert wb.pending_wake()["pending"] is False


def test_a_verified_delivery_is_still_terminal(monkeypatch):
    _wire(monkeypatch, bools=[True, True, True, True], turns=[1, 2])
    _decide_wake(302)
    r = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                          source="companion", event_id=302)
    assert r["ok"] is True
    wb.acknowledge(302)
    assert wb.pending_wake()["pending"] is False


def test_the_latch_does_not_swallow_a_never_attempted_event(monkeypatch):
    """Fail-closed must not mean fail-silent: an event whose phrase never fired is still
    offered. A refused claim never reaches the composer, so nothing is latched."""
    _decide_wake(303)
    assert wb.was_submitted(303) is False
    assert wb.pending_wake()["event_id"] == 303


def test_refused_claim_leaves_no_latch(monkeypatch):
    """Cooldown refusal happens before the composer is touched."""
    _wire(monkeypatch, bools=[True, True, True, True], turns=[1, 2])
    _decide_wake(304)
    cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                      source="companion", event_id=304)          # claims, latches
    r2 = cdp.submit_phrase("https://chatgpt.com/c/delivery-test-chat", "P",
                           source="companion", event_id=305)     # cooldown refuses
    assert r2["ok"] is False and r2["reason"].startswith("not_claimed:")
    assert wb.was_submitted(305) is False, "a refused claim must not latch"
