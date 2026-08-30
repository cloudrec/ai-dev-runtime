"""The actionable waiting class: emitted on an edge, ranked above history, never spammed.

Every test here corresponds to a proven failure of 2026-08-13 03:58–04:10 UTC, when
`payorch-sbp-resumed` repeatedly entered waiting_input and the owner had to ping the chat by
hand: the transition emitted no actionable event at all, and the one send the 900s cooldown
allowed went to an event from two days earlier.
"""
from __future__ import annotations

import pytest

from core import wake_bridge as wb
from core.control_plane import waiting_transitions as wt

AGENT = "payorch-sbp-resumed:0.0"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat("https://chatgpt.com/c/default-test-chat")
    yield


class _Emitter:
    """Stand-in for cto.emit that hands out increasing ids and remembers what it was told."""

    def __init__(self):
        self.calls = []
        self._next = 1000

    def __call__(self, source, type, **kw):
        self._next += 1
        self.calls.append({"source": source, "type": type, **kw})
        return {"event_id": self._next, "pushed": False, "notification": None}


def _wake(event_id, *, event_type="", severity="critical", now=1000.0, **kw):
    d = wb.should_wake(event_id=event_id, severity=severity, event_type=event_type, now=now, **kw)
    wb.record(d, event_id=event_id, severity=severity, event_type=event_type, now=now)
    return d


# ── the missing event: edge-triggered emission ──────────────────────────────
def test_working_to_waiting_emits_exactly_one_actionable_event():
    """The event that did not exist on 2026-08-13. An agent already live, already known,
    stops mid-task — that must become a durable owner-actionable record."""
    em = _Emitter()
    r = wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
                   project="payorch", conversation_id="conv-a", progress="turn 12",
                   emit_fn=em)
    assert r["emitted"] is True
    assert len(em.calls) == 1
    call = em.calls[0]
    assert call["type"] == wt.EVENT_TYPE == "agent_waiting_input"
    assert call["severity"] == "high" and call["owner_action_required"] is True
    assert call["agent_id"] == AGENT


def test_steady_waiting_never_re_emits():
    """Waiting is a LEVEL that stays true every tick. Emitting on level is a poke loop."""
    em = _Emitter()
    first = wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
                       conversation_id="conv-a", progress="turn 12", emit_fn=em)
    assert first["emitted"] is True

    # the same block, observed again and again — including a re-entry that looks like an edge
    for _ in range(5):
        again = wt.observe(target=AGENT, prev_state="waiting_input", cur_state="waiting_input",
                           conversation_id="conv-a", progress="turn 12", emit_fn=em)
        assert again["emitted"] is False
    repeat_edge = wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
                             conversation_id="conv-a", progress="turn 12", emit_fn=em)
    assert repeat_edge["emitted"] is False
    assert repeat_edge["reason"] == "unchanged_waiting_fingerprint"
    assert len(em.calls) == 1


def test_new_progress_then_waiting_emits_a_second_event():
    """Answered, worked, stopped again — genuinely a new thing to answer, so a new event."""
    em = _Emitter()
    wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
               conversation_id="conv-a", progress="turn 12", emit_fn=em)
    # the owner answers; the agent works; it stops on something new
    wt.observe(target=AGENT, prev_state="waiting_input", cur_state="working",
               conversation_id="conv-a", progress="turn 13", emit_fn=em)
    second = wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
                        conversation_id="conv-a", progress="turn 14", emit_fn=em)
    assert second["emitted"] is True
    assert second["fingerprint"] != em.calls[0]["payload"]["fingerprint"]
    assert len(em.calls) == 2


def test_a_first_sighting_is_not_a_transition():
    """After a restart every agent looks new. Treating that as an edge pokes the chat once
    per waiting agent — a restart storm, not a stall."""
    em = _Emitter()
    r = wt.observe(target=AGENT, prev_state="", cur_state="waiting_input", emit_fn=em)
    assert r["emitted"] is False and em.calls == []


def test_the_fingerprint_is_durable_across_a_restart():
    """The anti-spam state lives in the DB, so a process that dies mid-wait does not re-announce."""
    em = _Emitter()
    wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
               conversation_id="conv-a", progress="turn 12", emit_fn=em)
    stored = wt.last_seen(AGENT)
    assert stored["fingerprint"] == wt.fingerprint(AGENT, "conv-a", "turn 12")
    assert stored["emissions"] == 1

    # a fresh caller with no memory of its own observes the same block
    again = wt.observe(target=AGENT, prev_state="working", cur_state="waiting_input",
                       conversation_id="conv-a", progress="turn 12", emit_fn=_Emitter())
    assert again["emitted"] is False


# ── selection: fresh actionable outranks a multi-day backlog ────────────────
def test_a_fresh_actionable_wake_outranks_a_multi_day_backlog():
    """Event 3746 was from Aug 11 and was delivered at 04:09:57 on Aug 13, ahead of the
    stall, purely because selection was oldest-first."""
    old = 3746.0                                   # two days earlier
    for eid in (3746, 3801, 3899):
        assert _wake(eid, severity="high", now=old)["wake"] is True
        old += wb.COOLDOWN_SECS + 1

    fresh = _wake(3921, event_type="agent_waiting_input", severity="high", now=old + 10)
    assert fresh["wake"] is True and fresh["actionable"] is True

    p = wb.pending_wake(now=old + 10)
    assert p["pending"] is True
    assert p["event_id"] == 3921, "the blocked pane must be offered before two-day-old history"
    assert p["actionable"] is True


def test_the_generic_cooldown_cannot_suppress_a_new_actionable_transition():
    """Defect A and B compounding: a generic wake consumed the 900s slot, so the stall that
    followed 10 seconds later was refused as `cooldown_active`."""
    assert _wake(3746, severity="high", now=1000.0)["wake"] is True

    blocked = wb.should_wake(event_id=3921, severity="high", now=1010.0)
    assert blocked["wake"] is False and blocked["reason"] == "cooldown_active"

    actionable = wb.should_wake(event_id=3921, severity="high",
                                event_type="agent_waiting_input", now=1010.0)
    assert actionable["wake"] is True
    assert actionable["reason"] == "actionable_waiting_transition"


def test_actionable_wakes_still_have_a_floor_of_their_own():
    """Bypassing the generic floor is not the same as having no floor — a flapping agent
    must not become a burst of pokes."""
    assert _wake(4001, event_type="agent_waiting_input", severity="high", now=1000.0)["wake"] is True
    soon = _wake(4002, event_type="agent_waiting_input", severity="high", now=1005.0)
    assert soon["wake"] is False and soon["reason"] == "actionable_cooldown_active"
    later = _wake(4003, event_type="agent_waiting_input", severity="high",
                  now=1000.0 + wb.ACTIONABLE_COOLDOWN_SECS + 1)
    assert later["wake"] is True


def test_the_same_actionable_event_still_never_wakes_twice():
    """The cooldown bypass must never become a dedupe bypass."""
    assert _wake(4100, event_type="agent_waiting_input", severity="high", now=1000.0)["wake"] is True
    again = _wake(4100, event_type="agent_waiting_input", severity="high", now=99_000.0)
    assert again["wake"] is False and again["reason"] == "already_woke_for_this_event"


# ── coalescing the generic backlog, with an audit ──────────────────────────
def test_stale_generic_wakes_coalesce_into_the_newest_with_a_durable_audit():
    """The phrase carries no event content — N queued generic wakes are N copies of one
    instruction. They collapse; they are never silently dropped."""
    now = 1000.0
    for eid in (3746, 3801, 3899):
        assert _wake(eid, severity="high", now=now)["wake"] is True
        now += wb.COOLDOWN_SECS + 1

    res = wb.coalesce_generic_backlog()
    assert res["superseded"] == 2
    assert sorted(res["superseded_event_ids"]) == [3746, 3801]
    assert res["kept_event_id"] == 3899

    p = wb.pending_wake(now=now)
    assert p["event_id"] == 3899, "one generic wake means: read every current Owner OS event"

    hist = wb.coalesce_history()
    assert {h["event_id"] for h in hist} == {3746, 3801}
    assert all(h["superseded_by_event_id"] == 3899 for h in hist)
    assert all(h["reason"] and h["at"] for h in hist)


def test_coalescing_never_touches_actionable_wakes():
    """Each blocked pane is a distinct thing to answer, not a duplicate instruction."""
    assert _wake(5001, event_type="agent_waiting_input", severity="high", now=1000.0)["wake"] is True
    assert _wake(5002, event_type="agent_waiting_input", severity="high",
                 now=1000.0 + wb.ACTIONABLE_COOLDOWN_SECS + 1)["wake"] is True
    res = wb.coalesce_generic_backlog()
    assert res["superseded"] == 0
    # oldest actionable first, none retired
    p = wb.pending_wake(now=1000.0 + wb.ACTIONABLE_COOLDOWN_SECS + 1)
    assert p["event_id"] == 5001


# ── the pre-existing idempotency guarantees still hold ─────────────────────
def test_the_submission_latch_still_prevents_a_duplicate_actionable_send():
    """df24ecf: a fired phrase is never offered again, however the verification landed."""
    assert _wake(6001, event_type="agent_waiting_input", severity="high", now=1000.0)["wake"] is True
    assert wb.pending_wake(now=1000.0)["event_id"] == 6001
    wb.mark_submitted(6001, source="companion")
    assert wb.pending_wake(now=1000.0)["pending"] is False


def test_an_actionable_claim_is_not_refused_by_the_generic_send_cooldown():
    """Deciding to wake and then refusing the send would move the stall, not fix it."""
    assert wb.claim_send("companion", event_id=3746, now=1000.0)["allowed"] is True
    generic = wb.claim_send("companion", event_id=3921, now=1010.0)
    assert generic["allowed"] is False and generic["reason"].startswith("global_cooldown_active")

    act = wb.claim_send("companion", event_id=3921, actionable=True, now=1010.0)
    assert act["allowed"] is True and act["reason"] == "claimed_actionable"

    # ...and the actionable path keeps a choke point of its own
    burst = wb.claim_send("companion", event_id=3922, actionable=True, now=1015.0)
    assert burst["allowed"] is False
    assert burst["reason"].startswith("actionable_cooldown_active")


# ── 2026-08-30: managed-agent lifecycle terminals belong in the FAST lane ────
# Event 15448 (`work_stopped_incomplete`, mess-opus:0.0) was detected immediately by the
# quiescence rule and then faced up to COOLDOWN_SECS before its project chat could be
# woken. A stopped managed agent is a project standing still, not history to be read
# whenever. Detection was never the problem; the lane was.

def test_lifecycle_terminals_take_the_fast_lane():
    from core import wake_bridge as wb
    for t in ("work_stopped_incomplete", "task_completed",
              "agent_process_failed", "agent_dead"):
        assert wb.is_lifecycle(t), t
        assert wb.is_actionable(t), f"{t} must not wait out the generic floor"


def test_the_waiting_input_fast_path_is_unchanged():
    from core import wake_bridge as wb
    for t in ("agent_waiting_input", "agent_needs_response",
              "agent_prompt_needs_response", "owner_decision_required",
              "agent_crash_loop", "wake_loop_no_progress", "wake_loop_stalled"):
        assert wb.is_actionable(t), t
        assert not wb.is_lifecycle(t), f"{t} is a waiting transition, not a lifecycle stop"


def test_notification_noise_is_NOT_made_fast():
    """The narrowness is the point. Channel-health chatter arrives constantly; putting it
    in the fast lane would be noise, not latency."""
    from core import wake_bridge as wb
    for t in ("notification_dead_letter", "notifications_red",
              "notification_channel_down"):
        assert not wb.is_lifecycle(t), t
        assert not wb.is_actionable(t), f"{t} must stay in the generic lane"


def test_routine_chatter_stays_out_of_the_fast_lane():
    from core import wake_bridge as wb
    for t in ("agent_recovered", "work_report_published", "agent_state",
              "runtime_job_state", "agent_control_plane_recovered"):
        assert not wb.is_actionable(t), t


def test_lifecycle_is_audited_under_its_own_reason():
    """The audit should say WHICH authority spoke: "the agent stopped" is not "the agent
    is waiting for an answer", even though both take the same lane."""
    from core import wake_bridge as wb
    assert wb.is_significant(event_type="work_stopped_incomplete",
                             severity="high")["reason"] == "lifecycle_terminal_transition"
    assert wb.is_significant(event_type="agent_waiting_input",
                             severity="high")["reason"] == "actionable_waiting_transition"
    for t in ("work_stopped_incomplete", "task_completed", "agent_dead"):
        assert wb.is_significant(event_type=t, severity="high")["actionable"] is True


def test_a_lifecycle_stop_is_not_blocked_by_a_stale_generic_backlog(tmp_path, monkeypatch):
    """The acceptance case, at the decision gate: a generic wake seconds ago must not make
    a managed-agent stop wait out COOLDOWN_SECS."""
    from core import wake_bridge as wb
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import wake_routes as wr
    conn = wb._conn()[0]
    # Without a real binding the target resolves to the FALLBACK route, the route-scoped
    # floor below never matches the inserted row, and the test would pass vacuously.
    wr.bind_route("mess", "https://chatgpt.com/c/6a92e516-a50c-83eb-a1af-1bb4634f4845",
                  by="test", conn=conn)
    now = 10_000.0
    conn.execute(
        "INSERT INTO wake_audit (ts,at,event_id,decision,reason,event_type,actionable,"
        "project_id,agent_id,route_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now - 5, "t", 900, "wake", "urgent_event_not_yet_signalled",
         "notifications_red", 0, "mess", "", "mess"))
    conn.commit()
    d = wb.should_wake(event_id=901, event_type="work_stopped_incomplete",
                       severity="high", owner_action_required=False,
                       project_id="mess", agent_id="mess-opus:0.0",
                       conn=conn, now=now)
    assert d["wake"] is True, d
    assert d["actionable"] is True


def test_exactly_once_still_wins_over_the_fast_lane(tmp_path, monkeypatch):
    """Being fast must never become being duplicated: the per-event dedupe is checked
    before any lane logic and is unchanged."""
    from core import wake_bridge as wb
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    conn = wb._conn()[0]
    now = 10_000.0
    conn.execute(
        "INSERT INTO wake_audit (ts,at,event_id,decision,reason,event_type,actionable,"
        "project_id,agent_id,route_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now - 5, "t", 902, "wake", "lifecycle_terminal_transition",
         "work_stopped_incomplete", 1, "mess", "mess-opus:0.0", "mess"))
    conn.commit()
    d = wb.should_wake(event_id=902, event_type="work_stopped_incomplete",
                       severity="high", owner_action_required=False,
                       project_id="mess", agent_id="mess-opus:0.0",
                       conn=conn, now=now)
    assert d["wake"] is False and d["reason"] == "already_woke_for_this_event"


def test_the_fast_lane_still_has_its_own_bounded_floor(tmp_path, monkeypatch):
    """Fast is not unbounded: two distinct lifecycle stops on one route cannot burst."""
    from core import wake_bridge as wb
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import wake_routes as wr
    conn = wb._conn()[0]
    wr.bind_route("mess", "https://chatgpt.com/c/6a92e516-a50c-83eb-a1af-1bb4634f4845",
                  by="test", conn=conn)
    now = 10_000.0
    conn.execute(
        "INSERT INTO wake_audit (ts,at,event_id,decision,reason,event_type,actionable,"
        "project_id,agent_id,route_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now - 1, "t", 903, "wake", "lifecycle_terminal_transition",
         "task_completed", 1, "mess", "other:0.0", "mess"))
    conn.commit()
    d = wb.should_wake(event_id=904, event_type="agent_dead", severity="high",
                       owner_action_required=False, project_id="mess",
                       agent_id="mess-opus:0.0", conn=conn, now=now)
    assert d["wake"] is False and d["reason"] == "actionable_cooldown_active"
    assert d["wait_secs"] <= wb.ACTIONABLE_COOLDOWN_SECS
