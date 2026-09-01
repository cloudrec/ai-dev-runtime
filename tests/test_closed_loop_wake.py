"""Closed-loop wake (task 211): agents wake ChatGPT themselves through Owner OS, with
no owner typing. Exercises the five semantic trigger classes end-to-end — real
`cto.emit` -> `wake_bridge` decision/dedupe/route — plus the SLO watchdog and the
owner_intervention metric, and pins the exact live incident these were built from: the
2026-08-15 three-terminal moment (gaika-video lost continuation, payorch internal gate
wait, jobhunter child workflow wait, all three live at once).
"""
from __future__ import annotations

import sqlite3

import pytest

from core import agent_watch as aw
from core import stall_doctor as sd
from core import wake_bridge as wb
from core import wake_routes as wr
from core import closed_loop_wake as clw
from core.control_plane import cto


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


NOW = 1_700_000_000.0

GAIKA_PENDING = ("Proceed with the opening fix exactly as instructed. "
                 "Then verify UA and EN public links.")
GAIKA_TAIL = """\
  All link checks documented.
✻ Sautéed for 1m 5s
───────────────────────────
❯ Proceed with the opening fix exactly as instructed…
───────────────────────────
  ⏵⏵ auto mode on
"""
PAYORCH_TAIL = """\
  Stage B enforcement finished locally.
  Waiting for gate results before pushing.
───────────────────────────
❯
───────────────────────────
"""
PAYORCH_OWNER_POWER_TAIL = """\
  Stage B enforcement finished locally.
  Waiting for gate results — owner approval needed before deploy.
───────────────────────────
❯
───────────────────────────
"""


def jobhunter_tail(done=32):
    return f"""\
✻ Waiting for 1 dynamic workflow to finish
  4 tasks (0 done, 4 open)
───────────────────────────
❯ continue autonomously when the audit finishes
───────────────────────────
  ◯ fable-second-pass-audit {done}/71 agents
"""


TOOL_COMPLETION_TAIL = """\
  Running the release checklist.
  Background command "monitor output" completed (exit code 0)
"""

COMPLETION_TAIL = """\
  All link checks documented and published.
  All done. Final report written to reports/AUDIT.md.
"""

PROMPT_TAIL = """╭───────────────────────────────╮
 Bash command: rm -rf build/
 Do you want to proceed?
 ❯ 1. Yes
   2. No
╰───────────────────────────────╯"""

BLOCKER_TAIL = """Migration analysis finished.
Development paused at safe checkpoint.
Waiting for migration instructions from the owner."""

CRASH_TAIL = "Traceback (most recent call last):\n  segmentation fault"


def _doctor_agent(target, cwd, state="idle"):
    return {"target": target, "cwd": cwd, "claude_cwd": cwd, "state": state,
            "alive": True, "is_agent": True}


def _watch_agent(target, cwd, alive=True, state="waiting_input"):
    return {"target": target, "cwd": cwd, "claude_cwd": cwd,
            "alive": alive, "is_agent": True, "state": state}


class Deliver:
    """Stall doctor's `deliver_fn` — never touches a real pane."""
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, target, cwd, *, action, text):
        self.calls.append({"target": target, "action": action, "text": text})
        return {"ok": self.ok}


@pytest.fixture()
def conn():
    from core.control_plane.store import connect, init_db
    c = connect()
    init_db(c)
    yield c
    c.close()


def _bind(route_key, url):
    return wr.bind_route(route_key, url)


# ── (a) the three-terminal incident: right subset acted, no cross-talk ──────
def test_three_terminal_incident_no_cross_talk(conn):
    """gaika-video (lost continuation), payorch (plain internal gate wait), jobhunter
    (child workflow, PROGRESSING) all at once. The doctor must submit gaika's own
    queued line, nudge payorch locally, and stay silent for jobhunter — and NONE of
    that is itself a ChatGPT wake: these are exactly the cases stall_doctor already
    resolves on its own."""
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    _bind("payment-orchestrator", "https://chatgpt.com/c/payorch-chat")
    _bind("jobhunter-ai", "https://chatgpt.com/c/jobhunter-chat")

    agents = [
        _doctor_agent("gaika-video:0.0", "/opt/gaika-drop", state="waiting_input"),
        _doctor_agent("payorch-live:0.0", "/opt/payment-orchestrator", state="idle"),
        _doctor_agent("jobhunter-audit:0.0", "/opt/jobhunter-ai", state="waiting_input"),
    ]
    tails = {"gaika-video:0.0": GAIKA_TAIL, "payorch-live:0.0": PAYORCH_TAIL,
            "jobhunter-audit:0.0": jobhunter_tail(done=10)}
    pend = {"gaika-video:0.0": GAIKA_PENDING,
           "jobhunter-audit:0.0": "continue autonomously when the audit finishes"}
    dlv = Deliver()

    def _scan(now):
        return sd.scan(agents=agents, read_fn=lambda t: tails[t],
                       pending_fn=lambda t, tail, cwd: pend.get(t, ""),
                       deliver_fn=dlv, emit_fn=cto.emit, conn=conn, now=now)

    r1 = _scan(NOW)
    assert not r1["acted"], "every shape opens quiet, within its own SLO"

    # jobhunter's child progresses (10 -> 45): its digest changes every scan, so it can
    # never age past its SLO — the exact "progressing -> silent" contract.
    tails["jobhunter-audit:0.0"] = jobhunter_tail(done=45)
    r2 = _scan(NOW + sd.QUEUED_SLO_SECS + 1)
    acted = {a["target"]: a["action"] for a in r2["acted"]}
    assert acted["gaika-video:0.0"] == "submit_queued"
    assert acted.get("payorch-live:0.0") is None, "payorch not yet past its own SLO"
    assert "jobhunter-audit:0.0" not in acted, "progressing child stays silent"

    r3 = _scan(NOW + sd.WAIT_SLO_SECS + 1)
    acted3 = {a["target"]: a["action"] for a in r3["acted"]}
    assert acted3.get("payorch-live:0.0") == "nudge"

    # None of gaika's submit or payorch's nudge is a ChatGPT wake — both are audited as
    # `stall_doctor_action`, which is a routine (non-waking) event type.
    assert wb.health()["wakes_total"] == 0, "no cross-talk: nothing here wakes ChatGPT"


# ── (b) lost continuation: doctor acts, no wake ──────────────────────────────
def test_lost_continuation_no_wake_needed(conn):
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    agents = [_doctor_agent("gaika-video:0.0", "/opt/gaika-drop", state="waiting_input")]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": GAIKA_PENDING}
    dlv = Deliver()
    sd.scan(agents=agents, read_fn=lambda t: tails[t],
           pending_fn=lambda t, tail, cwd: pend[t], deliver_fn=dlv, emit_fn=cto.emit,
           conn=conn, now=NOW)
    r = sd.scan(agents=agents, read_fn=lambda t: tails[t],
               pending_fn=lambda t, tail, cwd: pend[t], deliver_fn=dlv, emit_fn=cto.emit,
               conn=conn, now=NOW + sd.QUEUED_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["submit_queued"]
    assert wb.health()["wakes_total"] == 0


# ── (c) queued-messages stall: doctor submits ────────────────────────────────
def test_queued_line_at_rest_gets_submitted(conn):
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    agents = [_doctor_agent("gaika-video:0.0", "/opt/gaika-drop", state="waiting_input")]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": GAIKA_PENDING}
    dlv = Deliver()
    sd.scan(agents=agents, read_fn=lambda t: tails[t],
           pending_fn=lambda t, tail, cwd: pend[t], deliver_fn=dlv, emit_fn=cto.emit,
           conn=conn, now=NOW)
    sd.scan(agents=agents, read_fn=lambda t: tails[t],
           pending_fn=lambda t, tail, cwd: pend[t], deliver_fn=dlv, emit_fn=cto.emit,
           conn=conn, now=NOW + sd.QUEUED_SLO_SECS + 1)
    assert dlv.calls[0]["action"] == "submit" and dlv.calls[0]["text"] == GAIKA_PENDING


# ── (d) child workflow wait: progressing silent, static -> nudge, terminal -> wake ──
def test_child_workflow_wait_full_lifecycle(conn):
    _bind("jobhunter-ai", "https://chatgpt.com/c/jobhunter-chat")
    agents = [_doctor_agent("jobhunter-audit:0.0", "/opt/jobhunter-ai", state="waiting_input")]
    pend = {"jobhunter-audit:0.0": "continue autonomously when the audit finishes"}
    dlv = Deliver()
    tail = {"t": jobhunter_tail(done=10)}

    def _scan(now):
        return sd.scan(agents=agents, read_fn=lambda t: tail["t"],
                       pending_fn=lambda t, tl, cwd: pend[t], deliver_fn=dlv,
                       emit_fn=cto.emit, conn=conn, now=now)

    _scan(NOW)
    # progressing: the counter moves every poll, so it never ages past its SLO
    tail["t"] = jobhunter_tail(done=20)
    r2 = _scan(NOW + sd.CHILD_SLO_SECS + 1)
    assert not r2["acted"], "a moving child counter resets the episode clock"

    # static now: no more movement, past its own SLO -> a safe local nudge
    r3 = _scan(NOW + sd.CHILD_SLO_SECS + 1 + sd.CHILD_SLO_SECS + 1)
    assert [a["action"] for a in r3["acted"]] == ["nudge"]
    assert wb.health()["wakes_total"] == 0, "a nudge is not a wake"

    # still static past the loop guard -> escalate -> THIS is a real wake
    now = NOW + sd.CHILD_SLO_SECS + 1 + sd.CHILD_SLO_SECS + 1
    for _ in range(sd.MAX_ACTIONS_PER_EPISODE):
        now += sd.ACTION_COOLDOWN_SECS + 1
        r = _scan(now)
    assert [a["action"] for a in r["acted"]] == ["escalate"]
    h = wb.health()
    assert h["wakes_total"] == 1
    d = wb.last_delivery() if h["wakes_total"] else None  # sanity: no crash on empty path
    p = wb.pending_wake()
    assert p["pending"] is True
    assert p["trigger_class"] == "blocker"


# ── (e) internal wait naming an owner power -> decision wake, not a nudge ───
def test_internal_wait_naming_owner_power_is_a_decision_wake(conn):
    _bind("payment-orchestrator", "https://chatgpt.com/c/payorch-chat")
    agents = [_doctor_agent("payorch-live:0.0", "/opt/payment-orchestrator", state="idle")]
    tails = {"payorch-live:0.0": PAYORCH_OWNER_POWER_TAIL}
    dlv = Deliver()
    sd.scan(agents=agents, read_fn=lambda t: tails[t],
           pending_fn=lambda t, tl, cwd: "", deliver_fn=dlv, emit_fn=cto.emit,
           conn=conn, now=NOW)
    r = sd.scan(agents=agents, read_fn=lambda t: tails[t],
               pending_fn=lambda t, tl, cwd: "", deliver_fn=dlv, emit_fn=cto.emit,
               conn=conn, now=NOW + sd.WAIT_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["escalate"]
    assert not dlv.calls, "never a nudge for a real owner-power wait"

    p = wb.pending_wake()
    assert p["pending"] is True
    assert p["trigger_class"] == "owner_decision"
    assert p["route_key"] == "payment-orchestrator"
    assert p["conversation"] == "https://chatgpt.com/c/payorch-chat"
    assert f"event={p['event_id']}" in p["phrase"]
    assert "trigger=owner_decision" in p["phrase"]
    assert "type=owner_decision_required" in p["phrase"]
    assert "project=payment-orchestrator" in p["phrase"]
    assert "payorch-live:0.0" in p["phrase"]
    assert p["phrase"].endswith(wb.WAKE_PHRASE)

    # dedupe: escalating again on the SAME episode must not double-wake
    r2 = sd.scan(agents=agents, read_fn=lambda t: tails[t],
                pending_fn=lambda t, tl, cwd: "", deliver_fn=dlv, emit_fn=cto.emit,
                conn=conn, now=NOW + sd.WAIT_SLO_SECS + 30)
    assert not r2["acted"], "already_escalated — no second event, no second wake"
    assert wb.health()["wakes_total"] == 1


# ── (f) genuine completion -> completion wake, correct project route ────────
def test_genuine_completion_wakes_the_correct_project_chat(conn):
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    agents = [_watch_agent("gaika-video:0.0", "/opt/gaika-drop")]
    # first sight establishes "working", second sight is the stated finish at rest
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW)
    r = aw.scan(agents=agents, read_fn=lambda t: COMPLETION_TAIL, emit_fn=cto.emit,
               conn=conn, now=NOW + 5)
    assert [e["class"] for e in r["emitted"]] == ["completed"]

    p = wb.pending_wake()
    assert p["pending"] is True
    assert p["conversation"] == "https://chatgpt.com/c/gaika-chat", \
        "the completion wake must land in gaika's OWN chat, never owner-os"
    assert p["trigger_class"] == "completion"
    assert "trigger=completion" in p["phrase"]
    assert "type=task_completed" in p["phrase"]
    assert "project=gaika-drop" in p["phrase"]
    assert p["phrase"].endswith(wb.WAKE_PHRASE)


# ── (g) crash wakes; recovered crash retires, no stale wake ──────────────────
def test_crash_wakes_and_recovery_retires_without_a_stale_wake(conn):
    _bind("payment-orchestrator", "https://chatgpt.com/c/payorch-chat")
    agents = [_watch_agent("payorch-live:0.0", "/opt/payment-orchestrator")]
    r = aw.scan(agents=agents, read_fn=lambda t: CRASH_TAIL, emit_fn=cto.emit,
               conn=conn, now=NOW)
    assert [e["class"] for e in r["emitted"]] == ["crashed"]
    p = wb.pending_wake()
    assert p["pending"] is True
    eid = p["event_id"]
    assert p["trigger_class"] == "failure"

    # the pane is observed alive and working again before anyone answered the crash wake
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW + 10)
    alerts = aw.recent_alerts(include_invalid=True)
    assert any(a["event_id"] == eid and a.get("invalid") for a in alerts), \
        "the crash alert must be retired, not left standing as current truth"
    assert wb.pending_wake()["pending"] is False, \
        "the recovered crash's wake must not still be offered"


def test_repeated_crashes_are_a_distinct_failure_trigger_class(conn):
    """task 211 'repeated failure': N crashes for the same agent inside the rolling
    window emit a DISTINCT agent_crash_loop event, on top of the ordinary per-crash
    alert. `_check_crash_loop` counts genuinely durable `agent_process_failed` rows —
    exercised directly here with three distinct incidents (a real scan()-driven
    three-crash sequence would collide on agent_watch's own `crashed` digest, which is
    always the fixed value "gone" by design and is not what this unit is about)."""
    target = "payorch-live:0.0"
    for i, text in enumerate(("segmentation fault", "Killed", "core dumped")):
        cto.emit("agent_watch", "agent_process_failed", project_id="payment-orchestrator",
                agent_id=target, severity="critical", owner_action_required=True,
                payload={"excerpt": text}, action_taken=text,
                correlation_id=f"agentwatch:{target}",
                dedup_key=f"agentwatch:{target}:crashed:incident-{i}",
                dedup_window_secs=86400, conn=conn)

    class Emit:
        def __init__(self):
            self.calls = []

        def __call__(self, source, etype, **kw):
            self.calls.append({"source": source, "type": etype, **kw})
            return {"event_id": 999}

    emit = Emit()
    eid = aw._check_crash_loop(conn, emit, project="payment-orchestrator",
                               target=target, now=NOW)
    assert eid == 999
    assert emit.calls[0]["type"] == "agent_crash_loop"
    assert emit.calls[0]["severity"] == "critical"
    assert emit.calls[0]["owner_action_required"] is True
    assert wb.trigger_class_for("agent_crash_loop") == "failure"

    # below threshold: no emission
    emit2 = Emit()
    assert aw._check_crash_loop(conn, emit2, project="x", target="other:0.0",
                                now=NOW) is None
    assert emit2.calls == []


# ── (h) false shell-completion tool banner never wakes ───────────────────────
def test_false_shell_completion_never_wakes(conn):
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    agents = [_watch_agent("gaika-video:0.0", "/opt/gaika-drop")]
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW)
    r = aw.scan(agents=agents, read_fn=lambda t: TOOL_COMPLETION_TAIL, emit_fn=cto.emit,
               conn=conn, now=NOW + 5)
    assert r["emitted"] == [], "a shell exit code banner is not a task finish"
    assert wb.health()["wakes_total"] == 0


# ── owner_intervention metric ─────────────────────────────────────────────────
def test_owner_intervention_recorded_when_pane_resumes_without_a_delivered_wake(conn):
    agents = [_watch_agent("ip-seal:0.0", "/opt/clients-help-landing", state="waiting_owner")]
    r = aw.scan(agents=agents, read_fn=lambda t: PROMPT_TAIL, emit_fn=cto.emit,
               conn=conn, now=NOW)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]
    # no route bound at all -> the wake bridge never delivers anything for this event
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW + 30)
    n = conn.execute("SELECT COUNT(*) FROM owner_intervention_log").fetchone()[0]
    assert n == 1
    row = conn.execute(
        "SELECT type FROM event WHERE type='owner_intervention'").fetchone()
    assert row is not None


def test_owner_intervention_not_recorded_when_the_companion_delivered(conn):
    _bind("clients-help-landing", "https://chatgpt.com/c/landing-chat")
    agents = [_watch_agent("ip-seal:0.0", "/opt/clients-help-landing", state="waiting_owner")]
    aw.scan(agents=agents, read_fn=lambda t: PROMPT_TAIL, emit_fn=cto.emit,
           conn=conn, now=NOW)
    p = wb.pending_wake()
    assert p["pending"] is True
    # simulate a REAL companion delivery: mark it delivered and acknowledge it
    wb.record_delivery("companion", event_id=p["event_id"], delivered=True,
                       reason="submitted_and_user_turn_id_advanced",
                       conversation=p["conversation"], route_key=p["route_key"], conn=conn)
    wb.acknowledge(p["event_id"], conn=conn)
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW + 30)
    n = conn.execute("SELECT COUNT(*) FROM owner_intervention_log").fetchone()[0]
    assert n == 0, "a companion-submitted turn must never be misclassified as an intervention"


# ── SLO watchdog: re-wake once, then escalate on continued silence ──────────
def test_slo_watchdog_rewakes_once_then_escalates(conn):
    class Emit:
        def __init__(self):
            self.calls = []

        def __call__(self, source, etype, **kw):
            self.calls.append({"source": source, "type": etype, **kw})
            return {"event_id": 9000 + len(self.calls)}

    emit = Emit()
    clw.register_delivery(event_id=42, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    r0 = clw.slo_scan(conn=conn, now=NOW + 1, emit_fn=emit)
    assert not r0["rewoken"] and not r0["escalated"], "within the SLO -> quiet"

    r1 = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r1["rewoken"]] == [42]
    assert emit.calls[-1]["type"] == "wake_loop_no_progress"
    assert emit.calls[-1]["agent_id"] == "mess:0.0"

    r_quiet = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 30, emit_fn=emit)
    assert not r_quiet["escalated"], "not yet a full SLO window past the re-wake"

    r2 = clw.slo_scan(conn=conn, now=NOW + 2 * clw.WAKE_LOOP_SLO_SECS + 60, emit_fn=emit)
    assert [e["event_id"] for e in r2["escalated"]] == [42]
    assert emit.calls[-1]["type"] == "wake_loop_stalled"

    r3 = clw.slo_scan(conn=conn, now=NOW + 3 * clw.WAKE_LOOP_SLO_SECS + 90, emit_fn=emit)
    assert not r3["rewoken"] and not r3["escalated"], "terminal — no repeat escalation"


def test_slo_watchdog_stays_quiet_when_progress_is_observed(conn):
    emit_calls = []

    def emit(source, etype, **kw):
        emit_calls.append(etype)
        return {"event_id": 1}

    clw.register_delivery(event_id=55, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    # something happens for this agent after delivery — real progress
    cto.emit("agent_watch", "agent_state", agent_id="mess:0.0", project_id="mess",
             conn=conn)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"], "observed progress suppresses the re-wake"
    assert emit_calls == []


# ── observability counters ────────────────────────────────────────────────────
def test_diagnostics_closed_loop_wake_counters_are_additive(conn):
    from core.control_plane import diagnostics
    _bind("gaika-drop", "https://chatgpt.com/c/gaika-chat")
    agents = [_watch_agent("gaika-video:0.0", "/opt/gaika-drop")]
    aw.scan(agents=agents, read_fn=lambda t: "✻ Compacting… (esc to interrupt)",
           emit_fn=cto.emit, conn=conn, now=NOW)
    aw.scan(agents=agents, read_fn=lambda t: COMPLETION_TAIL, emit_fn=cto.emit,
           conn=conn, now=NOW + 5)
    p = wb.pending_wake(conn=conn)
    wb.record_delivery("companion", event_id=p["event_id"], delivered=True,
                       reason="submitted_and_user_turn_appeared",
                       conversation=p["conversation"], route_key=p["route_key"], conn=conn)
    c = diagnostics.closed_loop_wake_report(conn=conn)
    assert c["wakes_delivered_by_trigger_class"].get("completion") == 1
    assert c["wakes_delivered_total"] == 1
    assert "owner_intervention_count" in c and "loop_slo_rewoken" in c

    summary = diagnostics.observability_summary()
    assert "closed_loop_wake" in summary


# ── watchdog resolution-blindness / self-feeding chain (2026-08-15) ─────────
# Live incident: event 5548 (owner_prompt) was delivered, re-woken as 5563, and that
# rewake was ITSELF registered as a new watch — which re-woke AGAIN as 5595, an
# unbounded chain rate-limited only by the SLO window. Separately, runtime-job watches
# (5576, then again 5584) were re-woken (5597, then 5599) even though the underlying
# job had already gone terminal and the original wake had already been retired —
# because nothing ever re-checked whether the condition a watch exists for was still
# true. Both classes of fix live in `register_delivery` (never watch our own
# watchdog output) and `deregister_resolved` (silently retire a watch whose condition
# already resolved, checked proactively every scan, not just as a rewake gate).
class _Emit:
    def __init__(self):
        self.calls = []
        self._n = 9000

    def __call__(self, source, etype, **kw):
        self._n += 1
        self.calls.append({"source": source, "type": etype, **kw})
        return {"event_id": self._n}


def test_register_delivery_never_watches_loop_watchdog_events(conn):
    """(a) A `wake_loop_no_progress`/`wake_loop_stalled` delivery must never itself
    become a new watch row — that IS the self-feeding chain."""
    clw.register_delivery(event_id=100, target="x:0.0", project_id="mess",
                          event_type="wake_loop_no_progress", conn=conn, now=NOW)
    clw.register_delivery(event_id=101, target="x:0.0", project_id="mess",
                          event_type="wake_loop_stalled", conn=conn, now=NOW)
    n = conn.execute("SELECT COUNT(*) FROM wake_loop_watch").fetchone()[0]
    assert n == 0
    # an ordinary trigger class still registers normally
    clw.register_delivery(event_id=102, target="x:0.0", project_id="mess",
                          event_type="agent_waiting_input", conn=conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM wake_loop_watch").fetchone()[0] == 1


def test_rewake_event_does_not_spawn_a_second_generation_rewake(conn):
    """(c) The 5548 -> 5563 -> 5595 chain: a REWAKE event, once delivered, must not
    itself become a new watch. Exercised the way it actually happens live: slo_scan
    emits the rewake, and the companion's own `register_delivery` call for that
    delivery (with the rewake's real event_type) must be a no-op."""
    emit = _Emit()
    clw.register_delivery(event_id=200, target="capacity-blockchain:0.0",
                          project_id="owner-os", event_type="agent_prompt_needs_response",
                          conn=conn, now=NOW)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    rewake_event_id = r["rewoken"][0]["rewoken_event_id"]
    assert emit.calls[-1]["type"] == "wake_loop_no_progress"

    # the companion "delivers" the rewake and calls register_delivery exactly as
    # tools/wake_companion.py does, carrying the rewake's OWN event_type
    clw.register_delivery(event_id=rewake_event_id, target="capacity-blockchain:0.0",
                          project_id="owner-os", event_type="wake_loop_no_progress",
                          conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 5)
    row = conn.execute("SELECT 1 FROM wake_loop_watch WHERE event_id=?",
                       (rewake_event_id,)).fetchone()
    assert row is None, "a rewake event must never become a second watch"

    # only the ONE original watch exists, and it can still escalate on its own clock —
    # the chain is broken, not the terminal escalation path
    r2 = clw.slo_scan(conn=conn, now=NOW + 2 * clw.WAKE_LOOP_SLO_SECS + 60, emit_fn=emit)
    assert [e["event_id"] for e in r2["escalated"]] == [200]


def test_5576_5597_runtime_job_terminal_plus_invalid_overlay_deregisters(conn):
    """(b) The exact 5576->5597 shape: a runtimejob watch whose original event was
    marked invalid (agent_watch's audited overlay) AND whose job is terminal. The
    next scan must deregister it SILENTLY — no wake_loop_no_progress, nothing emitted."""
    emit = _Emit()
    clw.register_delivery(event_id=300, target="runtimejob:b34772f4",
                          project_id="owner-os", event_type="owner_decision_required",
                          conn=conn, now=NOW)
    conn.execute("CREATE TABLE IF NOT EXISTS agent_alert_invalid (event_id INTEGER "
                "PRIMARY KEY, at TEXT, ts REAL, by TEXT, reason TEXT)")
    conn.execute("INSERT INTO agent_alert_invalid (event_id, at, ts, by, reason) "
                "VALUES (300, 'now', ?, 'owner_os', 'job terminal, wake retired')",
                (NOW,))
    conn.commit()

    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and not r["escalated"]
    assert [d["event_id"] for d in r["deregistered"]] == [300]
    assert r["deregistered"][0]["reason"] == "event_marked_invalid"
    assert emit.calls == [], "a resolved watch must emit nothing at all"

    row = conn.execute("SELECT resolved, resolved_reason FROM wake_loop_watch "
                       "WHERE event_id=300").fetchone()
    assert row == (1, "event_marked_invalid")

    # stays quiet forever after — never reconsidered once resolved
    r2 = clw.slo_scan(conn=conn, now=NOW + 5 * clw.WAKE_LOOP_SLO_SECS, emit_fn=emit)
    assert not r2["rewoken"] and not r2["escalated"] and not r2["deregistered"]
    assert emit.calls == []


def test_5584_5599_runtime_job_terminal_without_overlay_deregisters(conn, monkeypatch, tmp_path):
    """(b) The SECOND live instance: 5584 -> 5599. This one was never marked invalid —
    only the underlying job went terminal (fallback_plan_only). Resolution must be
    caught by the runtime-job-terminal check alone, with no overlay involved."""
    monkeypatch.setenv("RUNTIME_DB", str(tmp_path / "jobs.db"))
    from core import job_store
    monkeypatch.setattr(job_store, "_DB", str(tmp_path / "jobs.db"))
    job_store.init_db()
    job = job_store.create_job(goal="router smoke test", instructions="x",
                               project_path="/opt/payment-orchestrator")
    job_store.update_job(job["id"], status="fallback_plan_only")

    emit = _Emit()
    target = f"runtimejob:{job['id'][:8]}"
    clw.register_delivery(event_id=301, target=target, project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=NOW)

    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and not r["escalated"]
    assert [d["event_id"] for d in r["deregistered"]] == [301]
    assert r["deregistered"][0]["reason"] == "runtime_job_terminal"
    assert emit.calls == []


def test_runtime_job_still_in_flight_is_not_resolved(conn, monkeypatch, tmp_path):
    """A runtime job that has NOT reached a terminal status must still re-wake
    normally — the fix must not silence every runtimejob watch, only resolved ones.

    Uses REAL wall-clock time (not the fixed `NOW` fixture constant): job_store's
    own lifecycle mirroring (`_emit_transition`) writes a REAL-timestamped CTO event
    for this target as a side effect of `create_job`/`update_job`, and `_progress_since`
    correctly treats that as progress — a synthetic `NOW` far in the past would make
    that real event look like it landed AFTER an equally-synthetic `delivered_ts`,
    which is a clock-mismatch test artifact, not the behavior under test here."""
    from core.control_plane.store import now_ts
    monkeypatch.setenv("RUNTIME_DB", str(tmp_path / "jobs2.db"))
    from core import job_store
    monkeypatch.setattr(job_store, "_DB", str(tmp_path / "jobs2.db"))
    job_store.init_db()
    job = job_store.create_job(goal="still working", instructions="x")
    job_store.update_job(job["id"], status="editing")

    t0 = now_ts()
    emit = _Emit()
    target = f"runtimejob:{job['id'][:8]}"
    clw.register_delivery(event_id=302, target=target, project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=t0)
    r = clw.slo_scan(conn=conn, now=t0 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["deregistered"]
    assert [e["event_id"] for e in r["rewoken"]] == [302]


def test_pane_alive_and_working_deregisters_silently(conn):
    """A tmux-pane watch whose agent is CURRENTLY observed working already moved on
    by itself — no wake needed, nothing to escalate."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_watch_state (target TEXT PRIMARY KEY, "
        "cls TEXT, digest TEXT, at TEXT, ts REAL, notified_cls TEXT, "
        "notified_digest TEXT, notified_at TEXT, notified_ts REAL, "
        "emissions INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO agent_watch_state (target, cls) VALUES (?, 'working')",
                ("chemmy-fast:0.0",))
    conn.commit()

    emit = _Emit()
    clw.register_delivery(event_id=303, target="chemmy-fast:0.0", project_id="mess",
                          event_type="agent_waiting_input", conn=conn, now=NOW)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and not r["escalated"]
    assert r["deregistered"][0]["reason"] == "pane_alive_and_working"
    assert emit.calls == []


def test_deregister_is_proactive_not_only_a_rewake_gate(conn):
    """Resolution is checked on EVERY scan, not only when a rewake/escalate would
    otherwise fire — a watch resolves the instant its condition is proven false, not
    900s later."""
    clw.register_delivery(event_id=304, target="runtimejob:deadbeef", project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=NOW)
    conn.execute("CREATE TABLE IF NOT EXISTS agent_alert_invalid (event_id INTEGER "
                "PRIMARY KEY, at TEXT, ts REAL, by TEXT, reason TEXT)")
    conn.execute("INSERT INTO agent_alert_invalid (event_id, at, ts, by, reason) "
                "VALUES (304, 'now', ?, 'owner_os', 'retired')", (NOW,))
    conn.commit()
    emit = _Emit()
    # well within the SLO window — a rewake would never fire yet either way, but
    # deregistration must happen regardless
    r = clw.slo_scan(conn=conn, now=NOW + 5, emit_fn=emit)
    assert [d["event_id"] for d in r["deregistered"]] == [304]
    assert emit.calls == []


def test_deregister_resolved_is_directly_callable_for_one_time_cleanup(conn):
    """The one-time production cleanup path: `deregister_resolved` on its own, not
    only as a side effect of `slo_scan`."""
    clw.register_delivery(event_id=305, target="runtimejob:cafef00d", project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=NOW)
    conn.execute("CREATE TABLE IF NOT EXISTS agent_alert_invalid (event_id INTEGER "
                "PRIMARY KEY, at TEXT, ts REAL, by TEXT, reason TEXT)")
    conn.execute("INSERT INTO agent_alert_invalid (event_id, at, ts, by, reason) "
                "VALUES (305, 'now', ?, 'owner_os', 'retired')", (NOW,))
    conn.commit()
    out = clw.deregister_resolved(conn=conn, now=NOW)
    assert [d["event_id"] for d in out] == [305]
    row = conn.execute("SELECT resolved FROM wake_loop_watch WHERE event_id=305").fetchone()
    assert row == (1,)


# ── task 221 (events 10268/10284, mess/chemmy-fast): parked wake-loop false
# positives — an agent that finished its authorized scope and is explicitly at
# rest must not keep getting no-progress/stalled wakes for that same unchanged
# state, while every other state keeps its existing wake behavior exactly. ──

def _set_cls(conn, target, cls):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_watch_state (target TEXT PRIMARY KEY, "
        "cls TEXT, digest TEXT, at TEXT, ts REAL, notified_cls TEXT, "
        "notified_digest TEXT, notified_at TEXT, notified_ts REAL, "
        "emissions INTEGER DEFAULT 0)")
    conn.execute("INSERT OR REPLACE INTO agent_watch_state (target, cls) VALUES (?, ?)",
                (target, cls))
    conn.commit()


def test_explicit_parked_idle_suppresses_repeated_loop_wake(conn):
    """cls == 'completed' (agent_watch's stated_finish_at_rest) is DONE, not stuck —
    no wake_loop_no_progress, no wake_loop_stalled, ever, for this unchanged state."""
    _set_cls(conn, "chemmy-fast:0.0", "completed")
    emit = _Emit()
    clw.register_delivery(event_id=310, target="chemmy-fast:0.0", project_id="mess",
                          event_type="agent_waiting_input", conn=conn, now=NOW)
    r1 = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r1["rewoken"] and not r1["escalated"]
    assert r1["deregistered"][0]["reason"] == "agent_parked_completed"
    r2 = clw.slo_scan(conn=conn, now=NOW + 3 * clw.WAKE_LOOP_SLO_SECS, emit_fn=emit)
    assert not r2["rewoken"] and not r2["escalated"] and not r2["deregistered"]
    assert emit.calls == [], "a parked/completed agent must never be woken for this"


def test_waiting_owner_still_wakes_despite_the_parked_suppression(conn):
    """cls == 'owner_prompt' is the opposite of parked — a real waiting-owner state
    must still re-wake and escalate exactly as before."""
    _set_cls(conn, "mess:0.0", "owner_prompt")
    emit = _Emit()
    clw.register_delivery(event_id=311, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    r1 = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r1["rewoken"]] == [311]
    assert emit.calls[-1]["type"] == "wake_loop_no_progress"
    r2 = clw.slo_scan(conn=conn, now=NOW + 2 * clw.WAKE_LOOP_SLO_SECS + 60, emit_fn=emit)
    assert [e["event_id"] for e in r2["escalated"]] == [311]
    assert emit.calls[-1]["type"] == "wake_loop_stalled"


def test_stale_non_parked_state_still_wakes(conn):
    """cls == 'idle' (no_signal — no positive completion evidence) is genuinely
    ambiguous, not an explicit park; it must keep the pre-existing stuck-and-stale
    wake behavior, not be silently swallowed."""
    _set_cls(conn, "gaika-server:0.0", "idle")
    emit = _Emit()
    clw.register_delivery(event_id=312, target="gaika-server:0.0", project_id="gaika-extension",
                          conn=conn, now=NOW)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r["rewoken"]] == [312]
    assert emit.calls[-1]["type"] == "wake_loop_no_progress"


def test_a_new_event_after_parking_still_gets_its_own_wake(conn):
    """A NEW owner-facing event for the same target, after it was parked, must wake
    normally — the suppression only ever silences the OLD, unchanged watch, never a
    fresh one for a state the agent has since moved into."""
    _set_cls(conn, "chemmy-fast:0.0", "completed")
    emit = _Emit()
    clw.register_delivery(event_id=313, target="chemmy-fast:0.0", project_id="mess",
                          event_type="agent_waiting_input", conn=conn, now=NOW)
    r1 = clw.slo_scan(conn=conn, now=NOW + 10, emit_fn=emit)
    assert r1["deregistered"][0]["event_id"] == 313, "the stale parked watch resolves"

    # the agent is handed NEW work and stops being parked
    _set_cls(conn, "chemmy-fast:0.0", "owner_prompt")
    clw.register_delivery(event_id=314, target="chemmy-fast:0.0", project_id="mess",
                          conn=conn, now=NOW + 20)
    r2 = clw.slo_scan(conn=conn, now=NOW + 20 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r2["rewoken"]] == [314]


def test_state_change_away_from_parked_resets_suppression(conn):
    """Resolution is checked fresh on every scan against the CURRENT cls, not
    snapshotted at registration time: an agent parked at delivery time but handed
    new work (owner_prompt) before the SLO window elapses must wake normally — the
    parked state does not stick to a watch once the agent has moved on."""
    _set_cls(conn, "mess:0.0", "completed")
    emit = _Emit()
    clw.register_delivery(event_id=315, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    _set_cls(conn, "mess:0.0", "owner_prompt")
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r["rewoken"]] == [315]
    assert emit.calls[-1]["type"] == "wake_loop_no_progress"


# ── 2026-08-30: an INTENTIONAL external wait is not a stall ──────────────────
# diamond-auction:0.0 finished its stage, armed a read-only monitor for a natural auction
# close and said so — "if a natural auction close occurs, it auto-anchors and I'll be
# notified... Idle on the watch." Nothing was stuck. But `_progress_since` counts NEW
# EVENTS and a correctly-waiting agent emits none, so the watchdog re-woke it and then
# escalated wake_loop_no_progress (15519) at the owner.
#
# The distinction is not readable from the sentence. Claude Code states it structurally:
# a session that stopped with background_tasks running or session_crons armed is waiting
# BY DESIGN, and the native Stop hook records exactly those fields.

def _turn_stopped(conn, target, payload, ts=None):
    """`_resolution_reason` reads the clock itself, so the record must be dated in REAL
    time to be inside its lookback — dating it 1000.0 made the row invisible and the first
    version of these tests failed for that reason, not for a defect in the rule."""
    import json as _j
    import time as _t
    ts = _t.time() if ts is None else ts
    conn.execute(
        "INSERT INTO event (ts,ts_epoch,source,type,agent_id,severity,payload) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t", ts, "claude_hook", "agent_turn_stopped", target, "info", _j.dumps(payload)))
    conn.commit()


def test_an_armed_background_task_marks_the_wait_intentional(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import closed_loop_wake as clw
    from core.control_plane.api import _c
    conn, _ = _c(None)
    t = "diamond-auction:0.0"
    _turn_stopped(conn, t, {"background_tasks": [{"id": "watch1", "status": "running"}]})
    assert clw._armed_external_wait(conn, t, __import__("time").time()) is True
    assert clw._resolution_reason(conn, event_id=1, target=t) == "intentional_external_wait"


def test_an_armed_session_cron_also_marks_it_intentional(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import closed_loop_wake as clw
    from core.control_plane.api import _c
    conn, _ = _c(None)
    t = "diamond-auction:0.0"
    _turn_stopped(conn, t, {"session_crons": [{"schedule": "*/10 * * * *"}]})
    assert clw._resolution_reason(conn, event_id=1, target=t) == "intentional_external_wait"


def test_a_stop_with_NO_monitor_armed_still_escalates(tmp_path, monkeypatch):
    """The whole point of the watchdog is preserved: a genuinely stalled agent, which
    stopped with nothing armed, must still be re-woken and escalated."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import closed_loop_wake as clw
    from core.control_plane.api import _c
    conn, _ = _c(None)
    t = "some-agent:0.0"
    _turn_stopped(conn, t, {"last_assistant_message": "stopping here", "background_tasks": []})
    assert clw._armed_external_wait(conn, t, __import__("time").time()) is False
    assert clw._resolution_reason(conn, event_id=1, target=t) is None


def test_no_structured_record_falls_back_to_the_old_behaviour(tmp_path, monkeypatch):
    """FAIL-SAFE: an older Claude, hooks disabled, or a session started before install
    leaves no record. Unproven must mean NOT resolved — never the reverse."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import closed_loop_wake as clw
    from core.control_plane.api import _c
    conn, _ = _c(None)
    assert clw._armed_external_wait(conn, "never-seen:0.0", __import__("time").time()) is False
    assert clw._resolution_reason(conn, event_id=1, target="never-seen:0.0") is None


def test_a_stale_armed_record_does_not_resolve_forever(tmp_path, monkeypatch):
    """The evidence expires. A monitor armed days ago says nothing about now."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core import closed_loop_wake as clw
    from core.control_plane.api import _c
    conn, _ = _c(None)
    t = "diamond-auction:0.0"
    import time as _t
    base = _t.time() - clw._INTENTIONAL_WAIT_LOOKBACK_SECS - 600
    _turn_stopped(conn, t, {"background_tasks": [{"id": "old"}]}, ts=base)
    now = _t.time()
    assert clw._armed_external_wait(conn, t, now) is False


# ── a wake addressed to a session that is gone can never resolve itself (event 15923) ──
# Hook wakes are addressed `session:<conversation id>`, a namespace absent from
# agent_watch_state, so none of the pane-based resolutions can fire for one. cp-canary was
# killed at 20:20:35Z; its wake was delivered at 20:47:20Z to an agent that no longer
# existed, re-woken at 21:03 and escalated critical at 21:18:49Z with no possible end.

def _hook_wake(conn, event_id=9100, cwd="/root/cp-canary-v2",
               target="session:b2635b20-8de", terminal=True):
    import json as _j
    conn.execute("INSERT INTO event (id,ts,ts_epoch,source,type,agent_id,severity,payload) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (event_id, "t", 1000.0, "claude_hook", "agent_waiting_input", target,
                  "high", _j.dumps({"cwd": cwd, "session_id": "b2635b20"})))
    if terminal:
        # recorded in the TMUX namespace, as agent_watch really records it — the mismatch
        # this resolution has to bridge
        conn.execute("INSERT INTO event "
                     "(ts,ts_epoch,source,type,agent_id,project_id,severity,payload) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("t", 1001.0, "agent_watch", "agent_dead", "cp-canary:0.0",
                      cwd.rstrip("/").rsplit("/", 1)[-1], "high", "{}"))
    conn.commit()
    clw.register_delivery(event_id=event_id, target=target, project_id="owner-os",
                          conn=conn, now=1000.0)


def test_a_gone_session_resolves_instead_of_escalating_forever():
    conn, _ = clw._conn()
    _hook_wake(conn)
    out = clw.deregister_resolved(conn=conn, now=2000.0, agents=[])
    assert out and out[0]["reason"] == "target_session_no_longer_present"


def test_a_live_session_still_escalates_exactly_as_before():
    """Fail-closed: a pane still present keeps its watch, however quiet it is."""
    conn, _ = clw._conn()
    _hook_wake(conn)
    live = [{"target": "cp-canary:0.0", "claude_cwd": "/root/cp-canary-v2", "alive": True}]
    assert clw.deregister_resolved(conn=conn, now=2000.0, agents=live) == []


def test_a_gone_session_with_no_terminal_event_keeps_waking():
    """The owner must have been told once, by agent_dead, before this goes quiet."""
    conn, _ = clw._conn()
    _hook_wake(conn, event_id=9101, target="session:never-declared-dead", terminal=False)
    assert clw.deregister_resolved(conn=conn, now=2000.0, agents=[]) == []


def test_an_unknown_cwd_makes_no_claim():
    conn, _ = clw._conn()
    _hook_wake(conn, event_id=9102, cwd="", target="session:no-cwd")
    assert clw.deregister_resolved(conn=conn, now=2000.0, agents=[]) == []


# ── a prompt wake whose prompt is gone must stop escalating (16042→16068→16102) ────────
# The premise of agent_prompt_needs_response is that a question is on screen. That pane was
# idle, with no pending input and no assigned task, and the watchdog re-woke it and then
# escalated to CRITICAL over a question nobody was asking.

def _prompt_watch(conn, event_id=9200, etype="agent_prompt_needs_response",
                  target="seo-worker:0.0", cls="idle"):
    conn.execute("INSERT INTO event (id,ts,ts_epoch,source,type,agent_id,severity,payload) "
                 "VALUES (?,?,?,?,?,?,?,'{}')",
                 (event_id, "t", 1000.0, "agent_watch", etype, target, "high"))
    conn.execute("CREATE TABLE IF NOT EXISTS agent_watch_state ("
                 "target TEXT PRIMARY KEY, cls TEXT, digest TEXT, at TEXT, ts REAL,"
                 "notified_cls TEXT, notified_digest TEXT, notified_at TEXT,"
                 "notified_ts REAL, emissions INTEGER DEFAULT 0, miss_count INTEGER,"
                 "digest_since REAL)")
    conn.execute("INSERT OR REPLACE INTO agent_watch_state(target,cls) VALUES(?,?)",
                 (target, cls))
    conn.commit()
    clw.register_delivery(event_id=event_id, target=target, project_id="owner-os",
                          conn=conn, now=1000.0)


def test_a_prompt_wake_resolves_once_the_prompt_is_gone():
    conn, _ = clw._conn()
    _prompt_watch(conn)
    out = clw.deregister_resolved(conn=conn, now=2000.0)
    assert out and out[0]["reason"] == "prompt_no_longer_present"


def test_an_agent_still_prompting_keeps_escalating():
    """The genuine case must be untouched."""
    conn, _ = clw._conn()
    _prompt_watch(conn, event_id=9201, target="still-asking:0.0", cls="owner_prompt")
    assert clw.deregister_resolved(conn=conn, now=2000.0) == []


def test_a_blocker_class_pane_keeps_escalating():
    conn, _ = clw._conn()
    _prompt_watch(conn, event_id=9202, target="blocked:0.0", cls="blocker")
    assert clw.deregister_resolved(conn=conn, now=2000.0) == []


def test_a_non_prompt_wake_is_not_resolved_by_going_idle():
    """This must NOT become the general 'idle means done' claim."""
    conn, _ = clw._conn()
    _prompt_watch(conn, event_id=9203, etype="work_stopped_incomplete",
                  target="stopped:0.0", cls="idle")
    assert clw.deregister_resolved(conn=conn, now=2000.0) == []


def test_a_crashed_pane_still_wakes_even_for_a_prompt_wake():
    conn, _ = clw._conn()
    _prompt_watch(conn, event_id=9204, target="crashed-one:0.0", cls="crashed")
    assert clw.deregister_resolved(conn=conn, now=2000.0) == []


# ── the watch must keep the SOURCE project, with the route as a separate fact ─────────
# `pending_wake` never returned the originating project, so the companion passed the ROUTE
# KEY as `project_id`. Every SLO alarm about a /opt/seo agent was therefore filed under
# `owner-os`, the chat it was delivered to (events 16068, 16102).

def test_pending_wake_exposes_the_originating_project():
    """The companion cannot pass what it is never given."""
    import inspect
    from core import wake_bridge as _wb
    src = inspect.getsource(_wb.pending_wake)
    assert '"project_id": project_id' in src
    assert '"route_key": target["route_key"]' in src


def test_the_watch_keeps_project_and_route_apart():
    conn, _ = clw._conn()
    conn.execute("INSERT INTO event (id,ts,ts_epoch,source,type,agent_id,severity,payload) "
                 "VALUES (?,?,?,?,?,?,?,'{}')",
                 (9300, "t", 1000.0, "agent_watch", "agent_waiting_input",
                  "mess-postsignup-cleanup-sonnet-v4:0.0", "high"))
    conn.commit()
    clw.register_delivery(event_id=9300, target="mess-postsignup-cleanup-sonnet-v4:0.0",
                          project_id="seo", route_key="owner-os",
                          event_type="agent_waiting_input", conn=conn, now=1000.0)
    row = conn.execute("SELECT project_id, route_key FROM wake_loop_watch "
                       "WHERE event_id=9300").fetchone()
    assert row == ("seo", "owner-os")


def test_the_watchdog_files_its_alarm_under_the_source_project():
    """The alarm names the agent's project; the route travels as routing context."""
    conn, _ = clw._conn()
    conn.execute("INSERT INTO event (id,ts,ts_epoch,source,type,agent_id,severity,payload) "
                 "VALUES (?,?,?,?,?,?,?,'{}')",
                 (9301, "t", 1000.0, "agent_watch", "work_stopped_incomplete",
                  "seo-worker:0.0", "high"))
    conn.commit()
    clw.register_delivery(event_id=9301, target="seo-worker:0.0", project_id="seo",
                          route_key="owner-os", event_type="work_stopped_incomplete",
                          conn=conn, now=1000.0)
    seen = []

    def emit(source, etype, **kw):
        seen.append((etype, kw.get("project_id"), (kw.get("payload") or {}).get("route_key")))
        return {"event_id": 9999}

    clw.slo_scan(conn=conn, now=1000.0 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert seen and seen[0][0] == "wake_loop_no_progress"
    assert seen[0][1] == "seo"          # filed under the agent's real project
    assert seen[0][2] == "owner-os"     # route preserved, as routing context


def test_a_row_predating_the_route_column_still_scans():
    """A live DB has watches written before route_key existed."""
    conn, _ = clw._conn()
    conn.execute("INSERT INTO wake_loop_watch (event_id,target,project_id,delivered_ts,"
                 "delivered_at) VALUES (?,?,?,?,?)", (9302, "old:0.0", "owner-os", 1.0, "t"))
    conn.commit()
    clw.slo_scan(conn=conn, now=2.0, emit_fn=lambda *a, **k: {"event_id": 1})


def test_the_companion_passes_project_and_route_separately():
    """The third link: the companion must forward both, not substitute one for the other."""
    src = open("/root/ai-dev-runtime/tools/wake_companion.py", encoding="utf-8").read()
    call = src[src.index("closed_loop_wake.register_delivery("):][:400]
    assert 'project_id=p.get("project_id"' in call
    assert 'route_key=p.get("route_key"' in call
    assert 'project_id=p.get("route_key"' not in call     # the original defect


# ── one agent, two names (2026-09-01) ───────────────────────────────────────
# 34 `wake_loop_stalled` criticals in 24 h, and every one of their deliveries had
# already recorded `submitted_and_assistant_started_generating` — the wake landed
# and the assistant began generating. The watchdog escalated anyway.
#
# `agent_watch` files events under the tmux target (`gaika-opus:0.0`); the native
# hooks file theirs under `session:<conversation[:12]>`, because a hook knows its
# session and not the tmux world. Both are the same agent. `_progress_since`
# counted only one name, so it could not see `agent_turn_stopped` — at 835 events
# a day the most abundant proof of life in the system. A session-form target was
# worse off still: `agent_watch_state` is keyed by tmux target, so it has no row
# there at all and `pane_alive_and_working` could never resolve it.
#
# This module already names the defect for `runtimejob:` targets — "a job has no
# pane, so `_progress_since` can NEVER see progress for one; every runtimejob
# watch is a guaranteed future false positive". A plain agent had it too.

CONV = "cc43ebcf-6474-428f-a3e5-c034ba244e85"
PANE = "gaika-opus:0.0"
ALIAS = "session:cc43ebcf-647"


def _register_agent(conn, target=PANE, conv=CONV, cwd="/opt/gaika-extension"):
    from core.control_plane import api
    api.register_agent(target, session=target.split(":")[0], cwd=cwd,
                       conversation_id=conv, conn=conn)


def _emit_calls():
    calls = []

    def emit(source, etype, **kw):
        calls.append(etype)
        return {"event_id": 9000 + len(calls)}
    return calls, emit


def test_the_two_names_resolve_to_each_other(conn):
    _register_agent(conn)
    assert ALIAS in clw._identities(conn, PANE)
    assert PANE in clw._identities(conn, ALIAS)


def test_a_target_always_lists_itself_first(conn):
    """Best-effort by construction: never fewer identities than before."""
    assert clw._identities(conn, "unknown:0.0")[0] == "unknown:0.0"
    assert clw._identities(conn, "runtimejob:abc")[0] == "runtimejob:abc"
    assert clw._identities(conn, "")[0] == ""


def test_hook_progress_under_the_session_name_suppresses_the_rewake(conn):
    """The exact false positive: the agent is working and saying so every turn,
    under the only name a hook can know."""
    _register_agent(conn)
    calls, emit = _emit_calls()
    clw.register_delivery(event_id=71, target=PANE, project_id="gaika-extension",
                          conn=conn, now=NOW)
    cto.emit("claude_hook", "agent_turn_stopped", agent_id=ALIAS,
             project_id="gaika-extension", conn=conn)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and calls == []


def test_pane_progress_reaches_a_watch_registered_on_the_session_name(conn):
    """The same blindness in the other direction."""
    _register_agent(conn)
    calls, emit = _emit_calls()
    clw.register_delivery(event_id=72, target=ALIAS, project_id="gaika-extension",
                          conn=conn, now=NOW)
    cto.emit("agent_watch", "agent_state", agent_id=PANE,
             project_id="gaika-extension", conn=conn)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and calls == []


def test_a_session_watch_resolves_on_the_panes_working_class(conn):
    """`agent_watch_state` is keyed by tmux target, so a session-form watch could
    never be resolved by `pane_alive_and_working` before this."""
    _register_agent(conn)
    aw._conn(conn)                       # ensure agent_watch's own schema exists
    conn.execute("INSERT OR REPLACE INTO agent_watch_state (target, cls) VALUES (?,?)",
                 (PANE, "working"))
    conn.commit()
    assert clw._resolution_reason(conn, event_id=73, target=ALIAS) == \
        "pane_alive_and_working"


def test_a_genuinely_silent_agent_still_escalates(conn):
    """The cheap way to end false stalls is to stop reporting stalls. Nothing
    happens under EITHER name here, and the escalation must survive."""
    _register_agent(conn)
    calls, emit = _emit_calls()
    clw.register_delivery(event_id=74, target=PANE, project_id="gaika-extension",
                          conn=conn, now=NOW)
    clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    clw.slo_scan(conn=conn, now=NOW + 2 * clw.WAKE_LOOP_SLO_SECS + 60, emit_fn=emit)
    assert calls == ["wake_loop_no_progress", "wake_loop_stalled"]


def test_another_agents_activity_is_still_not_progress(conn):
    """Widening the identity set must not widen it to the whole fleet."""
    _register_agent(conn)
    _register_agent(conn, target="mess-opus:0.0", conv="90df737f-85c7-40e1-0000-000000000000",
                    cwd="/opt/mess")
    calls, emit = _emit_calls()
    clw.register_delivery(event_id=75, target=PANE, project_id="gaika-extension",
                          conn=conn, now=NOW)
    cto.emit("claude_hook", "agent_turn_stopped", agent_id="session:90df737f-85c",
             project_id="mess", conn=conn)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [e["event_id"] for e in r["rewoken"]] == [75]


def test_an_unreadable_registry_never_claims_resolution():
    """Unknown must never mean resolved: a lookup that raises yields the target
    alone, which is exactly the behaviour that existed before aliases."""
    class Broken:
        def execute(self, *a, **kw):
            raise RuntimeError("registry unreadable")

    assert clw._identities(Broken(), PANE) == (PANE,)
    assert clw._watch_state_cls(Broken(), PANE) == ""


# ── a job on an owner gate is waiting, not stalled (2026-09-01) ─────────────
# 6 of the 8 escalated `runtimejob:` watches on record were sitting in
# `waiting_approval`. `runtime_events` had already announced that properly as
# `owner_decision_required` (high); the job then sat exactly where it must until a
# human acted; `_progress_since` saw nothing move; and this watchdog escalated
# `wake_loop_stalled` — CRITICAL — telling the owner a second and louder time
# about a decision already sitting in their queue. Re-waking cannot help, because
# the only thing that ends the state is the owner.
#
# `runtime_watchdog` states the rule outright in its own module docstring —
# "`waiting_approval` is NEVER a stall: it is a true owner decision, announced
# once by the lifecycle bridge (runtime_events), not re-announced here" — and this
# module simply never learned it.

def _job_at(monkeypatch, tmp_path, status, name="gate.db"):
    monkeypatch.setenv("RUNTIME_DB", str(tmp_path / name))
    from core import job_store
    monkeypatch.setattr(job_store, "_DB", str(tmp_path / name))
    job_store.init_db()
    job = job_store.create_job(goal="needs a human", instructions="x",
                               project_path="/root/ai-dev-runtime")
    job_store.update_job(job["id"], status=status)
    return f"runtimejob:{job['id'][:8]}"


def test_a_job_awaiting_approval_is_resolved_not_escalated(conn, monkeypatch, tmp_path):
    target = _job_at(monkeypatch, tmp_path, "waiting_approval")
    emit = _Emit()
    clw.register_delivery(event_id=310, target=target, project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=NOW)
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["rewoken"] and not r["escalated"]
    assert r["deregistered"][0]["reason"] == "runtime_job_awaiting_owner"
    assert emit.calls == [], "the owner must not be told a second time"


def test_awaiting_owner_is_not_reported_as_terminal(conn, monkeypatch, tmp_path):
    """The job is not finished, and conflating the two would let a parked job be
    read as a completed one."""
    target = _job_at(monkeypatch, tmp_path, "waiting_approval", name="gate2.db")
    assert clw._runtime_job_terminal(target) is False
    assert clw._resolution_reason(conn, event_id=311, target=target) == \
        "runtime_job_awaiting_owner"


def test_an_executing_job_still_escalates(conn, monkeypatch, tmp_path):
    """The fix must resolve only owner-gated jobs, never every runtimejob watch."""
    from core.control_plane.store import now_ts
    target = _job_at(monkeypatch, tmp_path, "editing", name="gate3.db")
    t0 = now_ts()
    emit = _Emit()
    clw.register_delivery(event_id=312, target=target, project_id="owner-os",
                          event_type="owner_decision_required", conn=conn, now=t0)
    r = clw.slo_scan(conn=conn, now=t0 + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert not r["deregistered"]
    assert [e["event_id"] for e in r["rewoken"]] == [312]


def test_an_unreadable_job_store_resolves_nothing(conn, monkeypatch):
    """A store this module cannot see must never be treated as resolved."""
    from core import job_store
    monkeypatch.setattr(job_store, "_DB", "/nonexistent/dir/jobs.db")
    assert clw._runtime_job_status("runtimejob:deadbeef") == ""
    assert clw._runtime_job_terminal("runtimejob:deadbeef") is False
    assert clw._resolution_reason(conn, event_id=313,
                                  target="runtimejob:deadbeef") is None


def test_only_the_status_that_both_wakes_and_parks_is_gated():
    """`draft`/`superseded` never wake, so they can never open a watch — they are
    deliberately not in the set, rather than added on speculation."""
    from core import runtime_events as re_
    assert clw._RUNTIME_JOB_OWNER_GATE_STATUSES == {"waiting_approval"}
    ev, _sev, oar = re_.EVENT_FOR_STATUS["waiting_approval"]
    assert ev == "owner_decision_required" and oar is True
    assert "draft" not in re_.EVENT_FOR_STATUS
    assert not (clw._RUNTIME_JOB_OWNER_GATE_STATUSES
                & clw._RUNTIME_JOB_TERMINAL_STATUSES)


# ── a watch is ABOUT an agent (2026-09-02) ─────────────────────────────────
# Fourteen live rows carried an empty target. `slo_scan` skips those by name, so
# each could never resolve, never progress and never escalate: a row that lives
# forever and means nothing. Every one of them had the answer on its own event the
# whole time — `agent_id` IS the target (`runtimejob:bea93aec`,
# `capacity-blockchain:0.0`, `agent_waiting_input` panes). The caller did not pass
# it and nothing looked.

def test_a_missing_target_is_taken_from_the_event(conn):
    cto.emit("agent_watch", "agent_waiting_input", agent_id="capacity-blockchain:0.0",
             project_id="capacity", conn=conn)
    eid = conn.execute("SELECT MAX(id) FROM event").fetchone()[0]
    clw.register_delivery(event_id=eid, project_id="capacity", conn=conn, now=NOW)
    row = conn.execute("SELECT target FROM wake_loop_watch WHERE event_id=?",
                       (eid,)).fetchone()
    assert row and row[0] == "capacity-blockchain:0.0"


def test_an_explicit_target_still_wins(conn):
    cto.emit("agent_watch", "agent_waiting_input", agent_id="from-event:0.0",
             project_id="p", conn=conn)
    eid = conn.execute("SELECT MAX(id) FROM event").fetchone()[0]
    clw.register_delivery(event_id=eid, target="explicit:0.0", project_id="p",
                          conn=conn, now=NOW)
    row = conn.execute("SELECT target FROM wake_loop_watch WHERE event_id=?",
                       (eid,)).fetchone()
    assert row[0] == "explicit:0.0"


def test_a_watch_with_no_nameable_subject_is_not_created(conn):
    """Refusing is the honest outcome — a tracking row for an unnameable subject is
    not tracking."""
    clw.register_delivery(event_id=987654, project_id="p", conn=conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM wake_loop_watch WHERE event_id=987654"
                        ).fetchone()[0] == 0


def test_an_event_whose_agent_id_is_blank_creates_nothing(conn):
    cto.emit("controller", "owner_decision_required", project_id="owner-os", conn=conn)
    eid = conn.execute("SELECT MAX(id) FROM event").fetchone()[0]
    clw.register_delivery(event_id=eid, project_id="owner-os", conn=conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM wake_loop_watch WHERE event_id=?",
                        (eid,)).fetchone()[0] == 0


def test_rows_written_before_the_guard_are_retired_not_left_open(conn):
    """The fourteen already on disk. Retiring beats leaving a permanent row that a
    future backfill of `target` would silently re-animate into a weeks-late wake."""
    clw._conn(conn)                     # ensure the watch schema exists
    conn.execute("INSERT INTO wake_loop_watch (event_id,target,project_id,route_key,"
                 "delivered_ts,delivered_at) VALUES (?,?,?,?,?,?)",
                 (4863, "", "owner-os", "", NOW, "then"))
    conn.commit()
    assert clw._resolution_reason(conn, event_id=4863, target="") == "watch_has_no_target"

    emit = _Emit()
    r = clw.slo_scan(conn=conn, now=NOW + 3 * clw.WAKE_LOOP_SLO_SECS, emit_fn=emit)
    assert 4863 in [d["event_id"] for d in r["deregistered"]]
    assert not r["rewoken"] and not r["escalated"]
    assert emit.calls == [], "retiring an orphan must not wake anyone"


# ── a wake that worked is FINISHED (2026-09-02) ────────────────────────────
# `slo_scan` treated observed progress as a reason to skip the row for one pass,
# never as the state a watch reaches by SUCCEEDING. So a watch whose wake plainly
# worked stayed open forever, re-evaluated on every scan for the life of the row.
# Observed live: 26 open watches for a single session, every one with progress
# recorded, the oldest 107 hours old. They can never fire — progress measured from
# delivery only ever accumulates — and never close. Inert, but immortal.

def test_a_watch_whose_wake_produced_progress_is_retired(conn):
    clw.register_delivery(event_id=800, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    cto.emit("agent_watch", "agent_state", agent_id="mess:0.0", project_id="mess",
             conn=conn)
    assert clw._resolution_reason(conn, event_id=800, target="mess:0.0",
                                  delivered_ts=NOW) == "progress_observed"

    emit = _Emit()
    r = clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    assert [d["event_id"] for d in r["deregistered"]] == [800]
    assert r["deregistered"][0]["reason"] == "progress_observed"
    assert emit.calls == [], "retiring a successful watch must not wake anyone"


def test_it_does_not_reopen_on_a_later_scan(conn):
    """The point of a terminal state: the row stops being work."""
    clw.register_delivery(event_id=801, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    cto.emit("agent_watch", "agent_state", agent_id="mess:0.0", project_id="mess",
             conn=conn)
    emit = _Emit()
    clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    r2 = clw.slo_scan(conn=conn, now=NOW + 9 * clw.WAKE_LOOP_SLO_SECS, emit_fn=emit)
    assert r2["deregistered"] == [] and not r2["rewoken"] and not r2["escalated"]


def test_silence_after_delivery_still_escalates(conn):
    """Retiring on progress must not become retiring on everything: escalation
    requires the ABSENCE of exactly what `progress_observed` asserts."""
    clw.register_delivery(event_id=802, target="quiet:0.0", project_id="mess",
                          conn=conn, now=NOW)
    emit = _Emit()
    clw.slo_scan(conn=conn, now=NOW + clw.WAKE_LOOP_SLO_SECS + 1, emit_fn=emit)
    clw.slo_scan(conn=conn, now=NOW + 2 * clw.WAKE_LOOP_SLO_SECS + 60, emit_fn=emit)
    assert [c["type"] for c in emit.calls] == ["wake_loop_no_progress",
                                               "wake_loop_stalled"]


def test_a_structural_reason_still_wins_the_audit_trail(conn):
    """`progress_observed` is the weakest claim — it says only that something
    happened afterwards — so it is checked last."""
    aw._conn(conn)
    conn.execute("INSERT OR REPLACE INTO agent_watch_state (target, cls) VALUES (?,?)",
                 ("mess:0.0", "working"))
    conn.commit()
    clw.register_delivery(event_id=803, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    cto.emit("agent_watch", "agent_state", agent_id="mess:0.0", project_id="mess",
             conn=conn)
    assert clw._resolution_reason(conn, event_id=803, target="mess:0.0",
                                  delivered_ts=NOW) == "pane_alive_and_working"


def test_a_caller_that_omits_delivered_ts_gets_the_old_behaviour(conn):
    """Progress is only claimed when the caller supplies the delivery time."""
    clw.register_delivery(event_id=804, target="mess:0.0", project_id="mess",
                          conn=conn, now=NOW)
    cto.emit("agent_watch", "agent_state", agent_id="mess:0.0", project_id="mess",
             conn=conn)
    assert clw._resolution_reason(conn, event_id=804, target="mess:0.0") is None
