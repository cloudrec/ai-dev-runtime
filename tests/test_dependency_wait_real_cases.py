"""Acceptance on the real HU/Balt and WEST stops of 2026-11-17 (synthetic panes and DBs
only; the wording is taken from the live events, nothing live is read or touched).

* WEST declared its terminal wait in words #338 did not know (events 87094, 87495) and
  was re-woken and escalated every SLO window (87176, 87219).
* A new task delivered by the tech lead and finished between two sweeps must be
  announced even when the agent re-states its wait (changed task identity).
* Distinct actionable completions inside the wake cooldown are delayed, never lost
  (HU 87093 and WEST 87094 landed in the same second).
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw
from core import closed_loop_wake as clw
from core import wake_bridge as wb

HU = "gaika-country-hu:0.0"
WEST = "gaika-country-west:0.0"

# final messages as the agents wrote them (shortened)
WEST_87094 = ("● ICA SE - Next: only genuine owner captures remain, so I'm stopping. I did "
              "not write a handoff file, since this reply carries it.\n✻ Baked for 9s\n\n❯ \n")
WEST_87495 = ("● The only file changed is the report. I made no network requests, merges, "
              "pushes or agent runs, and the tests weren't repeated. Standing by until "
              "captures arrive.\n✻ Baked for 4s\n\n❯ \n")
HU_87893 = ("● The tests-and-handoff branch feat/hu-si-rs-liveplan-20261117 is done and with "
            "RU. Next: wait for owner captures (IKI, Auchan HU, Idea RS).\n✻ Baked for 6s\n"
            "\n❯ \n")
HU_87093 = ("● A visible \"observed at\" time in the plan would be a shared core/planner.js "
            "change. I did not make it, and it needs coordination first. The handoff is "
            "reports/HANDOFF_HU_SI_RS_LIVEPLAN_2026-11-17.md.\n✻ Baked for 5s\n\n❯ \n")
WORK = "✻ Working… (12s · esc to interrupt)\n● Обновляю сканер\n"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"type": etype, **kw})
        return {"event_id": len(self.calls)}


def _sweep(emit, target, tail, now, state="idle"):
    a = {"target": target, "cwd": "/opt/gaika-worktrees/x", "claude_cwd": "/opt/gaika-worktrees/x",
         "alive": True, "is_agent": True, "state": state}
    return aw.scan(agents=[a], read_fn=lambda t: tail, emit_fn=emit, now=now)


def _turn(emit, target, tail, t0, work_secs=20):
    _sweep(emit, target, WORK, t0, state="working")
    _sweep(emit, target, WORK + " ", t0 + work_secs, state="working")
    _sweep(emit, target, tail, t0 + work_secs + 1)
    return _sweep(emit, target, tail, t0 + work_secs + 2 + aw.QUIESCENT_SECS)


def _rest(emit, target, tail, t0):
    """The agent is seen only at rest: whatever it did happened between two sweeps."""
    _sweep(emit, target, tail, t0)
    return _sweep(emit, target, tail, t0 + 1 + aw.QUIESCENT_SECS)


def _types(emit):
    return [c["type"] for c in emit.calls]


# ── phrases ──────────────────────────────────────────────────────────────────────────
def test_the_real_west_and_hu_wordings_are_dependency_waits():
    for tail in (WEST_87094, WEST_87495, HU_87893):
        assert aw.declares_dependency_wait(tail), tail


def test_near_misses_are_not_waits():
    assert not aw.declares_dependency_wait(HU_87093)          # needs coordination: a stop
    for tail in ("● Next: I will process the captures that remain.\n❯ \n",
                 "● Only 3 captures remain in the queue, parsing them now.\n❯ \n",
                 "● Standing by until the owner approves the deploy.\n❯ \n",   # owner gate
                 "● Error: standing by until captures arrive, the upload failed.\n❯ \n"):
        assert not aw.declares_dependency_wait(tail), tail


# ── announced once; the watchdog does not re-wake or escalate it ─────────────────────
@pytest.mark.parametrize("tail", [WEST_87094, WEST_87495, HU_87893])
def test_a_terminal_wait_is_announced_once_then_parked(tail):
    emit, t = _Emit(), 1000.0
    _turn(emit, WEST, tail, t)
    assert _types(emit) == ["work_stopped_incomplete"]
    for i in range(1, 30):                    # every sweep for hours: nothing new
        _sweep(emit, WEST, tail, t + 400 + 60 * i)
    assert _types(emit) == ["work_stopped_incomplete"]


def _event_row(event_id, etype, target, now):
    conn, own = clw._conn(None)
    try:
        conn.execute("INSERT INTO event (id, ts, ts_epoch, source, type, agent_id) VALUES "
                     "(?, 'x', ?, 'agent_watch', ?, ?)", (event_id, now, etype, target))
        conn.commit()
    finally:
        if own:
            conn.close()


def test_repeated_watchdog_windows_do_not_rewake_a_declared_wait():
    """WEST 87094 -> 87176 (no_progress) -> 87219 (stalled): with the wait recognised the
    watch is retired at its first SLO check and every later window stays silent."""
    emit, t = _Emit(), 1000.0
    _turn(emit, WEST, WEST_87094, t)
    _event_row(87094, "work_stopped_incomplete", WEST, t)
    clw.register_delivery(event_id=87094, target=WEST, project_id="west-cart",
                          event_type="work_stopped_incomplete", now=t + 400)
    slo = _Emit()
    for k in range(1, 5):
        now = t + 400 + k * (clw.WAKE_LOOP_SLO_SECS + 1)
        _sweep(emit, WEST, WEST_87094, now - 5)
        r = clw.slo_scan(now=now, emit_fn=slo)
        assert not r["rewoken"] and not r["escalated"], (k, r)
    assert slo.calls == []


def test_without_the_new_wordings_the_same_stop_would_have_been_rewoken(monkeypatch):
    """The control: the 87094 stop with the wait NOT recognised is re-woken, as live."""
    monkeypatch.setattr(aw, "declares_dependency_wait", lambda tail: False)
    emit, t = _Emit(), 1000.0
    _turn(emit, WEST, WEST_87094, t)
    _event_row(87094, "work_stopped_incomplete", WEST, t)
    clw.register_delivery(event_id=87094, target=WEST, project_id="west-cart",
                          event_type="work_stopped_incomplete", now=t + 400)
    r = clw.slo_scan(now=t + 400 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=_Emit())
    assert r["rewoken"], r


# ── new evidence re-arms ─────────────────────────────────────────────────────────────
def test_a_different_stop_after_the_wait_is_announced():
    emit, t = _Emit(), 1000.0
    _turn(emit, HU, HU_87893, t)
    _turn(emit, HU, HU_87093, t + 1000)       # a real handoff with an open question
    assert _types(emit) == ["work_stopped_incomplete", "work_stopped_incomplete"]


def test_long_new_work_then_the_same_wait_is_announced():
    emit, t = _Emit(), 1000.0
    _turn(emit, HU, HU_87893, t)
    _turn(emit, HU, HU_87893.replace("419c", "51aa"), t + 1000,
          work_secs=aw.REARM_WORK_SECS + 30)
    assert len(emit.calls) == 2


def test_the_wait_lapses_after_its_ttl():
    """A restatement past the TTL is announced again; a frozen pane past the TTL is the
    closed loop's to escalate (test_agent_watch_dependency_wait reviewA)."""
    emit, t = _Emit(), 1000.0
    _turn(emit, HU, HU_87893, t)
    _turn(emit, HU, HU_87893, t + 1000)
    assert len(emit.calls) == 1
    _turn(emit, HU, HU_87893, t + aw.WAIT_TTL_SECS + 10)
    assert len(emit.calls) == 2


# ── changed task identity ────────────────────────────────────────────────────────────
NEW_TASK_DONE = ("● Сделал задачу техлида: добавил Spar SI в fleet, тесты 46/46. Next: wait "
                 "for owner captures (IKI).\n✻ Baked for 40s\n\n❯ \n")


def test_a_short_new_task_from_the_tech_lead_is_announced_even_if_never_seen_working():
    emit, t = _Emit(), 1000.0
    _turn(emit, HU, HU_87893, t)
    assert len(emit.calls) == 1
    aw.note_task_delivered("gaika-country-hu")            # agent_send by the API caller
    _rest(emit, HU, NEW_TASK_DONE, t + 600)                # finished between two sweeps
    assert _types(emit) == ["work_stopped_incomplete", "work_stopped_incomplete"]
    # and it is a declared wait again: further restatements stay parked
    for i in range(1, 10):
        _sweep(emit, HU, NEW_TASK_DONE, t + 1200 + 60 * i)
    assert len(emit.calls) == 2


def test_without_a_delivered_task_the_same_restatement_stays_parked():
    """The supervisor's idle nudge is not an API send: no re-arm, #338 still holds."""
    emit, t = _Emit(), 1000.0
    _turn(emit, HU, HU_87893, t)
    _rest(emit, HU, NEW_TASK_DONE, t + 600)
    assert len(emit.calls) == 1


def test_a_short_answer_to_a_plain_stop_is_announced_too():
    """Not only waits: a completed stop answered within one sweep is news after a task."""
    emit, t = _Emit(), 1000.0
    _turn(emit, WEST, HU_87093, t)
    aw.note_task_delivered(WEST)
    _rest(emit, WEST, HU_87093.replace("needs coordination first", "is now done"), t + 600)
    assert len(emit.calls) == 2


def test_note_task_delivered_touches_only_that_session():
    panes = {HU: HU_87893, WEST: WEST_87094, "gaika-country-hu_x:0.0": HU_87893}

    def sweep(emit, now, state="idle", tails=None):
        tails = tails or panes
        agents = [{"target": k, "cwd": "/opt/x", "claude_cwd": "/opt/x", "alive": True,
                   "is_agent": True, "state": state} for k in tails]
        return aw.scan(agents=agents, read_fn=lambda t: tails[t], emit_fn=emit, now=now)

    emit, t = _Emit(), 1000.0
    work = {k: WORK for k in panes}
    sweep(emit, t, "working", work)
    sweep(emit, t + 20, "working", {k: WORK + " " for k in panes})
    sweep(emit, t + 21)
    sweep(emit, t + 22 + aw.QUIESCENT_SECS)
    assert len(emit.calls) == 3
    # LIKE must treat "_" literally: "gaika-country-hu" must not reach "gaika-country-hu_x"
    assert aw.note_task_delivered("gaika-country-hu") == 1
    sweep(emit, t + 600)
    sweep(emit, t + 601 + aw.QUIESCENT_SECS)
    assert [c["agent_id"] for c in emit.calls[3:]] == [HU], emit.calls[3:]


def test_agent_send_re_arms_only_for_a_delivered_api_send(monkeypatch):
    from core import agent_control as ac
    seen = []
    monkeypatch.setattr(aw, "note_task_delivered", lambda target, **kw: seen.append(target))
    ac._note_task_delivered(HU, "api:bearer/mcp-token-3", None, {"delivered": True})
    ac._note_task_delivered(HU, "api:bearer", "x|send_queue", {"delivered": True})
    ac._note_task_delivered(HU, None, None, {"delivered": True})               # watchdog
    ac._note_task_delivered(HU, "agent_continuation_watchdog", None, {"delivered": True})
    ac._note_task_delivered(HU, "api:bearer", None, {"delivered": False, "duplicate": True})
    ac._note_task_delivered(HU, "api:bearer", None, {"refused": "working"})
    assert seen == [HU, HU]


def test_a_failing_watch_store_never_fails_the_send(monkeypatch):
    from core import agent_control as ac

    def boom(*a, **k):
        raise RuntimeError("db gone")
    monkeypatch.setattr(aw, "note_task_delivered", boom)
    ac._note_task_delivered(HU, "api:bearer", None, {"delivered": True})


# ── cooldown: distinct actionable completions are delayed, never lost ────────────────
def _decide(eid, target, now, etype="work_stopped_incomplete"):
    d = wb.should_wake(event_id=eid, severity="warning", event_type=etype,
                       correlation_id=f"agentwatch:{target}", project_id="",
                       agent_id=target, now=now)
    wb.record(d, event_id=eid, severity="warning", event_type=etype,
              correlation_id=f"agentwatch:{target}", project_id="", agent_id=target, now=now)
    return d


def _served(eid):
    conn, own = wb._conn(None)
    try:
        return conn.execute("SELECT COUNT(*) FROM wake_audit WHERE event_id=? AND "
                            "decision='wake'", (eid,)).fetchone()[0] > 0
    finally:
        if own:
            conn.close()


def _drain(now, rounds=6):
    """The companion: take the pending wake, submit it, wait the cooldown out, repeat."""
    order = []
    for _ in range(rounds):
        p = wb.pending_wake(now=now)
        if p.get("pending"):
            order.append(p["event_id"])
            wb.mark_submitted(p["event_id"], source="companion", now=now)
            wb.acknowledge(p["event_id"], now=now)
        now += wb.ACTIONABLE_COOLDOWN_SECS + 5
    return order


def test_hu_and_west_in_the_same_second_are_both_delivered():
    wb.bind_chat("https://chatgpt.com/c/synthetic-tech-lead")
    t = 1_000_000.0
    first = _decide(87093, HU, t)
    second = _decide(87094, WEST, t + 0.1)
    assert first["wake"] is True
    order = _drain(t + 1)
    assert sorted(order) == [87093, 87094], (second, order)


def test_a_new_completion_of_the_same_agent_inside_the_cooldown_is_not_lost():
    wb.bind_chat("https://chatgpt.com/c/synthetic-tech-lead")
    t = 1_000_000.0
    _decide(1, HU, t)
    assert _drain(t + 1, rounds=1) == [1]
    d = _decide(2, HU, t + 10)                 # task B finished 10 s after A was sent
    assert d["wake"] is False and d["reason"] == "actionable_cooldown_active"
    assert 2 in _drain(t + 11)
    assert _served(2)


def test_a_burst_from_one_agent_ends_on_its_newest_completion():
    wb.bind_chat("https://chatgpt.com/c/synthetic-tech-lead")
    t = 1_000_000.0
    _decide(1, HU, t)
    _drain(t + 1, rounds=1)
    for i, eid in enumerate((2, 3, 4)):
        _decide(eid, HU, t + 5 + i)
    order = _drain(t + 10)
    assert order and order[-1] == 4, order
    conn, own = wb._conn(None)
    try:
        for eid in (2, 3):                     # each older one: delivered or retired by 4
            delivered = eid in order
            retired = conn.execute(
                "SELECT COUNT(*) FROM wake_audit WHERE event_id=? AND superseded_by IS NOT "
                "NULL", (eid,)).fetchone()[0]
            assert delivered or retired, (eid, order)
    finally:
        if own:
            conn.close()
