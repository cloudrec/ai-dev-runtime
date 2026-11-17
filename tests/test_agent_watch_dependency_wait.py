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
    assert aw.declares_dependency_wait("● Next: wait for owner captures (IKI).\n❯ \n")
    assert aw.declares_dependency_wait("● Готово, ждём новое задание.\n❯ \n")
    assert not aw.declares_dependency_wait(_plain(1))
    # owner gates are not dependency waits: they must keep reminding
    for gate in ("● Жду подтверждения владельца на деплой.\n❯ \n",
                 "● Waiting for owner approval to deploy.\n❯ \n",
                 "● Ожидаю решения техлида.\n❯ \n"):
        assert not aw.declares_dependency_wait(gate), gate
    # only the agent's final message counts, and never an error line
    assert not aw.declares_dependency_wait(
        "● Ждём новое задание.\n✻ Baked\n● Запустил тесты: 3 failed.\n❯ \n")
    assert not aw.declares_dependency_wait(
        "● Error: ждём новое задание от сервера, соединение упало.\n❯ \n")
    assert not aw.declares_dependency_wait(
        "$ tail log\nждём новое задание (из лога)\n❯ \n")


def test_a_declared_wait_is_announced_once_and_restatements_are_not_news():
    emit, t = _Emit(), 1000.0
    _settle(emit, _wait(1), t)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]
    for i in range(2, 8):                      # each turn re-states the wait in new words
        t += 1800                              # all within the wait's TTL
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
    assert clw._dependency_wait(conn, T, t + 600)
    _turn(emit, _plain(2), t + 2000)                        # a plain stop: re-armed
    assert not clw._dependency_wait(conn, T, t + 2600)



# ── 2026-11-17 security review of #338 ──────────────────────────────────────
def _types(emit):
    return [c["type"] for c in emit.calls]


def test_review1_an_owner_gate_blocker_keeps_reminding():
    emit, t = _Emit(), 1000.0
    gate = "● Жду подтверждения владельца на деплой. Нужно ваше решение.\n✻ Baked\n\n❯ \n"
    _turn(emit, gate, t)
    n = len(emit.calls)
    assert n >= 1
    _sweep(emit, gate, t + 400 + aw.REMINDER_SECS + 1)
    assert len(emit.calls) == n + 1, _types(emit)          # hourly reminder still fires


def test_review2_a_short_tasks_completion_is_announced_inside_a_wait():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    done = "● Готово: исправил тест и закоммитил. Ждём новое задание.\n✻ Baked\n\n❯ \n"
    _turn(emit, done, t + 2000, work_secs=60)
    assert len(emit.calls) == 2, _types(emit)


def test_review3_a_wait_then_a_blocker_rearms():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    blocker = "● Не могу продолжить: нужен доступ к репозиторию, жду инструкций.\n❯ \n"
    _turn(emit, blocker, t + 2000)
    assert len(emit.calls) == 2, _types(emit)
    _turn(emit, _wait(3), t + 4000)                         # and the next wait is news again
    assert len(emit.calls) == 3, _types(emit)


def test_review4_a_wait_lapses_after_its_ttl():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    _turn(emit, _wait(2), t + 1000)
    assert len(emit.calls) == 1
    _turn(emit, _wait(3), t + aw.WAIT_TTL_SECS + 10)
    assert len(emit.calls) == 2


def test_review4_closed_loop_stops_resolving_after_the_ttl():
    from core import closed_loop_wake as clw
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    conn = aw._conn(None)[0]
    since = conn.execute("SELECT wait_since FROM agent_watch_state WHERE target=?",
                         (T,)).fetchone()[0]
    assert clw._dependency_wait(conn, T, since + 10)
    assert not clw._dependency_wait(conn, T, since + aw.WAIT_TTL_SECS + 1)


def test_review5_a_pane_that_leaves_drops_its_wait():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    other = {"target": "other:0.0", "cwd": "/x", "claude_cwd": "/x", "alive": True,
             "is_agent": True, "state": "idle"}
    aw.scan(agents=[other], read_fn=lambda _t: "", emit_fn=emit, now=t + 1000)
    conn = aw._conn(None)[0]
    assert conn.execute("SELECT wait_since FROM agent_watch_state WHERE target=?",
                        (T,)).fetchone()[0] is None


def test_review7_no_external_wait_is_declared_so_stall_doctor_still_sees_the_pane():
    from core import native_supervisor as ns
    from core import stall_doctor as sd
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    conn = aw._conn(None)[0]
    assert not ns.in_external_wait(T, conn=conn, now=t + 500)
    assert not sd.in_declared_external_wait(T, conn=conn, now=t + 500)


def test_reviewA_a_frozen_pane_past_the_ttl_is_not_reparked_and_can_escalate():
    from core import closed_loop_wake as clw
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    conn = aw._conn(None)[0]
    since = conn.execute("SELECT wait_since FROM agent_watch_state WHERE target=?",
                         (T,)).fetchone()[0]
    late = t + aw.WAIT_TTL_SECS + 100
    for k in range(3):                              # the same frozen pane, sweep after sweep
        _sweep(emit, _wait(1), late + 60 * k)
    assert conn.execute("SELECT wait_since FROM agent_watch_state WHERE target=?",
                        (T,)).fetchone()[0] == since          # never renewed
    assert clw._dependency_wait(conn, T, since + 60)
    assert not clw._dependency_wait(conn, T, late + 300)      # re-wake/escalation resume
    assert len(emit.calls) == 1


def test_reviewA_a_restatement_after_the_ttl_is_announced_once_then_parks_again():
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    late = t + aw.WAIT_TTL_SECS + 100
    _turn(emit, _wait(2), late)
    assert len(emit.calls) == 2
    _turn(emit, _wait(3), late + 1000)
    assert len(emit.calls) == 2


def test_closed_loop_resolves_on_the_idle_pane_after_the_announced_wait(monkeypatch):
    from core import closed_loop_wake as clw
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    _sweep(emit, _wait(1), t + 1000)               # unchanged pane: reclassified idle
    conn = aw._conn(None)[0]
    assert conn.execute("SELECT cls FROM agent_watch_state WHERE target=?",
                        (T,)).fetchone()[0] == "idle"
    conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id) "
                 "VALUES (?,?,?,?,?,?)", (900, "x", t + 400, "agent_watch",
                                          "work_stopped_incomplete", T))
    conn.commit()
    assert clw._resolution_reason(conn, event_id=900, target=T, agents=[],
                                  now=t + 1100) == "agent_dependency_wait"


def test_review6_a_failure_or_question_watch_during_a_wait_still_wakes():
    from core import closed_loop_wake as clw
    emit, t = _Emit(), 1000.0
    _turn(emit, _wait(1), t)
    _sweep(emit, _wait(1), t + 1000)               # unchanged pane: idle, wait live
    conn = aw._conn(None)[0]
    for eid, etype in ((901, "agent_process_failed"), (902, "agent_prompt_needs_response"),
                       (903, "agent_waiting_input")):
        conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id) "
                     "VALUES (?,?,?,?,?,?)", (eid, "x", t + 1050, "hook", etype, T))
    conn.commit()
    for eid in (901, 902, 903):
        assert clw._resolution_reason(conn, event_id=eid, target=T, agents=[],
                                      now=t + 1100) != "agent_dependency_wait", eid
