"""Recurring watchdog wakes after the de94158 release (2026-11-17 20:00Z).

Replays, on synthetic panes and DBs only, the three live chains:
* HU 88281 -> 88309/88316, WEST 88302 -> 88314/88321 (and PL 88303 -> 88315/88322): the
  agent handed its stage over with tools/stage_done.py; five minutes later agent_watch
  announced the same rest again as `work_stopped_incomplete`, and that watch, not being a
  `task_completed`, was re-woken and escalated.
* owneros-autonomy-loop 88292/88295 -> 88312/88313/88317/88318 (and 88326): the release
  report said "Подтвердится на деле…"; the bare stem `подтверд` made it an owner prompt,
  which re-woke itself and kept the stage_done watch from resolving.
Timings are the live ones (stage_done 30-150 s before the final output; quiescence 300 s).
"""
from __future__ import annotations

import json

import pytest

from core import agent_watch as aw
from core import closed_loop_wake as clw

HU = "gaika-country-hu:0.0"
WEST = "gaika-country-west:0.0"
LOOP = "owneros-autonomy-loop:0.0"
WORK = "✻ Working… (12s · esc to interrupt)\n● Обновляю сканер\n"
CHROME = ("\n✻ Baked for 5s\n\n────────────\n❯ {draft}\n────────────\n"
          "  ⏵⏵ accept edits on · ✔ Update installed · /restart to apply\n")


def _pane(text, draft=""):
    return f"● {text}" + CHROME.format(draft=draft)


H_HU = _pane("I did not touch RU's quarantine work. The frozen candidate is unchanged. "
             "I ran no new tests, made no network calls, did not push. I'm standing by "
             "for captures or a re-review request.")
H_WEST = _pane("What it needs: a planner rule that compares price within a freshness "
               "window. That is a planner change, outside this task, and RU should decide "
               "on it.")
R_88295 = _pane("Техлиду сообщил (событие 88292). Подтвердится на деле, когда очередной "
                "агент закончит и передаст работу: должно прийти одно уведомление.")
UNFINISHED = _pane("Сделал шаг 3 из 5: обновил план корзины, тесты прошли.")
FAILED = _pane("API Error: 529 overloaded. Request ID: req_synthetic.")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))


PARKED = ("agent_dependency_wait", "stage_done_handoff")


def _old_code_state(target):
    """What de94158 left behind: the stop announced, no declared wait."""
    conn, own = clw._conn(None)
    conn.execute("UPDATE agent_watch_state SET wait_since=NULL, notified_cls='quiescent' "
                 "WHERE target=?", (target,))
    conn.commit()
    if own:
        conn.close()


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"type": etype, **kw})
        return {"event_id": 9000 + len(self.calls)}


def _sweep(emit, tail, now, target, state="idle"):
    a = {"target": target, "cwd": "/opt/x", "claude_cwd": "/opt/x", "alive": True,
         "is_agent": True, "state": state}
    return aw.scan(agents=[a], read_fn=lambda t: tail, emit_fn=emit, now=now)


def _event(event_id, etype, ts, target, source="agent_watch", payload=None):
    conn, own = clw._conn(None)
    try:
        conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id, payload) "
                     "VALUES (?, 'x', ?, ?, ?, ?, ?)",
                     (event_id, ts, source, etype, target, json.dumps(payload or {})))
        conn.commit()
    finally:
        if own:
            conn.close()


def _turn(emit, tail, t0, target, stage_done_at=None, stage_id=None, work_secs=150):
    """A turn that works `work_secs`, optionally runs stage_done at `stage_done_at`, prints
    its final message and rests past the quiescence window."""
    _sweep(emit, WORK, t0, target, state="working")
    if stage_done_at is not None:
        _event(stage_id, "task_completed", stage_done_at, target, source="agent_stage_done")
    _sweep(emit, WORK + " ", t0 + work_secs, target, state="working")
    _sweep(emit, tail, t0 + work_secs + 1, target)
    return _sweep(emit, tail, t0 + work_secs + 2 + aw.QUIESCENT_SECS, target)


def _slo(event_id, etype, t, target, n=4):
    clw.register_delivery(event_id=event_id, target=target, project_id="p",
                          event_type=etype, now=t)
    slo, out = _Emit(), []
    for k in range(1, n + 1):
        out.append(clw.slo_scan(now=t + k * (clw.WAKE_LOOP_SLO_SECS + 1), emit_fn=slo))
    return out, slo


# ── chain 1: a stage_done hand-over announced a second time ─────────────────────────────
@pytest.mark.parametrize("target,tail,lead", [(HU, H_HU, 90), (WEST, H_WEST, 143)])
def test_a_stage_done_rest_is_not_announced_again(target, tail, lead):
    emit, t = _Emit(), 10_000.0
    r = _turn(emit, tail, t, target, stage_done_at=t + 151 - lead, stage_id=1)
    assert emit.calls == [], emit.calls
    assert {"target": target, "why": "stage_done_handoff"} in r["skipped"]
    for i in range(1, 40):                          # the pane just sits there
        _sweep(emit, tail, t + 600 + 60 * i, target)
    assert emit.calls == []


@pytest.mark.parametrize("target,tail", [(HU, H_HU), (WEST, H_WEST)])
def test_the_stage_done_watch_itself_is_resolved_not_escalated(target, tail):
    emit, t = _Emit(), 10_000.0
    _turn(emit, tail, t, target, stage_done_at=t + 100, stage_id=88277)
    out, slo = _slo(88277, "task_completed", t + 200, target)
    assert slo.calls == [] and all(not r["rewoken"] for r in out), out
    assert out[0]["deregistered"][0]["reason"] in PARKED


@pytest.mark.parametrize("target,tail", [(HU, H_HU), (WEST, H_WEST)])
def test_a_watch_opened_before_the_release_is_resolved(target, tail):
    """88281/88302 were already announced (old code, or a sweep that raced stage_done)."""
    emit, t = _Emit(), 10_000.0
    _turn(emit, tail, t, target, stage_done_at=t + 100, stage_id=88277)
    _event(88281, "work_stopped_incomplete", t + 452, target,
           payload={"class": "quiescent", "excerpt": tail[-200:]})
    for i in range(1, 30):                          # the restart: new connections, same DB
        _sweep(emit, tail, t + 460 + 60 * i, target)
    _old_code_state(target)
    out, slo = _slo(88281, "work_stopped_incomplete", t + 500, target)
    assert slo.calls == [], slo.calls
    assert out[0]["deregistered"][0]["reason"] == "stage_done_handoff"


def test_without_stage_done_the_same_stop_still_rewakes_and_escalates():
    emit, t = _Emit(), 10_000.0
    _turn(emit, H_WEST, t, WEST)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]
    _event(88302, "work_stopped_incomplete", t + 452, WEST,
           payload={"excerpt": H_WEST[-200:]})
    out, _ = _slo(88302, "work_stopped_incomplete", t + 500, WEST)
    assert any(r["rewoken"] for r in out) and any(r["escalated"] for r in out), out


def test_a_stage_done_older_than_the_new_work_does_not_cover_the_stop():
    emit, t = _Emit(), 10_000.0
    _turn(emit, UNFINISHED, t, WEST, stage_done_at=t - 2000, stage_id=1, work_secs=900)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]


@pytest.mark.parametrize("delay", [30, 500])
def test_a_task_delivered_after_stage_done_makes_the_next_stop_news(delay):
    """Even a quick task stopping within the stage_done window is the next task's news."""
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, WEST, state="working")
    _event(1, "task_completed", t + 10, WEST, source="agent_stage_done")
    _sweep(emit, H_WEST, t + 20, WEST)
    _sweep(emit, H_WEST, t + 21 + aw.QUIESCENT_SECS, WEST)
    assert emit.calls == []
    t1 = t + 21 + aw.QUIESCENT_SECS + delay
    aw.note_task_delivered(WEST, now=t1)            # the tech lead sends the next task
    _sweep(emit, UNFINISHED, t1 + 5, WEST)          # finished between two sweeps
    _sweep(emit, UNFINISHED, t1 + 6 + aw.QUIESCENT_SECS, WEST)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]


def test_a_task_delivered_inside_the_stage_done_window_is_news():
    """The tech lead answers the stage at once; the agent finishes the new task in seconds,
    so its stop sits well inside REARM_WORK_SECS of the old stage_done."""
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, WEST, state="working")
    _event(1, "task_completed", t + 10, WEST, source="agent_stage_done")
    _sweep(emit, H_WEST, t + 20, WEST)
    aw.note_task_delivered(WEST, now=t + 60)
    _sweep(emit, UNFINISHED, t + 70, WEST)
    _sweep(emit, UNFINISHED, t + 71 + aw.QUIESCENT_SECS, WEST)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]


def test_a_failure_after_stage_done_is_announced_and_keeps_waking():
    emit, t = _Emit(), 10_000.0
    _turn(emit, FAILED, t, WEST, stage_done_at=t + 100, stage_id=1)
    assert len(emit.calls) == 1
    _event(700, "work_stopped_incomplete", t + 452, WEST,
           payload={"excerpt": "API Error: 529 overloaded."})
    out, _ = _slo(700, "work_stopped_incomplete", t + 500, WEST)
    assert any(r["rewoken"] for r in out), out


def test_another_agents_stage_done_does_not_cover_this_pane():
    emit, t = _Emit(), 10_000.0
    _event(1, "task_completed", t + 100, HU, source="agent_stage_done")
    _turn(emit, H_WEST, t, WEST)
    assert len(emit.calls) == 1


def test_a_task_completed_from_another_emitter_does_not_cover():
    emit, t = _Emit(), 10_000.0
    _event(2, "task_completed", t + 100, WEST, source="runtime_job")
    _turn(emit, H_WEST, t, WEST, stage_done_at=None)
    assert len(emit.calls) == 1


def test_a_crash_after_stage_done_is_announced():
    emit, t = _Emit(), 10_000.0
    _turn(emit, H_WEST, t, WEST, stage_done_at=t + 100, stage_id=1)
    a = {"target": WEST, "alive": False, "is_agent": True, "state": ""}
    aw.scan(agents=[a], read_fn=lambda _t: "", emit_fn=emit, now=t + 900)
    assert [c["type"] for c in emit.calls] == ["agent_process_failed"]


# ── chain 2: a statement read as an owner prompt ─────────────────────────────────────────
def test_88295_a_statement_with_the_stem_is_not_an_owner_prompt():
    c = aw.classify(R_88295, state="idle", prev_cls="working", quiet_secs=400)
    assert c["cls"] != "owner_prompt", c


@pytest.mark.parametrize("text", ["Подтвердите деплой?", "Подтвердить выкатку?",
                                  "Подтверди, пожалуйста, деплой.",
                                  "Подтверждаете отключение старой ветки?"])
def test_a_real_confirmation_request_is_still_an_owner_prompt(text):
    c = aw.classify(_pane(text), state="idle", prev_cls="working", quiet_secs=400)
    assert c["cls"] == "owner_prompt", c


@pytest.mark.parametrize("text", ["Подтвердилось: 9 из 9 строк скрыты.",
                                  "Владелец подтвердил деплой, выкатил.",
                                  "Тесты подтвердили фикс."])
def test_past_and_future_statements_are_not_prompts(text):
    c = aw.classify(_pane(text), state="idle", prev_cls="working", quiet_secs=400)
    assert c["cls"] != "owner_prompt", c


def test_88292_88295_replay_no_rewake_no_escalation():
    emit, t = _Emit(), 10_000.0
    r = _turn(emit, R_88295, t, LOOP, stage_done_at=t + 140, stage_id=88292)
    assert emit.calls == [], emit.calls                # neither prompt nor second stop
    assert {"target": LOOP, "why": "stage_done_handoff"} in r["skipped"]
    out, slo = _slo(88292, "task_completed", t + 200, LOOP)
    assert slo.calls == [], slo.calls
    assert out[0]["deregistered"][0]["reason"] in PARKED


# ── stale, unsubmitted composer text (owner draft or the CLI's suggested prompt) ───────
@pytest.mark.parametrize("draft", ["Подтвердите деплой?", "что дальше делаем?",
                                   "do you want to proceed (y/n)",
                                   "1. Yes", "Подтвердите\n  деплой de94158?"])
def test_composer_text_is_never_read_as_the_agents_prompt(draft):
    c = aw.classify(_pane("Готово. Ветка на месте.", draft=draft), state="idle",
                    prev_cls="working", quiet_secs=400)
    assert c["cls"] != "owner_prompt", (draft, c)


def test_composer_text_is_left_alone_and_not_submitted():
    """The watcher only reads: a scan with an owner draft in the composer emits at most
    one event and never types into the pane (read_fn is the only pane access)."""
    emit, t = _Emit(), 10_000.0
    tail = _pane("Сделал шаг 3 из 5.", draft="Подтвердите деплой?")
    _turn(emit, tail, t, WEST)
    assert [c["type"] for c in emit.calls] == ["work_stopped_incomplete"]
    assert "Подтвердите" not in emit.calls[0]["payload"]["excerpt"]


# ── the HU wording itself ────────────────────────────────────────────────────────────────
def test_hu_standing_by_for_captures_is_a_declared_wait():
    assert aw.declares_dependency_wait(H_HU)
    assert not aw.declares_dependency_wait(_pane("Standing by for captures failed: error."))
