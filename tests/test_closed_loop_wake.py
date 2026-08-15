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
