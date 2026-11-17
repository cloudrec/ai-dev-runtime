"""A review the tech lead has not decided is asked again, then the owner hears once.

2026-11-09: a unit without the wake switch lost the first wake of seven gaika questions and
they sat unanswered for an hour; nothing asked again. Same harness as test_fullcycle_e2e:
real engine, tmux, git; the clock is shifted, not slept.
"""
from __future__ import annotations

import json

import pytest

from core import task_contract as tc
from core.fullcycle import engine as fe
from tests.test_fullcycle_e2e import _events, _state, fc, h  # noqa: F401


class Clock:
    def __init__(self, e):
        self.base, self.offset = e.clock, 0.0
        e.clock = self

    def __call__(self):
        return self.base() + self.offset


@pytest.fixture
def waiting(fc, monkeypatch):
    told = []
    monkeypatch.setattr(fe, "_tell_owner", told.append)
    e, a, log, root = fc({"add": ["correct"]})
    h.wait_for(lambda: e.tick() and _state(e.status(), "add")["state"] == tc.VERIFYING, 30)
    assert len(fe.pending_reviews(e.project_id)) == 1
    return e, Clock(e), told


def _reminders():
    return [json.loads(p)["reminder"] for _i, _t, p, _o in _events(fe.REVIEW_EVENT)
            if "reminder" in json.loads(p)]


def test_no_reminder_before_the_wait(waiting):
    e, clock, told = waiting
    clock.offset = fe.REVIEW_REMIND_SECS - 5
    e.tick()
    assert _reminders() == [] and told == []


def test_an_unanswered_review_is_asked_again_spaced_and_bounded(waiting):
    e, clock, told = waiting
    for i in range(fe.REVIEW_REMIND_MAX + 3):
        clock.offset = (i + 1) * (fe.REVIEW_REMIND_SECS + 1)
        e.tick()
        e.tick()                                    # a second tick in the same window: nothing
    assert _reminders() == list(range(1, fe.REVIEW_REMIND_MAX + 1))
    assert len(told) == 1 and "add" in told[0]
    owner_flags = [o for _i, _t, p, o in _events(fe.REVIEW_EVENT)
                   if json.loads(p).get("reminder") == fe.REVIEW_REMIND_MAX]
    assert owner_flags == [1]


def test_a_reminder_carries_what_is_needed_to_decide(waiting):
    e, clock, _ = waiting
    clock.offset = fe.REVIEW_REMIND_SECS + 1
    e.tick()
    (p,) = [json.loads(p) for _i, _t, p, _o in _events(fe.REVIEW_EVENT)
            if "reminder" in json.loads(p)]
    rid = fe.pending_reviews(e.project_id)[0]["review_id"]
    assert p["review_id"] == rid and p["decide_with"]["review_id"] == rid
    assert p["task"] == "add" and p["waiting_s"] >= fe.REVIEW_REMIND_SECS


def test_a_decided_review_is_never_reminded(waiting):
    e, clock, told = waiting
    rid = fe.pending_reviews(e.project_id)[0]["review_id"]
    fe.submit_decision(rid, "approve", key="d-rem-1",
                       by="api:client/chatgpt-tech-lead")
    clock.offset = 10 * fe.REVIEW_REMIND_SECS
    e.tick()
    assert _reminders() == [] and told == []


def test_a_reminder_of_a_pending_review_is_not_retired_as_decided(waiting):
    """2026-11-17 live: reminders 82925/82938/82961/82985 of pending review 137 were
    expired as fullcycle_review_already_decided, because the reminder's event id differs
    from the review's announcing event id. A reminder is newer: it must ring."""
    from core import wake_bridge as wb
    e, clock, told = waiting
    clock.offset = fe.REVIEW_REMIND_SECS + 1
    e.tick()
    (rid,) = [i for i, _t, p, _o in _events(fe.REVIEW_EVENT) if "reminder" in json.loads(p)]
    assert not wb._fullcycle_review_closed(e.conn, rid)
