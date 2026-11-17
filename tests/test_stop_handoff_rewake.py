"""Repeated work_stopped_incomplete / wake_loop_no_progress for agents that are done
(2026-11-17: 88186 -> 88198 for this very pane, 87669, 87835, 88001, 87778).

Synthetic panes and DBs only. Three kinds of stop are told apart:
* a completed hand-over (the next move is the owner's or the reviewer's): announced
  once, never re-woken or escalated while the pane is at rest;
* an external dependency wait: same (#338, test_dependency_wait_real_cases);
* genuine unfinished work or a failure: keeps waking exactly as before.
A new task delivered by the tech lead always makes the next stop news.
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw
from core import closed_loop_wake as clw

T = "owneros-autonomy-loop:0.0"
WORK = "✻ Working… (12s · esc to interrupt)\n● Обновляю сканер\n"


def _msg(text):
    return f"● {text}\n✻ Baked for 5s\n\n❯ \n"


# final messages as the agents wrote them (shortened)
H_88186 = _msg("Напиши задачу в ChatGPT и скажи мне «дал». Я начну следить.")
H_87669 = _msg("Решения от вас: запустить чек-лист на машине с Windows, и разрешить или нет "
               "выкатку e7d57a2.")
H_87835 = _msg("Остаётся от вас: запустить OBS и стол t1, чтобы проверить видео.")
H_EN = _msg("The branch is ready; merging it is your call.")
UNFINISHED = _msg("Сделал шаг 3 из 5: обновил план корзины, тесты прошли.")
FAILED = _msg("API Error: 529 overloaded. Request ID: req_synthetic. Решения от вас?")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"type": etype, **kw})
        return {"event_id": len(self.calls)}


def _sweep(emit, tail, now, state="idle", target=T):
    a = {"target": target, "cwd": "/opt/x", "claude_cwd": "/opt/x", "alive": True,
         "is_agent": True, "state": state}
    return aw.scan(agents=[a], read_fn=lambda t: tail, emit_fn=emit, now=now)


def _turn(emit, tail, t0, work_secs=20):
    _sweep(emit, WORK, t0, state="working")
    _sweep(emit, WORK + " ", t0 + work_secs, state="working")
    _sweep(emit, tail, t0 + work_secs + 1)
    return _sweep(emit, tail, t0 + work_secs + 2 + aw.QUIESCENT_SECS)


def _event(event_id, etype, now, source="agent_watch", target=T):
    conn, own = clw._conn(None)
    try:
        conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id) VALUES "
                     "(?, 'x', ?, ?, ?, ?)", (event_id, now, source, etype, target))
        conn.commit()
    finally:
        if own:
            conn.close()


def _windows(event_id, etype, t, tail, n=4, source="agent_watch"):
    """Register the delivered wake and run `n` SLO windows; the pane keeps `tail`."""
    _event(event_id, etype, t, source=source)
    clw.register_delivery(event_id=event_id, target=T, project_id="p",
                          event_type=etype, now=t + 400)
    out, emit, slo = [], _Emit(), _Emit()
    for k in range(1, n + 1):
        now = t + 400 + k * (clw.WAKE_LOOP_SLO_SECS + 1)
        _sweep(emit, tail, now - 5)
        out.append(clw.slo_scan(now=now, emit_fn=slo))
    return out, slo


# ── the detector ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tail", [H_88186, H_87669, H_87835, H_EN])
def test_real_handoffs_are_declared(tail):
    assert aw.declares_handoff(tail) and aw.declares_dependency_wait(tail)


@pytest.mark.parametrize("tail", [
    UNFINISHED,
    FAILED,                                                         # a failure always wakes
    _msg("Решения от вас не требуется, продолжаю."),                # negation
    _msg("Действий от владельца не требуется. Ветка готова."),     # negation alone
    _msg("Остаётся от вас: ничего. Продолжаю тесты."),              # carrying on
    _msg("Next I will continue with step 4; your call later."),     # carrying on
    _msg("Жду подтверждения владельца на деплой."),                 # owner gate: reminds
    _msg("Waiting for owner approval to deploy."),
    "$ grep log\nрешения от вас: (из лога)\n❯ \n",                   # not the agent's words
    _msg("Скажи мне, если что-то сломалось.\n● Запускаю тесты: 3 failed."),  # last bullet
])
def test_not_handoffs(tail):
    assert not aw.declares_handoff(tail), tail


# ── agent_watch: announced once ──────────────────────────────────────────────────────
def test_a_handoff_is_announced_once_and_restatements_are_parked():
    emit, t = _Emit(), 1000.0
    _turn(emit, H_88186, t)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]
    for i in range(1, 40):
        _sweep(emit, H_88186, t + 400 + 60 * i)
    assert len(emit.calls) == 1


def test_unfinished_work_is_still_announced_every_new_stop():
    emit, t = _Emit(), 1000.0
    _turn(emit, UNFINISHED, t)
    _turn(emit, UNFINISHED.replace("шаг 3", "шаг 4"), t + 1000)
    assert len(emit.calls) == 2


def test_a_failure_after_a_handoff_is_announced():
    emit, t = _Emit(), 1000.0
    _turn(emit, H_87669, t)
    _turn(emit, FAILED, t + 1000)
    assert len(emit.calls) == 2


def test_a_new_task_after_a_handoff_makes_the_next_stop_news():
    emit, t = _Emit(), 1000.0
    _turn(emit, H_88186, t)
    aw.note_task_delivered(T)                       # the tech lead sent the next task
    _sweep(emit, H_87835, t + 600)                  # done between two sweeps
    _sweep(emit, H_87835, t + 601 + aw.QUIESCENT_SECS)
    assert len(emit.calls) == 2


# ── closed loop: no re-wake, no escalation for a hand-over ───────────────────────────
@pytest.mark.parametrize("tail", [H_88186, H_87669, H_87835])
def test_88198_a_handoff_is_not_rewoken_or_escalated(tail):
    emit, t = _Emit(), 1000.0
    _turn(emit, tail, t)
    out, slo = _windows(88186, "work_stopped_incomplete", t, tail)
    assert all(not r["rewoken"] and not r["escalated"] for r in out), out
    assert slo.calls == []
    assert out[0]["deregistered"][0]["reason"] == "agent_dependency_wait"


def test_genuinely_unfinished_work_is_still_rewoken_then_escalated():
    emit, t = _Emit(), 1000.0
    _turn(emit, UNFINISHED, t)
    out, slo = _windows(500, "work_stopped_incomplete", t, UNFINISHED)
    assert any(r["rewoken"] for r in out) and any(r["escalated"] for r in out), out


def test_a_real_failure_is_never_suppressed():
    emit, t = _Emit(), 1000.0
    _turn(emit, FAILED, t)
    out, _ = _windows(501, "work_stopped_incomplete", t, FAILED)
    assert any(r["rewoken"] for r in out), out
    out, _ = _windows(502, "agent_process_failed", t, FAILED)
    assert not any(d.get("reason") in ("agent_dependency_wait", "stage_done_handoff")
                   for r in out for d in r["deregistered"]), out


# ── closed loop: a formal stage hand-over (tools/stage_done.py) ──────────────────────
def test_88001_a_stage_done_handoff_is_not_rewoken():
    emit, t = _Emit(), 1000.0
    _turn(emit, UNFINISHED, t)                      # wording says nothing; the event does
    out, slo = _windows(88001, "task_completed", t, UNFINISHED, source="agent_stage_done")
    assert all(not r["rewoken"] and not r["escalated"] for r in out), out
    assert out[0]["deregistered"][0]["reason"] == "stage_done_handoff"


def test_task_completed_from_another_emitter_keeps_its_watch():
    emit, t = _Emit(), 1000.0
    _turn(emit, UNFINISHED, t)
    out, _ = _windows(503, "task_completed", t, UNFINISHED, source="runtime_job")
    assert not any(d.get("reason") == "stage_done_handoff"
                   for r in out for d in r["deregistered"]), out


def test_a_stage_done_pane_that_crashed_keeps_its_watch():
    emit, t = _Emit(), 1000.0
    _turn(emit, UNFINISHED, t)
    conn, own = clw._conn(None)
    conn.execute("UPDATE agent_watch_state SET cls='crashed' WHERE target=?", (T,))
    conn.commit()
    _event(504, "task_completed", t, source="agent_stage_done")
    clw.register_delivery(event_id=504, target=T, project_id="p",
                          event_type="task_completed", now=t + 400)
    r = clw.slo_scan(now=t + 400 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=_Emit())
    assert not any(d.get("reason") == "stage_done_handoff" for d in r["deregistered"]), r
    if own:
        conn.close()
