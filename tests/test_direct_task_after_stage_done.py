"""A task submitted straight into the pane after a stage_done (ee9f1fa residual 2).

The owner types a new task in tmux minutes after the agent ran tools/stage_done.py; the
agent stops it unfinished inside REARM_WORK_SECS. ee9f1fa parked that stop as the old
hand-over. Evidence of a SUBMITTED prompt now comes from the session's own transcript
(`origin.kind == "human"`), attributed against agent_control deliveries. Synthetic
transcripts, panes and DBs only; nothing is typed anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

import pytest

from core import agent_control as ac
from core import agent_privilege as ap
from core import agent_watch as aw
from core import closed_loop_wake as clw
from core import native_sessions as ns

T = "gaika-country-west:0.0"
CWD = "/opt/x"
SID = "11111111-2222-3333-4444-555555555555"
OTHER_SID = "99999999-2222-3333-4444-555555555555"
WORK = "✻ Working… (12s · esc to interrupt)\n● Обновляю сканер\n"
CHROME = "\n✻ Baked for 5s\n\n────────────\n❯ {draft}\n────────────\n"
DONE = "● Этап готов, RU should decide on it." + CHROME.format(draft="")
UNFINISHED = "● Сделал шаг 1 из 3 по новой задаче." + CHROME.format(draft="")
SECRET_TASK = "synthetic-owner-task-7f3a: проверь корзину PL"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(ap, "claude_home", lambda: str(tmp_path))
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: SID if pid == 4242 else "")
    os.makedirs(tmp_path / ".claude" / "projects" / CWD.replace("/", "-"))
    ac._db().close()                              # the live delivery store always exists
    for sid in (SID, OTHER_SID):                  # a live session always has its file
        with open(_tpath(tmp_path, sid), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": _iso_ts(1000.0)}) + "\n")
    return tmp_path


def _tpath(tmp, sid=SID):
    return tmp / ".claude" / "projects" / CWD.replace("/", "-") / f"{sid}.jsonl"


def _iso_ts(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _line(tmp, epoch, text=SECRET_TASK, sid=SID, **kw):
    entry = {"type": "user", "timestamp": _iso_ts(epoch), "sessionId": sid,
             "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}
    entry.update(kw)
    with open(_tpath(tmp, sid), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"type": etype, **kw})
        return {"event_id": 9000 + len(self.calls)}


def _sweep(emit, tail, now, state="idle", pid=4242):
    a = {"target": T, "cwd": CWD, "claude_cwd": CWD, "alive": True, "is_agent": True,
         "state": state, "claude_pid": pid, "pid": pid}
    return aw.scan(agents=[a], read_fn=lambda t: tail, emit_fn=emit, now=now)


def _stage_done(epoch, event_id=1):
    conn, own = clw._conn(None)
    conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id, payload) "
                 "VALUES (?, 'x', ?, 'agent_stage_done', 'task_completed', ?, '{}')",
                 (event_id, epoch, T))
    conn.commit()
    if own:
        conn.close()


def _delivery(epoch, actor=None, key="k1", target=T, text=SECRET_TASK, fingerprint=True):
    ac._record_delivery(key, target, "agent_send", {"delivered": True}, actor=actor,
                        text_sha256=ac.text_fingerprint(text) if fingerprint else None)
    db = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    db.execute("UPDATE deliveries SET created_ts=? WHERE idempotency_key=?", (epoch, key))
    db.execute("UPDATE delivery_attribution SET recorded_ts=? WHERE idempotency_key=?",
               (epoch, key))
    db.commit()
    db.close()


def _scenario(_iso, submit=None, pid=4242):
    """The blind spot: the stage is handed over at t+10 (final output t+20); before the
    quiescence window ends, something is submitted at t+40, the agent works and stops at
    t+100 — 90 s after the stage_done, well inside REARM_WORK_SECS."""
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, state="working", pid=pid)
    _stage_done(t + 10)
    _sweep(emit, DONE, t + 20, pid=pid)
    if submit:
        submit(t + 40)
    _sweep(emit, WORK, t + 45, state="working", pid=pid)
    _sweep(emit, UNFINISHED, t + 100, pid=pid)
    r = _sweep(emit, UNFINISHED, t + 101 + aw.QUIESCENT_SECS, pid=pid)
    return emit, r, t


def _stopped(emit):
    return [c["type"] for c in emit.calls]


# ── the old failure, and the fix ─────────────────────────────────────────────────────────
def test_a_task_typed_in_tmux_after_stage_done_is_announced(_iso):
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "task:human_prompt_via_runtime"


def test_without_a_submitted_prompt_the_stop_stays_parked(_iso):
    emit, r, _ = _scenario(_iso)
    assert emit.calls == []
    assert {"target": T, "why": "dependency_wait_parked"} in r["skipped"] or \
        {"target": T, "why": "stage_done_handoff"} in r["skipped"], r


def test_it_is_announced_once_over_many_sweeps(_iso):
    emit, _, t = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    for i in range(1, 40):
        _sweep(emit, UNFINISHED, t + 800 + 60 * i)
    assert _stopped(emit) == ["work_stopped_incomplete"]


# ── what is NOT a submitted task ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("kw", [
    {"origin": {"kind": "task-notification", "producer": "session-task"}},
    {"origin": {"kind": "task-notification", "producer": "human"}},  # "human", wrong kind
    {"isMeta": True},
    {"isCompactSummary": True, "origin": None},
    {"origin": None},                                   # a local command's output
    {"message": {"role": "user", "content": [{"type": "tool_result", "content": "human"}]}},
])
def test_non_prompt_transcript_entries_do_not_count(_iso, kw):
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts, **kw))
    assert emit.calls == []


def test_an_unsent_draft_in_the_composer_does_not_count(_iso):
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, state="working")
    _stage_done(t + 10)
    _sweep(emit, DONE, t + 20)
    drafted = "● Этап готов, RU should decide on it." + CHROME.format(draft=SECRET_TASK)
    _sweep(emit, drafted, t + 21 + aw.QUIESCENT_SECS)
    _sweep(emit, drafted, t + 30 + 2 * aw.QUIESCENT_SECS)
    assert emit.calls == []
    with open(_tpath(_iso), encoding="utf-8") as fh:  # nothing was submitted
        assert '"human"' not in fh.read()


def test_an_internal_nudge_does_not_rearm(_iso):
    def submit(ts):
        _delivery(ts + 2, actor=None)                  # watchdog/orchestrator: unattributed
        _line(_iso, ts)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert emit.calls == []


def test_an_api_task_is_announced_once(_iso):
    def submit(ts):
        _delivery(ts + 2, actor="api:bearer/m")
        aw.note_task_delivered(T, now=ts + 2)
        _line(_iso, ts)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]


def test_a_nudge_long_before_the_prompt_does_not_mask_it(_iso):
    def submit(ts):
        _delivery(ts - aw.DELIVERY_MATCH_SECS - 30, actor=None, text="continue")
        _line(_iso, ts)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]


def test_a_prompt_before_the_stage_done_is_not_resurrected(_iso):
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, 10_000.0 + 5))  # t+5 < t+10
    assert emit.calls == []


def test_another_session_in_the_same_directory_does_not_count(_iso):
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts, sid=OTHER_SID))
    assert emit.calls == []


def test_an_unknown_session_is_announced_once_and_not_rewoken(_iso):
    """No identity at all: the stop is announced once (never parked silently, never
    guessed as a task) and its watch is the stage's rest, so it is not re-woken."""
    emit, _, t = _scenario(_iso, submit=lambda ts: _line(_iso, ts), pid=777)
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:session_unresolved"
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id, payload) "
                 "VALUES (501, 'x', ?, 'agent_watch', 'work_stopped_incomplete', ?, ?)",
                 (t + 402, T, json.dumps({"excerpt": "Сделал шаг 1 из 3"})))
    conn.commit()
    conn.close()
    clw.register_delivery(event_id=501, target=T, project_id="p",
                          event_type="work_stopped_incomplete", now=t + 410)
    out = [clw.slo_scan(now=t + 410 + k * (clw.WAKE_LOOP_SLO_SECS + 1), emit_fn=_Emit())
           for k in (1, 2, 3)]
    assert not any(r["rewoken"] or r["escalated"] for r in out), out


# ── persistence, closed loop, real prompts and failures ─────────────────────────────────
def test_the_evidence_persists_and_the_stop_watch_is_rewoken(_iso):
    emit, _, t = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])   # a fresh process's view
    task_ts = conn.execute("SELECT task_ts FROM agent_watch_state WHERE target=?",
                           (T,)).fetchone()[0]
    conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id, payload) "
                 "VALUES (500, 'x', ?, 'agent_watch', 'work_stopped_incomplete', ?, ?)",
                 (t + 700, T, json.dumps({"excerpt": "Сделал шаг 1 из 3"})))
    conn.commit()
    conn.close()
    assert abs(task_ts - (t + 40)) < 1
    clw.register_delivery(event_id=500, target=T, project_id="p",
                          event_type="work_stopped_incomplete", now=t + 710)
    out = [clw.slo_scan(now=t + 710 + k * (clw.WAKE_LOOP_SLO_SECS + 1), emit_fn=_Emit())
           for k in (1, 2, 3)]
    assert any(r["rewoken"] for r in out), out


def test_a_question_after_the_new_task_is_still_an_owner_prompt(_iso):
    ask = "● Подтвердите деплой?" + CHROME.format(draft="")
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, state="working")
    _stage_done(t + 10)
    _line(_iso, t + 30)
    _sweep(emit, ask, t + 40)
    assert _stopped(emit) == ["agent_prompt_needs_response"]


def test_a_failure_after_the_new_task_is_announced(_iso):
    fail = "● API Error: 529 overloaded." + CHROME.format(draft="")
    emit2, t = _Emit(), 50_000.0
    _sweep(emit2, WORK, t, state="working")
    _stage_done(t + 10, event_id=2)
    _sweep(emit2, fail, t + 20)
    _sweep(emit2, fail, t + 21 + aw.QUIESCENT_SECS)
    assert len(emit2.calls) == 1


def test_the_prompt_text_is_never_stored(_iso):
    _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    for db in ("CONTROL_PLANE_DB", "AGENT_CONTROL_DB"):
        conn = sqlite3.connect(os.environ[db])
        dump = "\n".join(conn.iterdump())
        conn.close()
        assert "synthetic-owner-task-7f3a" not in dump


# ── residuals of c1f629f ────────────────────────────────────────────────────────────────
# R1: a human prompt typed seconds after an automated nudge was attributed to the nudge.
def test_r1_a_human_prompt_right_after_a_nudge_is_a_task(_iso):
    def submit(ts):
        _delivery(ts - 10, actor=None, text="continue with the next safe step")
        _line(_iso, ts)                             # the owner's own, different text
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]


def test_r1_the_nudge_itself_is_still_not_a_task(_iso):
    nudge = "continue with the next safe step"
    def submit(ts):
        _delivery(ts + 3, actor=None, text=nudge)
        _line(_iso, ts, text=nudge)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert emit.calls == []


def test_r1_a_wrapped_paste_is_attributed_to_its_delivery(_iso):
    body = "line one of a long reminder\nline two"
    wrapped = f'<pasted_content id="ab12">\n{body}\n</pasted_content id="ab12">'
    def submit(ts):
        _delivery(ts + 3, actor=None, text=body)
        _line(_iso, ts, text=wrapped)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert emit.calls == []                         # an internal paste: still parked


def test_r1_an_unfingerprinted_delivery_at_that_moment_is_unknown(_iso):
    def submit(ts):
        _delivery(ts + 3, actor=None, fingerprint=False)
        _line(_iso, ts)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:unattributed_prompt"


# R2: the runtime's session list could not answer.
def _pidfile(tmp, pid, sid):
    d = tmp / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid}))


def test_r2_claude_codes_own_pid_record_resolves_the_session(_iso, monkeypatch):
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: "")
    _pidfile(_iso, 4242, SID)
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "task:human_prompt_via_pidfile"


def test_r2_a_pid_record_for_another_pid_is_ignored(_iso, monkeypatch):
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: "")
    d = _iso / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "4242.json").write_text(json.dumps({"pid": 1, "sessionId": SID}))
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:session_unresolved"


def test_r2_the_durable_identity_survives_a_lookup_outage(_iso, monkeypatch):
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, state="working")
    _stage_done(t + 10)
    _sweep(emit, DONE, t + 20)
    _sweep(emit, DONE, t + 21 + aw.QUIESCENT_SECS)  # resolved once, parked: identity kept
    assert emit.calls == []
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: "")  # outage
    _stage_done(t + 1000, event_id=2)
    _line(_iso, t + 1030)
    _sweep(emit, WORK, t + 1035, state="working")
    _sweep(emit, UNFINISHED, t + 1090)
    _sweep(emit, UNFINISHED, t + 1091 + aw.QUIESCENT_SECS)
    assert emit.calls[0]["payload"]["stage_done_evidence"] == \
        "task:human_prompt_via_checkpoint"


def test_r2_the_durable_identity_is_not_reused_for_a_new_process(_iso, monkeypatch):
    emit, t = _Emit(), 10_000.0
    _sweep(emit, WORK, t, state="working")
    _stage_done(t + 10)
    _sweep(emit, DONE, t + 20)
    _sweep(emit, DONE, t + 21 + aw.QUIESCENT_SECS)
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: "")
    _stage_done(t + 1000, event_id=2)
    _line(_iso, t + 1030)
    _sweep(emit, WORK, t + 1035, state="working", pid=5151)    # restarted claude
    _sweep(emit, UNFINISHED, t + 1090, pid=5151)
    _sweep(emit, UNFINISHED, t + 1091 + aw.QUIESCENT_SECS, pid=5151)
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:session_unresolved"


def test_r2_a_new_session_in_the_same_process_resets_the_checkpoint(_iso, monkeypatch):
    emit, t = _Emit(), 10_000.0
    _scenario(_iso)                                  # parks; identity + checkpoint stored
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    assert conn.execute("SELECT tr_sid FROM agent_watch_state").fetchone()[0] == SID
    conn.close()
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: OTHER_SID)  # /clear
    _stage_done(t + 2000, event_id=2)
    _line(_iso, t + 2030, sid=OTHER_SID)
    _sweep(emit, WORK, t + 2035, state="working")
    _sweep(emit, UNFINISHED, t + 2090)
    _sweep(emit, UNFINISHED, t + 2091 + aw.QUIESCENT_SECS)
    assert _stopped(emit) == ["work_stopped_incomplete"]
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    assert conn.execute("SELECT tr_sid FROM agent_watch_state").fetchone()[0] == OTHER_SID
    conn.close()


# R3: much output after the prompt; a long history before it.
def _bulk(tmp, epoch_from, epoch_to, mb, sid=SID):
    n = max(1, int(mb * 1024 * 1024 / 2048))
    pad = "y" * 1900
    with open(_tpath(tmp, sid), "a", encoding="utf-8") as fh:
        for i in range(n):
            ts = epoch_from + (epoch_to - epoch_from) * i / n
            fh.write(json.dumps({"type": "assistant", "timestamp": _iso_ts(ts),
                                 "message": {"content": pad}}) + "\n")


def test_r3_a_prompt_followed_by_megabytes_of_output_is_found(_iso):
    def submit(ts):
        _line(_iso, ts)
        _bulk(_iso, ts + 1, ts + 50, 3)              # 3 MB after the prompt
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]


def test_r3_a_long_history_is_skipped_not_read(_iso, monkeypatch):
    _bulk(_iso, 1000.0, 9_000.0, 24)                 # 24 MB written before the stage
    read = {"n": 0}
    real_open = ap.open_regular

    def counting(path, mode="r", **kw):
        fh = real_open(path, mode, **kw)
        orig = fh.read

        def rd(*a):
            b = orig(*a)
            read["n"] += len(b)
            return b
        fh.read = rd
        return fh
    monkeypatch.setattr(ap, "open_regular", counting)
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert read["n"] < 4 * 1024 * 1024, read        # binary search, not 24 MB


def test_r3_more_than_the_budget_since_the_stage_is_unknown(_iso, monkeypatch):
    monkeypatch.setattr(aw._te, "SCAN_BUDGET_BYTES", 1024 * 1024)
    def submit(ts):
        _bulk(_iso, ts - 25, ts - 1, 3)               # 3 MB between stage and prompt
        _line(_iso, ts)
    emit, _, _ = _scenario(_iso, submit=submit)
    assert _stopped(emit) == ["work_stopped_incomplete"]
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:transcript_backlog"


def test_r3_the_checkpoint_is_persisted_before_the_stage(_iso):
    _bulk(_iso, 1000.0, 9_000.0, 2)
    _scenario(_iso)
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    off, off_ts = conn.execute("SELECT tr_offset, tr_offset_ts FROM agent_watch_state"
                               ).fetchone()
    conn.close()
    assert off and off_ts <= 10_010.0               # never past the stage_done


def test_r2_a_new_sessions_file_is_read_from_its_start_not_the_old_offset(_iso, monkeypatch):
    _bulk(_iso, 1000.0, 9_000.0, 2)                  # old session: a 2 MB history
    _scenario(_iso)                                  # checkpoint ~2 MB into the old file
    monkeypatch.setattr(ns, "session_id_for_pid", lambda pid, now=None: OTHER_SID)  # /clear
    emit, t = _Emit(), 10_000.0
    _stage_done(t + 2000, event_id=2)
    _line(_iso, t + 2030, sid=OTHER_SID)             # near the start of the new file
    _bulk(_iso, t + 2031, t + 2080, 3, sid=OTHER_SID)
    _sweep(emit, WORK, t + 2035, state="working")
    _sweep(emit, UNFINISHED, t + 2090)
    _sweep(emit, UNFINISHED, t + 2091 + aw.QUIESCENT_SECS)
    assert _stopped(emit) == ["work_stopped_incomplete"]


def test_r3_the_checkpoint_never_passes_the_stage_even_with_later_output(_iso):
    _bulk(_iso, 1000.0, 9_000.0, 1)
    _scenario(_iso, submit=lambda ts: _bulk(_iso, ts, ts + 50, 1))  # output, no prompt
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    off_ts = conn.execute("SELECT tr_offset_ts FROM agent_watch_state").fetchone()[0]
    conn.close()
    assert off_ts is not None and off_ts <= 10_010.0


def test_r1_an_unreadable_delivery_store_is_unknown_not_human(_iso, monkeypatch):
    monkeypatch.setattr(ac, "_db_path", lambda: str(_iso / "missing" / "ac.db"))
    emit, _, _ = _scenario(_iso, submit=lambda ts: _line(_iso, ts))
    assert emit.calls[0]["payload"]["stage_done_evidence"] == "unknown:unattributed_prompt"
