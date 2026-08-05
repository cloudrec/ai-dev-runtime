"""The two real incidents that made Phase 3 a FAIL, reproduced.

Owner report: "MESS had `continue with slice 2` typed but not submitted; Payment had
owner-approved deploy text typed but not submitted. Both required manual intervention."

Reconstructed from the soak recordings:

  * payment:0.0 held 'Proceed with the previously queued read-only replication mon…' in
    state waiting_input for 985 consecutive samples (~16h). The autopilot recorded a bland
    `poke_owner_gated` once an hour and raised NOTHING. Across the entire system there was
    not one owner gate mentioning stuck or queued input.
  * mess-qa-automation:0.0 held its owner line from 18:32:50 to 18:50:17, 48 of 51 samples
    in state `working`. Standing off a busy pane is correct; recording nothing about the
    owner's waiting text is not.

The defect is therefore NOT that the text was un-submittable — sometimes it legitimately is.
It is that every refusal looked identical from outside: silence.
"""
from __future__ import annotations

import pytest

from core import commander_autopilot as ap


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def test_owner_text_on_an_ungated_target_becomes_a_blocker_not_silence():
    """The payment incident: approved text typed, target outside the actuation allowlist,
    16h of silence."""
    t0 = 1_000_000.0
    text = "Proceed with the previously queued read-only replication monitoring step"
    first = ap._track_queued_input(None, "payment:0.0", text, "not_actuatable", now=t0)
    assert first["stalled"] is False, "not an alarm on first sight"

    later = ap._track_queued_input(None, "payment:0.0", text, "not_actuatable",
                                   now=t0 + ap.QUEUED_STALL_SECS + 1)
    assert later["stalled"] is True
    assert later["reason"] == "not_actuatable"
    assert later["age_secs"] >= ap.QUEUED_STALL_SECS


def test_the_stall_escalates_exactly_once_not_every_tick():
    """An owner gate reopened every 60s is noise, not signal."""
    t0 = 1_000_000.0
    text = "Proceed with the previously queued read-only replication monitoring step"
    ap._track_queued_input(None, "payment:0.0", text, "not_actuatable", now=t0)
    hits = [ap._track_queued_input(None, "payment:0.0", text, "not_actuatable",
                                   now=t0 + ap.QUEUED_STALL_SECS + i)["stalled"]
            for i in range(1, 6)]
    assert hits.count(True) == 1, hits


def test_a_busy_pane_is_still_reported_while_it_holds_owner_text():
    """The MESS incident: 48 of 51 samples `working`. Standing off is right; the owner's
    text waiting behind that turn must still be visible."""
    t0 = 1_000_000.0
    text = "continue with slice 2"
    ap._track_queued_input(None, "mess-qa-automation:0.0", text, "pane_busy", now=t0)
    out = ap._track_queued_input(None, "mess-qa-automation:0.0", text, "pane_busy",
                                 now=t0 + ap.QUEUED_STALL_SECS + 1)
    assert out["stalled"] is True and out["reason"] == "pane_busy"


def test_a_short_wait_is_never_escalated():
    """Anti-overcorrection: text queued behind a normal turn must not page the owner."""
    t0 = 1_000_000.0
    ap._track_queued_input(None, "mess-qa-automation:0.0", "continue with slice 2",
                           "pane_busy", now=t0)
    for dt in (30, 60, 120, ap.QUEUED_STALL_SECS - 1):
        out = ap._track_queued_input(None, "mess-qa-automation:0.0",
                                     "continue with slice 2", "pane_busy", now=t0 + dt)
        assert out["stalled"] is False, dt


def test_replacing_the_text_restarts_the_clock():
    """The owner retyping something else is a NEW instruction, not a continuing stall."""
    t0 = 1_000_000.0
    ap._track_queued_input(None, "mess-qa-automation:0.0", "continue with slice 2",
                           "pane_busy", now=t0)
    out = ap._track_queued_input(None, "mess-qa-automation:0.0", "continue with slice 3",
                                 "pane_busy", now=t0 + ap.QUEUED_STALL_SECS + 1)
    assert out["stalled"] is False, "a different line starts its own watch"


def test_submission_clears_the_watch_so_it_cannot_alarm_later():
    t0 = 1_000_000.0
    ap._track_queued_input(None, "cp-canary:0.0", "do the thing", "pane_busy", now=t0)
    ap._clear_queued_watch(None, "cp-canary:0.0")
    out = ap._track_queued_input(None, "cp-canary:0.0", "do the thing", "pane_busy",
                                 now=t0 + ap.QUEUED_STALL_SECS + 1)
    assert out["stalled"] is False, "the clock restarts after a successful submission"
