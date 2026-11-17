"""Backlog #338 (2026-11-17, HU/WEST): an agent that declared a dependency/terminal wait
is announced once; restatements of the wait are not new events, re-wakes or
escalations. Real progress or a different kind of stop re-arms. Synthetic panes only."""
from __future__ import annotations

import pytest

from core import agent_watch as aw

T = "gaika-country-hu:0.0"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"type": etype, **kw})
        return {"event_id": len(self.calls)}


def _wait(n):
    return (f"● Принято. Handoff закрыт, кандидат {n}. Ждём авторизованные захваты IKI "
            f"и Auchan HU либо новое задание.\n✻ Baked for {n}s\n\n❯ \n")


def _plain(n):
    return f"● Сделал шаг {n}: обновил план корзины, тесты прошли.\n✻ Baked for 3s\n\n❯ \n"


WORK = "✻ Working… (12s · esc to interrupt)\n● Обновляю сканер\n"


def _sweep(emit, tail, now, state="idle"):
    a = {"target": T, "cwd": "/opt/gaika-worktrees/hu", "claude_cwd": "/opt/gaika-worktrees/hu",
         "alive": True, "is_agent": True, "state": state}
    return aw.scan(agents=[a], read_fn=lambda t: tail, emit_fn=emit, now=now)


def _turn(emit, tail, t0, work_secs=20):
    """One agent turn: working for `work_secs`, then at rest past the quiescence dwell."""
    _sweep(emit, WORK, t0, state="working")
    _sweep(emit, WORK + " ", t0 + work_secs, state="working")
    _sweep(emit, tail, t0 + work_secs + 1)
    return _sweep(emit, tail, t0 + work_secs + 2 + aw.QUIESCENT_SECS)


_settle = _turn


def test_the_phrase_detector():
    assert aw.declares_dependency_wait(_wait(1))
    assert aw.declares_dependency_wait("Next: wait for owner captures (IKI).\n❯ \n")
    assert aw.declares_dependency_wait("This is a normal dependency wait, not a failure.\n")
    assert not aw.declares_dependency_wait(_plain(1))


def test_a_declared_wait_is_announced_once_and_restatements_are_not_news():
    emit, t = _Emit(), 1000.0
    _settle(emit, _wait(1), t)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]
    for i in range(2, 8):                      # each turn re-states the wait in new words
        t += 3600
        r = _settle(emit, _wait(i), t)
        assert any(s["why"] == "dependency_wait_parked" for s in r["skipped"]), r
    assert len(emit.calls) == 1


def test_real_work_rearms_and_the_next_wait_is_announced_again():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    _turn(emit, _wait(2), t + 2000, work_secs=aw.REARM_WORK_SECS + 60)   # a real task
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"] * 2


def test_a_quick_turn_that_restates_the_wait_does_not_rearm():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    _turn(emit, _wait(2), t + 2000, work_secs=aw.REARM_WORK_SECS - 60)
    assert len(emit.calls) == 1


def test_a_different_kind_of_stop_rearms():
    emit, t = _Emit(), 1000.0
    _settle(emit, _wait(1), t)
    _settle(emit, _plain(2), t + 2000)                      # a plain stop: genuine news
    assert len(emit.calls) == 2


def test_a_question_on_screen_is_never_parked():
    emit, t = _Emit(), 1000.0
    _settle(emit, _wait(1), t)
    menu = ("Ждём решения. Do you want to proceed?\n❯ 1. Yes\n  2. No\n")
    _sweep(emit, menu, t + 2000, state="waiting_owner")
    assert emit.calls[-1]["type"] == "agent_prompt_needs_response"


def test_a_crash_is_never_parked():
    emit, t = _Emit(), 1000.0
    _settle(emit, _wait(1), t)
    a = {"target": T, "cwd": "/x", "claude_cwd": "/x", "alive": False, "is_agent": True,
         "state": "dead"}
    aw.scan(agents=[a], read_fn=lambda _t: "", emit_fn=emit, now=t + 2000)
    assert emit.calls[-1]["type"] in ("agent_process_failed", "agent_crashed",
                                      "agent_dead"), emit.calls[-1]


def test_closed_loop_does_not_rewake_a_declared_wait():
    from core import closed_loop_wake as clw
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    conn = aw._conn(None)[0]
    assert clw._dependency_wait(conn, T)
    _turn(emit, _plain(2), t + 2000)                        # a plain stop: re-armed
    assert not clw._dependency_wait(conn, T)
