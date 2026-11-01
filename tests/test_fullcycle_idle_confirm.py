"""False idle detection (Stage 12, residual risk 2 of the 2026-11-01 review fixes).

`idle` from agent_control.classify_state is the classifier's fall-through: no active
marker matched. A pane running something the classifier does not recognise could read
idle once. Before the engine acts on "idle" after a stall (resend, cancel, clean session)
the agent must read idle CONTINUOUSLY, with an unchanged screen fingerprint, for
QUIET_CONFIRM_SECS on the engine clock. A crash or a delivered result needs no window.
"""
from __future__ import annotations

import pytest

from core import task_contract as tc
from core.fullcycle import engine as fe
from tests.test_fullcycle_e2e import _state, fc, h  # noqa: F401
from tests.test_fullcycle_stall import HOUR2, _answer, _hung_add

WINDOW = 60


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(fe, "QUIET_CONFIRM_SECS", WINDOW)


def _screen(monkeypatch, a):
    """Drive the fixture adapter's idle evidence: (quiescent, fingerprint)."""
    s = {"quiet": True, "fp": "pane-A"}
    monkeypatch.setattr(a, "idle_evidence", lambda handle: (s["quiet"], s["fp"]))
    return s


def _sends(log):
    return [x["attempt"] for x in h.agent_log(log) if x["task_id"] == "tinycalc:add"]


def _stalled(fc, monkeypatch, choice="answer"):
    e, a, log, root, clock = _hung_add(fc)
    s = _screen(monkeypatch, a)
    clock.offset = HOUR2 + 1
    e.tick()
    assert _answer(e, choice, key=f"{choice}-k")["ok"]
    return e, a, log, root, clock, s


def test_a_single_idle_reading_never_resends(fc, window, monkeypatch):
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    e.tick()                                    # first idle reading: the window opens
    clock.offset += WINDOW - 5
    e.tick()
    assert _sends(log) == [1]                   # not yet: idle for < WINDOW
    clock.offset += 10
    h.wait_for(lambda: e.tick() and _sends(log) == [1, 2], 15)


def test_a_non_idle_reading_restarts_the_window(fc, window, monkeypatch):
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    e.tick()
    clock.offset += WINDOW - 10
    s["quiet"] = False                           # it was busy after all
    e.tick()
    s["quiet"] = True
    e.tick()                                     # window reopens here
    clock.offset += 20                           # > WINDOW since the FIRST idle, < since reset
    e.tick()
    assert _sends(log) == [1]
    clock.offset += WINDOW
    h.wait_for(lambda: e.tick() and _sends(log) == [1, 2], 15)


def test_a_changing_screen_is_not_idle(fc, window, monkeypatch):
    """A spinner the classifier does not know still changes the pane every tick."""
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    for i in range(6):
        s["fp"] = f"spinner-{i}s"
        e.tick()
        clock.offset += WINDOW / 2
    assert _sends(log) == [1]
    s["fp"] = "settled"
    e.tick()
    clock.offset += WINDOW + 1
    h.wait_for(lambda: e.tick() and _sends(log) == [1, 2], 15)


def test_unknown_readings_never_count_as_idle(fc, window, monkeypatch):
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    s["quiet"] = None
    for _ in range(4):
        e.tick()
        clock.offset += WINDOW
    assert _sends(log) == [1]


def test_a_cancel_also_waits_for_confirmed_idle(fc, window, monkeypatch):
    e, a, log, root, clock, s = _stalled(fc, monkeypatch, choice="cancel")
    e.tick()
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.WORKING
    clock.offset += WINDOW + 1
    e.tick()
    assert _state(e.status(), "add")["state"] == tc.CANCELLED
    assert _sends(log) == [1]


def test_the_window_survives_an_engine_restart(fc, window, monkeypatch):
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    e.tick()                                     # window opened by the first engine
    e.release()
    e2 = fe.Engine("tinycalc", a, holder="e2")
    from tests.test_fullcycle_stall import Clock
    c2 = Clock(e2)
    c2.offset = clock.offset + WINDOW + 1
    h.wait_for(lambda: e2.tick() and _sends(log) == [1, 2], 15)


def test_a_late_result_needs_no_window(fc, window, monkeypatch):
    from tests.test_fullcycle_stall import _write_late_result
    e, a, log, root, clock, s = _stalled(fc, monkeypatch)
    s["quiet"] = False
    e.tick()
    _write_late_result(root, "add", 1, h.SOLUTIONS["add"])
    st = h.run(e, h.StubReviewer(), until=lambda x: _state(x, "add")["state"] == tc.VERIFIED_DONE,
               timeout=30)
    assert _state(st, "add")["attempt"] == 1 and _sends(log) == [1]


def test_live_adapter_reads_state_and_screen_once():
    from core.fullcycle.tmux_adapter import AgentControlAdapter
    calls = []

    class AC:
        @staticmethod
        def agent_status(target, actor):
            calls.append(target)
            return {"state": "idle", "recent_activity": "> \n"}
    q, fp = AgentControlAdapter("s:0.0", ac=AC).idle_evidence({})
    assert q is True and fp and len(calls) == 1

    class Staged(AC):
        @staticmethod
        def agent_status(target, actor):
            return {"state": "waiting_input", "recent_activity": "> half an envelope"}
    assert AgentControlAdapter("s:0.0", ac=Staged).idle_evidence({})[0] is False


def test_the_default_window_is_on():
    import importlib
    import os
    assert os.getenv("FULLCYCLE_QUIET_CONFIRM_SECS") in (None, "60")
    src = importlib.import_module("core.fullcycle.engine").__file__
    assert 'os.getenv("FULLCYCLE_QUIET_CONFIRM_SECS", "60")' in open(src).read()
