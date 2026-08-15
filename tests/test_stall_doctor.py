"""Stall Doctor — regressions for the three live stall shapes of 2026-08-15
~12:27 (owner screenshot): lost continuation (gaika-video), passive internal
wait (payorch-live-buttons), child workflow wait (jobhunter-media-audit) —
plus the positive controls that keep the doctor quiet and safe."""
import sqlite3

import pytest

from core import stall_doctor as sd


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


NOW = 1_700_000_000.0

# ── live fixtures ───────────────────────────────────────────────────────────
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

def jobhunter_tail(done=32):
    return f"""\
✻ Waiting for 1 dynamic workflow to finish
  4 tasks (0 done, 4 open)
───────────────────────────
❯ continue autonomously when the audit finishes
───────────────────────────
  ◯ fable-second-pass-audit {done}/71 agents
"""


def _agent(target="gaika-video:0.0", cwd="/opt/gaika-drop", state="waiting_input"):
    return {"target": target, "cwd": cwd, "claude_cwd": cwd, "state": state,
            "alive": True, "is_agent": True}


class Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"source": source, "type": etype, **kw})
        return {"event_id": len(self.calls)}


class Deliver:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, target, cwd, *, action, text):
        self.calls.append({"target": target, "action": action, "text": text})
        return {"ok": self.ok}


def _scan(agents, tails, pendings, emit, deliver, conn, now=NOW):
    return sd.scan(agents=agents, read_fn=lambda t: tails[t],
                   pending_fn=lambda t, tail, cwd: pendings.get(t, ""),
                   deliver_fn=deliver, emit_fn=emit, conn=conn, now=now)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "doc.db"))
    yield c
    c.close()


# ── classification ──────────────────────────────────────────────────────────

def test_classify_the_three_live_shapes():
    a = sd.classify_wait(GAIKA_TAIL, state="waiting_input", pending=GAIKA_PENDING)
    assert a["shape"] == sd.LOST_CONTINUATION
    b = sd.classify_wait(PAYORCH_TAIL, state="idle")
    assert b["shape"] == sd.INTERNAL_WAIT
    c = sd.classify_wait(jobhunter_tail(), state="waiting_input",
                         pending="continue autonomously when the audit finishes")
    assert c["shape"] == sd.CHILD_WORKFLOW_WAIT
    assert c["child"]["name"] == "fable-second-pass-audit" and c["child"]["done"] == 32


def test_working_and_owner_prompt_are_not_doctor_domain():
    assert sd.classify_wait(GAIKA_TAIL, state="working",
                            pending=GAIKA_PENDING)["shape"] == sd.NONE
    menu = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
    assert sd.classify_wait(menu, state="idle")["shape"] == sd.OWNER_WAIT


def test_wait_naming_an_owner_power_escalates_not_nudges():
    """Task 211 regression (e): an internal wait that NAMES an owner power (no live
    dialog on screen) must classify distinctly from a real dialog/menu (OWNER_WAIT,
    agent_watch's domain) and must actually escalate through scan(), not be silently
    dropped as 'not_doctor_domain' the way OWNER_WAIT is."""
    t = "Waiting for gate results — owner approval needed before deploy."
    c = sd.classify_wait(t, state="idle")
    assert c["shape"] == sd.OWNER_DECISION_WAIT
    assert c["shape"] != sd.OWNER_WAIT

    emit, dlv = Emit(), Deliver()
    ag = [_agent(target="payorch-live:0.0", cwd="/opt/payorch")]
    tails = {"payorch-live:0.0": t}
    r1 = _scan(ag, tails, {}, emit, dlv, conn=sqlite3.connect(":memory:"), now=NOW)
    assert not r1["acted"], "quiet within the SLO"


# ── provenance gate for queued lines ────────────────────────────────────────

def test_may_submit_queued_gate():
    assert sd.may_submit_queued(GAIKA_PENDING)[0] is True
    assert sd.may_submit_queued("1")[0] is False                           # dialog answer
    assert sd.may_submit_queued("yes")[0] is False
    assert sd.may_submit_queued("да")[0] is False                          # ru dialog answer
    assert sd.may_submit_queued("proceed and push to origin")[0] is False  # forbidden
    assert sd.may_submit_queued("")[0] is False


def test_russian_owner_instruction_is_evaluable_and_submittable():
    """Second gaika-video incident (2026-08-15): a benign RUSSIAN queued
    instruction sat 10+ minutes because the old gate refused ALL non-ASCII.
    Russian is evaluable — the watchdog denylist carries its destructive
    stems — so benign Russian submits, destructive Russian never does."""
    ok, why = sd.may_submit_queued(
        "Продолжай по инструкции: почини вступление, затем проверь "
        "публичные ссылки UA и EN.")
    assert ok, why
    assert sd.may_submit_queued("дай финальные ссылки RU и EN")[0] is True
    # the 2026-08-03 incident text itself stays refused
    assert sd.may_submit_queued("удали старый scratchpad")[0] is False
    # destructive / live / power stems in Russian and English stay refused
    for bad in ("задеплой на прод", "переведи деньги на кошелек",
                "merge the branch to main", "мердж в main",
                "останови все сервисы", "запушь ветку"):
        got, why = sd.may_submit_queued(bad)
        assert got is False, f"{bad!r} must not auto-submit ({why})"
    # scripts the denylists cannot read stay unevaluable
    assert sd.may_submit_queued("继续执行任务")[0] is False


# ── shape A: lost continuation ──────────────────────────────────────────────

def test_lost_continuation_resubmits_after_slo(conn):
    emit, dlv = Emit(), Deliver()
    ag = [_agent()]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": GAIKA_PENDING}
    # first sight: episode opens, within SLO -> quiet
    r1 = _scan(ag, tails, pend, emit, dlv, conn, now=NOW)
    assert not r1["acted"] and not dlv.calls
    # past the queued-line SLO with no progress -> Enter on the owner's own line
    r2 = _scan(ag, tails, pend, emit, dlv, conn, now=NOW + sd.QUEUED_SLO_SECS + 1)
    assert [a["action"] for a in r2["acted"]] == ["submit_queued"]
    assert dlv.calls[0]["action"] == "submit" and dlv.calls[0]["text"] == GAIKA_PENDING
    # the auto-action is audited, not woken
    assert emit.calls[-1]["type"] == "stall_doctor_action"
    assert emit.calls[-1]["owner_action_required"] is False


def test_russian_lost_continuation_end_to_end_submits(conn):
    """The EXACT second-incident shape: Russian benign instruction queued,
    agent waiting_input, no progress — must be auto-submitted, not escalated
    and not left for the owner to find in the terminal."""
    emit, dlv = Emit(), Deliver()
    ru = "Продолжай по инструкции: почини вступление, затем проверь ссылки UA и EN."
    ag = [_agent()]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": ru}
    _scan(ag, tails, pend, emit, dlv, conn, now=NOW)
    r = _scan(ag, tails, pend, emit, dlv, conn, now=NOW + sd.QUEUED_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["submit_queued"]
    assert dlv.calls[0]["action"] == "submit" and dlv.calls[0]["text"] == ru
    # audited, no owner wake
    assert emit.calls[-1]["type"] == "stall_doctor_action"
    assert all(not c.get("owner_action_required") for c in emit.calls)


def test_unsubmittable_queued_line_escalates_once(conn):
    emit, dlv = Emit(), Deliver()
    ag = [_agent()]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": "удали старый scratchpad и перезапусти сервис"}
    _scan(ag, tails, pend, emit, dlv, conn, now=NOW)
    r = _scan(ag, tails, pend, emit, dlv, conn, now=NOW + sd.QUEUED_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["escalate"]
    assert emit.calls[-1]["type"] == "agent_waiting_input"
    assert emit.calls[-1]["owner_action_required"] is True
    assert not dlv.calls
    # and only once — the escalation does not repeat every tick
    r2 = _scan(ag, tails, pend, emit, dlv, conn, now=NOW + sd.QUEUED_SLO_SECS + 60)
    assert not r2["acted"]


# ── shape B: passive internal wait ──────────────────────────────────────────

def test_internal_wait_gets_safe_nudge_not_owner_ping(conn):
    emit, dlv = Emit(), Deliver()
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    r = _scan(ag, tails, {}, emit, dlv, conn, now=NOW + sd.WAIT_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["nudge"]
    assert dlv.calls[0]["action"] == "deliver"
    assert dlv.calls[0]["text"] == sd.NUDGE_INTERNAL
    # the nudge itself passes the fail-closed continuation classifier
    from core.agent_continuation_watchdog import is_safe_continuation
    assert is_safe_continuation(sd.NUDGE_INTERNAL)
    assert is_safe_continuation(sd.NUDGE_CHILD)
    # no owner wake for an internal wait
    assert all(not c.get("owner_action_required") for c in emit.calls)


# ── shape C: child workflow wait ────────────────────────────────────────────

def test_progressing_child_keeps_the_doctor_quiet(conn):
    emit, dlv = Emit(), Deliver()
    t = "jobhunter-media-audit:0.0"
    ag = [_agent(t, "/opt/jobhunter-ai")]
    pend = {t: "continue autonomously when the audit finishes"}
    _scan(ag, {t: jobhunter_tail(32)}, pend, emit, dlv, conn, now=NOW)
    # counter moved -> new episode every time, clock resets, silence forever
    for i, done in enumerate((38, 45, 52), start=1):
        r = _scan(ag, {t: jobhunter_tail(done)}, pend, emit, dlv, conn,
                  now=NOW + i * sd.CHILD_SLO_SECS)
        assert not r["acted"], f"nudged a PROGRESSING child at {done}/71"
    assert not dlv.calls and not emit.calls


def test_stale_child_gets_diagnose_nudge(conn):
    emit, dlv = Emit(), Deliver()
    t = "jobhunter-media-audit:0.0"
    ag = [_agent(t, "/opt/jobhunter-ai")]
    _scan(ag, {t: jobhunter_tail(38)}, {}, emit, dlv, conn, now=NOW)
    r = _scan(ag, {t: jobhunter_tail(38)}, {}, emit, dlv, conn,
              now=NOW + sd.CHILD_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["nudge"]
    assert dlv.calls[0]["text"] == sd.NUDGE_CHILD


# ── loop guard / cooldown / scope ───────────────────────────────────────────

def test_loop_guard_caps_actions_then_escalates(conn):
    emit, dlv = Emit(), Deliver()
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    base = NOW + sd.WAIT_SLO_SECS + 1
    for i in range(sd.MAX_ACTIONS_PER_EPISODE):
        r = _scan(ag, tails, {}, emit, dlv, conn,
                  now=base + i * (sd.ACTION_COOLDOWN_SECS + 1))
        assert [a["action"] for a in r["acted"]] == ["nudge"]
    r = _scan(ag, tails, {}, emit, dlv, conn,
              now=base + sd.MAX_ACTIONS_PER_EPISODE * (sd.ACTION_COOLDOWN_SECS + 1))
    assert [a["action"] for a in r["acted"]] == ["escalate"]
    assert len(dlv.calls) == sd.MAX_ACTIONS_PER_EPISODE


def test_cooldown_blocks_rapid_repeat_actions(conn):
    emit, dlv = Emit(), Deliver()
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW + sd.WAIT_SLO_SECS + 1)
    r = _scan(ag, tails, {}, emit, dlv, conn, now=NOW + sd.WAIT_SLO_SECS + 30)
    assert not r["acted"] and len(dlv.calls) == 1


def test_actuation_scope_env(monkeypatch, conn):
    monkeypatch.setattr(sd, "ACTUATE", "some-other:0.0")
    emit, dlv = Emit(), Deliver()
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    r = _scan(ag, tails, {}, emit, dlv, conn, now=NOW + sd.WAIT_SLO_SECS + 1)
    assert not dlv.calls
    assert any(s.get("why") == "actuation_not_allowed" for s in r["skipped"])


def test_progress_resets_episode_and_rearm(conn):
    emit, dlv = Emit(), Deliver()
    t = "gaika-video:0.0"
    ag = [_agent(t)]
    pend = {t: GAIKA_PENDING}
    _scan(ag, {t: GAIKA_TAIL}, pend, emit, dlv, conn, now=NOW)
    # agent went back to work -> state row cleared
    r = _scan([_agent(t, state="working")], {t: GAIKA_TAIL}, pend, emit, dlv,
              conn, now=NOW + 60)
    assert any(s["why"] == "shape_NONE" for s in r["skipped"])
    # a later identical stall is a NEW episode with a fresh SLO clock
    r2 = _scan(ag, {t: GAIKA_TAIL}, pend, emit, dlv, conn, now=NOW + 120)
    assert not r2["acted"]

# ── stale escalation retirement (chemmy-fast 2026-08-15 17:10) ──────────────

BAD_PENDING = "удали старый scratchpad и перезапусти сервис"


def _escalate_for_real(conn, now=NOW):
    """Drive a real escalation through the real event pipeline (sandbox DB)."""
    from core.control_plane.cto import emit
    from core.control_plane.store import init_db
    init_db(conn)
    dlv = Deliver()
    ag = [_agent()]
    tails = {"gaika-video:0.0": GAIKA_TAIL}
    pend = {"gaika-video:0.0": BAD_PENDING}
    sd.scan(agents=ag, read_fn=lambda t: tails[t],
            pending_fn=lambda t, tail, cwd: pend.get(t, ""),
            deliver_fn=dlv, emit_fn=emit, conn=conn, now=now)
    r = sd.scan(agents=ag, read_fn=lambda t: tails[t],
                pending_fn=lambda t, tail, cwd: pend.get(t, ""),
                deliver_fn=dlv, emit_fn=emit, conn=conn,
                now=now + sd.QUEUED_SLO_SECS + 1)
    assert [a["action"] for a in r["acted"]] == ["escalate"]
    eid = r["acted"][0]["event_id"]
    assert eid
    return eid


def _is_invalid(conn, eid):
    from core import agent_watch
    agent_watch._conn(conn)  # overlay table may not exist if nothing retired
    return conn.execute("SELECT 1 FROM agent_alert_invalid WHERE event_id=?",
                        (eid,)).fetchone() is not None


def test_escalation_retired_when_pane_resumes(conn):
    """chemmy-fast live case: escalated on an unsubmittable queued line, then
    resumed working minutes later — the actionable owner ping must be retired
    (invalid overlay + wake ack), not left paging for a stall that is gone."""
    from core.control_plane.cto import emit
    eid = _escalate_for_real(conn)
    working = [_agent(state="working")]
    sd.scan(agents=working, read_fn=lambda t: GAIKA_TAIL,
            pending_fn=lambda t, tail, cwd: "", deliver_fn=Deliver(),
            emit_fn=emit, conn=conn, now=NOW + sd.QUEUED_SLO_SECS + 120)
    assert _is_invalid(conn, eid), "stale escalation not retired on resume"
    led = conn.execute("SELECT action, delivered FROM stall_doctor_action "
                       "WHERE action='retire_escalation'").fetchone()
    assert led is not None


def test_escalation_retired_when_episode_moves_on(conn):
    """Digest moved (composer/pane changed) while the escalation stood — the
    old ping describes a state that no longer exists; retire it."""
    from core.control_plane.cto import emit
    eid = _escalate_for_real(conn)
    moved_tail = GAIKA_TAIL.replace("All link checks documented.",
                                    "Now verifying the deployed links instead.")
    assert sd._digest(moved_tail, None) != sd._digest(GAIKA_TAIL, None)
    sd.scan(agents=[_agent()], read_fn=lambda t: moved_tail,
            pending_fn=lambda t, tail, cwd: GAIKA_PENDING, deliver_fn=Deliver(),
            emit_fn=emit, conn=conn, now=NOW + sd.QUEUED_SLO_SECS + 120)
    assert _is_invalid(conn, eid), "stale escalation not retired on digest move"


def test_standing_escalation_is_not_retired(conn):
    """Positive control: while the SAME unsubmittable stall persists, the
    escalation stays valid — retirement only fires on real resolution."""
    from core.control_plane.cto import emit
    eid = _escalate_for_real(conn)
    sd.scan(agents=[_agent()], read_fn=lambda t: GAIKA_TAIL,
            pending_fn=lambda t, tail, cwd: BAD_PENDING, deliver_fn=Deliver(),
            emit_fn=emit, conn=conn, now=NOW + sd.QUEUED_SLO_SECS + 120)
    assert not _is_invalid(conn, eid), "live escalation wrongly retired"


# ── failed-delivery fast retry (gaika-presentation 2026-08-15 10:28) ────────

def test_failed_delivery_retries_on_short_clock(conn):
    """A verify=False delivery proves nothing about the pane; the bounded
    retry must come after FAILED_RETRY_COOLDOWN_SECS, not the full success
    cooldown (gaika-presentation sat ~30 idle minutes)."""
    emit, dlv = Emit(), Deliver(ok=False)
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    base = NOW + sd.WAIT_SLO_SECS + 1
    r1 = _scan(ag, tails, {}, emit, dlv, conn, now=base)
    assert [a["action"] for a in r1["acted"]] == ["nudge"] and len(dlv.calls) == 1
    # still inside the failed-retry window -> quiet, with the failed reason
    r2 = _scan(ag, tails, {}, emit, dlv, conn,
               now=base + sd.FAILED_RETRY_COOLDOWN_SECS - 10)
    assert not r2["acted"]
    assert any(s.get("why") == "failed_action_cooldown" for s in r2["skipped"])
    # past the short clock (far inside the long one) -> bounded retry fires
    r3 = _scan(ag, tails, {}, emit, dlv, conn,
               now=base + sd.FAILED_RETRY_COOLDOWN_SECS + 1)
    assert [a["action"] for a in r3["acted"]] == ["nudge"] and len(dlv.calls) == 2


def test_successful_delivery_keeps_long_cooldown(conn):
    """Positive control: after a VERIFIED delivery the long cooldown holds —
    the short clock is only for failures."""
    emit, dlv = Emit(), Deliver(ok=True)
    t = "payorch-live-buttons:0.0"
    ag = [_agent(t, "/opt/payment-orchestrator", state="idle")]
    tails = {t: PAYORCH_TAIL}
    _scan(ag, tails, {}, emit, dlv, conn, now=NOW)
    base = NOW + sd.WAIT_SLO_SECS + 1
    _scan(ag, tails, {}, emit, dlv, conn, now=base)
    r = _scan(ag, tails, {}, emit, dlv, conn,
              now=base + sd.FAILED_RETRY_COOLDOWN_SECS + 1)
    assert not r["acted"] and len(dlv.calls) == 1
    assert any(s.get("why") == "action_cooldown" for s in r["skipped"])
