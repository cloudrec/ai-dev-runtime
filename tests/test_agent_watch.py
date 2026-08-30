"""Agent watch: real pane states become owner notifications, without spam or lies.

Two generations of live failure are pinned here. First: gaika-ext-audit paused awaiting
migration instructions, gaika-ip-seal at a literal permission menu — zero notifications,
because nothing read pane text. Second, after the first fix: false completions for agents
whose own summaries said "4 shells still running" / "1 in progress, 4 open", a stale
quoted blocker phrase flagging the actively-working maintenance pane, and one completion
emitted twice across a restart. The rules under test: inventory state first, current
bottom region only, continuation evidence suppresses completion, class-level dedupe for
terminal classes, restart replays nothing.
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw

PROMPT_TAIL = """╭───────────────────────────────╮
 Bash command: rm -rf build/
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again
   3. No, and tell Claude what to do differently
╰───────────────────────────────╯"""

BLOCKER_TAIL = """Migration analysis finished.
Development paused at safe checkpoint.
Waiting for migration instructions from the owner."""

WORKING_TAIL = "✻ Compacting… (esc to interrupt · 32.1k tokens)"
IDLE_TAIL = "All checks passed. Report written to reports/AUDIT.md."


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


class _Emit:
    def __init__(self):
        self.calls = []
        self._n = 0

    def __call__(self, source, etype, **kw):
        self._n += 1
        self.calls.append({"source": source, "type": etype, **kw})
        return {"event_id": self._n}


def _agent(target="gaika-ext-audit:0.0", cwd="/opt/gaika-drop", alive=True,
           state="waiting_input"):
    return {"target": target, "cwd": cwd, "claude_cwd": cwd,
            "alive": alive, "is_agent": True, "state": state}


def _scan(agents, tails, emit, now=1000.0):
    return aw.scan(agents=agents, read_fn=lambda t: tails[t], emit_fn=emit, now=now)


# ── the classes ─────────────────────────────────────────────────────────────
def test_a_permission_prompt_is_an_actionable_owner_event():
    emit = _Emit()
    r = _scan([_agent("ip-seal:0.0", "/opt/clients-help-landing",
                      state="waiting_owner")],
              {"ip-seal:0.0": PROMPT_TAIL}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]
    call = emit.calls[0]
    assert call["type"] == "agent_prompt_needs_response"
    assert call["owner_action_required"] is True and call["severity"] == "high"
    assert "Do you want to proceed" in call["payload"]["excerpt"]


def test_paused_waiting_for_instructions_is_an_actionable_blocker():
    emit = _Emit()
    r = _scan([_agent()], {"gaika-ext-audit:0.0": BLOCKER_TAIL}, emit)
    assert [e["class"] for e in r["emitted"]] == ["blocker"]
    assert emit.calls[0]["type"] == "agent_waiting_input"


def test_line_wrapped_blocker_text_is_still_evidence():
    wrapped = "Development paused. Waiting for\n  migration\n  instructions."
    emit = _Emit()
    r = _scan([_agent()], {"gaika-ext-audit:0.0": wrapped}, emit)
    assert [e["class"] for e in r["emitted"]] == ["blocker"]


def test_coming_to_rest_after_work_is_one_completion():
    emit = _Emit()
    _scan([_agent(state="working")], {"gaika-ext-audit:0.0": WORKING_TAIL}, emit)
    assert emit.calls == []                      # working is not news
    r = _scan([_agent(state="waiting_input")], {"gaika-ext-audit:0.0": IDLE_TAIL},
              emit, now=1100.0)
    assert [e["class"] for e in r["emitted"]] == ["completed"]
    assert emit.calls[0]["type"] == "task_completed"


def test_a_vanished_working_pane_is_a_critical_crash_after_two_missed_scans():
    emit = _Emit()
    _scan([_agent(state="working")], {"gaika-ext-audit:0.0": WORKING_TAIL}, emit)
    r1 = _scan([], {}, emit, now=1100.0)         # first miss: waiting for confirmation
    assert r1["emitted"] == [] and emit.calls == []
    r2 = _scan([], {}, emit, now=1120.0)         # second consecutive miss: crash
    assert [e["class"] for e in r2["emitted"]] == ["crashed"]
    assert emit.calls[0]["type"] == "agent_process_failed"
    assert emit.calls[0]["severity"] == "critical"


def test_a_single_missed_scan_is_not_a_crash():
    """event 4393: this session's own live pane dropped out of one inventory sweep while
    its Claude process was busy, and was declared crashed. Presence resets the count."""
    emit = _Emit()
    t = "fable-wake-fix:0.0"
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    _scan([], {}, emit, now=1100.0)                              # one miss
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="working")],
              {t: WORKING_TAIL}, emit, now=1120.0)               # it is back
    assert r["emitted"] == [] and emit.calls == []
    _scan([], {}, emit, now=1140.0)                              # miss again: count reset
    r2 = _scan([], {}, emit, now=1160.0)
    assert [e["class"] for e in r2["emitted"]] == ["crashed"]    # two in a row now


def test_a_pane_that_left_from_rest_is_not_a_crash():
    emit = _Emit()
    _scan([_agent(state="idle")], {"gaika-ext-audit:0.0": IDLE_TAIL}, emit)
    r = _scan([], {}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


# ── the observed false positives, verbatim ─────────────────────────────────
GAIKA_VIDEO_REST = """Mux render pipeline checkpoint written.
Summary: 4 shells still running, QA pass next, then UA/EN localisation.
Continue after mux completes."""

JOBHUNTER_REST = "Status: 6 tasks (1 done, 1 in progress, 4 open). Next: wallet flows."

FABLE_STALE = """The owner reported the blocker text was:
"Development paused... Waiting for migration instructions."
Now editing core/agent_watch.py to fix the classifier."""


def test_still_running_shells_suppress_completion():
    """gaika-video, live: came to rest while its own summary said the work continues."""
    emit = _Emit()
    _scan([_agent("gaika-video:0.0", "/opt/gaika-video", state="working")],
          {"gaika-video:0.0": WORKING_TAIL}, emit)
    r = _scan([_agent("gaika-video:0.0", "/opt/gaika-video", state="waiting_input")],
              {"gaika-video:0.0": GAIKA_VIDEO_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_open_and_in_progress_tasks_suppress_completion():
    """jobhunter, live: 1 in progress and 4 open is not done."""
    emit = _Emit()
    _scan([_agent("jh:0.0", "/opt/jobhunter-ai", state="working")],
          {"jh:0.0": WORKING_TAIL}, emit)
    r = _scan([_agent("jh:0.0", "/opt/jobhunter-ai", state="waiting_input")],
              {"jh:0.0": JOBHUNTER_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


JOBHUNTER_TODO_REST = ("to merge/deploy on your end. Decide honest payout stance + "
                       "finish worker UX… ⎿  ◼ Decide honest … ◻ Audit + verify… "
                       "◻ Full QA sweep … ◻ Merge, deploy … ◻ Write final re… "
                       "… +1 completed")


def test_open_todo_checkboxes_suppress_completion():
    """jobhunter, live again (event 4086): the CLI todo widget at rest with unchecked
    boxes is open work, not a finish."""
    emit = _Emit()
    t = "jh:0.0"
    _scan([_agent(t, "/opt/jobhunter-ai", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/jobhunter-ai", state="idle")],
              {t: JOBHUNTER_TODO_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_a_suppressed_completion_still_fires_when_the_work_actually_finishes():
    emit = _Emit()
    t = "gaika-video:0.0"
    _scan([_agent(t, "/opt/gaika-video", state="working")], {t: WORKING_TAIL}, emit)
    _scan([_agent(t, "/opt/gaika-video", state="waiting_input")],
          {t: GAIKA_VIDEO_REST}, emit, now=1100.0)         # suppressed, stays working
    r = _scan([_agent(t, "/opt/gaika-video", state="waiting_input")],
              {t: "Render finished. Final report saved."}, emit, now=1200.0)
    assert [e["class"] for e in r["emitted"]] == ["completed"]


def test_a_stale_quoted_blocker_never_overrides_a_working_state():
    """The watcher's own maintenance pane, live: scrollback QUOTED a blocker sentence
    while the inventory said working. Inventory state wins; nothing is emitted."""
    emit = _Emit()
    r = _scan([_agent("fable-wake-fix:0.0", "/root/ai-dev-runtime", state="working")],
              {"fable-wake-fix:0.0": FABLE_STALE}, emit)
    assert r["emitted"] == [] and emit.calls == []


def test_an_unchanged_completion_survives_a_restart_and_text_drift_without_a_second_event():
    """chemmy, live: 4070 then 4071 — the rest-screen text drifted between scans and the
    digest minted a 'new' completion. Terminal classes dedupe on class, not digest."""
    emit = _Emit()
    t = "chemmy-fast:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    _scan([_agent(t, "/opt/mess", state="waiting_input")],
          {t: "Release 0196 built. All done."}, emit, now=1100.0)
    assert len(emit.calls) == 1
    emit2 = _Emit()                                        # companion restarted
    r = _scan([_agent(t, "/opt/mess", state="waiting_input")],
              {t: "Release 0196 built. All done. (screen redrew, timestamp moved)"},
              emit2, now=1200.0)
    assert r["emitted"] == [] and emit2.calls == []


CHEMMY_MENU_REST = """Scope work is staged and ready.
What should I do next?
 1. Wait for their signal (Recommended)
 2. Give me a disjoint scope I can own end-to-end
 3. Stand down entirely
 4. Type something else
 5. Chat about this"""


def test_waiting_owner_with_a_choice_menu_is_a_prompt_never_a_completion():
    """chemmy, live (event 4088): a strategy menu after substantive work read like a
    finish. waiting_owner outranks completion, always."""
    emit = _Emit()
    t = "chemmy-fast:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/mess", state="waiting_owner")],
              {t: CHEMMY_MENU_REST}, emit, now=1100.0)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]
    assert emit.calls[-1]["type"] == "agent_prompt_needs_response"


def test_a_generic_numbered_menu_is_a_prompt_even_without_yes_no_wording():
    emit = _Emit()
    r = _scan([_agent(state="waiting_input")],
              {"gaika-ext-audit:0.0": CHEMMY_MENU_REST}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]


def test_idle_after_work_without_a_stated_finish_is_not_a_completion():
    """jobhunter, live (event 4086): UI metadata said idle while the plan text still
    read 'Decide honest payout stance + finish worker UX…'. Quietness is never done."""
    emit = _Emit()
    t = "jh:0.0"
    _scan([_agent(t, "/opt/jobhunter-ai", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/jobhunter-ai", state="idle")],
              {t: "Plan: Decide honest payout stance + polish worker UX next."},
              emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_recovered_process_retires_its_crash_alert():
    """Event 5123, live: payorch-live-buttons was declared crashed at 09:11 and
    was visibly working minutes later — the critical crash alert stood as
    current truth. A pane observed ALIVE again must retire its crash alerts
    via the audited invalid overlay (event rows untouched)."""
    from core.control_plane.api import append_event
    emit = _Emit()
    t = "payorch-live-buttons:0.0"
    # the pane vanishes twice while tracked -> crash announced
    _scan([_agent(t, "/opt/payment-orchestrator", state="working")],
          {t: WORKING_TAIL}, emit)
    _scan([], {}, emit, now=1100.0)
    r = _scan([], {}, emit, now=1200.0)
    assert [e["class"] for e in r["emitted"]] == ["crashed"]
    # a REAL event row for the crash (the fake emitter bypasses the event log)
    eid = append_event("agent_watch", "agent_process_failed", agent_id=t,
                       severity="critical", owner_action_required=True)
    # the process is back and demonstrably alive
    _scan([_agent(t, "/opt/payment-orchestrator", state="working")],
          {t: WORKING_TAIL}, emit, now=1300.0)
    from core.control_plane.api import _c
    conn, own = _c(None)
    try:
        row = conn.execute("SELECT reason FROM agent_alert_invalid WHERE event_id=?",
                           (eid,)).fetchone()
    finally:
        if own:
            conn.close()
    assert row and "alive" in row[0]
    # retired from the default alert view, still auditable
    assert eid not in {a["event_id"] for a in aw.recent_alerts()}
    assert eid in {a["event_id"] for a in aw.recent_alerts(include_invalid=True)}


def test_tool_completion_telemetry_is_never_a_task_finish():
    """Event 5051, live: the bootstrap agent's pane showed the harness notice
    `Background command "Wait for 193 monitor output" completed (exit code 0)`
    while the agent was demonstrably mid-task — and was announced
    task_completed. A shell/monitor/subprocess completing is telemetry, never
    the agent finishing."""
    emit = _Emit()
    t = "owneros-runtime-supervisor-bootstrap:0.0"
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    tool_rest = ('Background command "Wait for 193 monitor output" completed '
                 "(exit code 0)")
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="idle")], {t: tool_rest},
              emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []
    # more shapes from the same family
    for rest in ('Monitor "terminal state of job" stream ended',
                 "process exited with code 0",
                 "Command finished. return code 0"):
        r = _scan([_agent(t, "/root/ai-dev-runtime", state="idle")], {t: rest},
                  emit, now=1200.0)
        assert r["emitted"] == [], rest
    assert emit.calls == []


def test_a_real_stated_finish_still_completes_after_tool_noise():
    """Positive control: the guard must not eat genuine completions."""
    emit = _Emit()
    t = "owneros-runtime-supervisor-bootstrap:0.0"
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="idle")],
              {t: "All checks passed. Final report written to reports/BRIDGE.md."},
              emit, now=1100.0)
    assert [e["class"] for e in r["emitted"]] == ["completed"]


def test_a_fingerprint_migration_does_not_renotify_an_unresolved_blocker():
    """events 4084/4085, live: a classifier deploy changed the digest scheme and every
    unresolved blocker re-announced. Same agent, same class, still unresolved, digest
    moved -> adopt silently."""
    emit = _Emit()
    t = "gaika-ext-audit:0.0"
    _scan([_agent()], {t: BLOCKER_TAIL}, emit)
    assert len(emit.calls) == 1
    drifted = BLOCKER_TAIL + "\nMinor extra status line the redraw added."
    r = _scan([_agent()], {t: drifted}, emit, now=1200.0)
    assert r["emitted"] == [] and len(emit.calls) == 1
    # and the stored fingerprint moved with it, so the next scan is quiet too
    r2 = _scan([_agent()], {t: drifted}, emit, now=1400.0)
    assert r2["skipped"][0]["why"] == "already_notified"


MAINTENANCE_REST = ("All tests passed. Commit c07f42e done. Delivery proof completed. "
                    "3 shells running in background for verification.")


def test_a_suppressed_maintenance_pane_never_emits_while_the_guard_is_active():
    """The watcher-maintenance pane (event 4096): suppressed with an EXPIRY, never a
    permanent blind spot — not observed, and not a crash when it vanishes."""
    emit = _Emit()
    t = "fable-wake-fix:0.0"
    aw.suppress(t, ttl_secs=600, reason="watcher maintenance session", now=1000.0)
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
              {t: PROMPT_TAIL}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []
    assert any(s["why"] == "suppressed_maintenance" for s in r["skipped"])
    r2 = _scan([], {}, emit, now=1200.0)
    assert r2["emitted"] == [] and emit.calls == []


def test_the_same_target_alerts_exactly_once_after_the_guard_expires():
    """v4's regression, inverted: maintenance ends, the pane later presents a REAL owner
    prompt — it must alert, exactly once, and a restart must not duplicate it."""
    emit = _Emit()
    t = "fable-wake-fix:0.0"
    aw.suppress(t, ttl_secs=600, reason="watcher maintenance session", now=1000.0)
    r0 = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
               {t: PROMPT_TAIL}, emit, now=1100.0)
    assert r0["emitted"] == []                              # guard active
    r1 = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
               {t: PROMPT_TAIL}, emit, now=1700.0)          # guard expired
    assert [e["class"] for e in r1["emitted"]] == ["owner_prompt"]
    assert len(emit.calls) == 1
    emit2 = _Emit()                                         # companion restarted
    r2 = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
               {t: PROMPT_TAIL}, emit2, now=1800.0)
    assert r2["emitted"] == [] and emit2.calls == []


def test_suppression_is_per_target_not_per_directory():
    """Another agent in the same repo stays fully observable."""
    emit = _Emit()
    aw.suppress("fable-wake-fix:0.0", ttl_secs=600, reason="maintenance", now=1000.0)
    t = "runtime-helper:0.0"
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
              {t: PROMPT_TAIL}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]


def test_the_reminder_cadence_default_is_one_hour():
    assert aw.REMINDER_SECS == 3600


def test_finish_words_with_background_activity_still_do_not_complete():
    """Defense in depth beyond exclusion: a maintenance-shaped rest screen — finish
    vocabulary plus running background shells — is continuation, not completion."""
    emit = _Emit()
    t = "some-agent:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/mess", state="idle")], {t: MAINTENANCE_REST}, emit,
              now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


CHEMMY_SUBAGENT_REST = """Audit dispatched to three reviewers.
◯ general-purpose 2m 30s · ↓ 122.9k
◯ general-purpose t2m 21s · ↓ 97.4k
◯ general-purpose  2m 14s · ↓ 97.2k"""

PAYORCH_SPINNER_REST = "Report saved to INTEGRITY_AUDIT_2026-08-13.md\n✶ Dilly-dallying…"

GAIKA_BOX_PROMPT = ("Latest blocked action: Blocked by classifier\n"
                    + "╌" * 120 + "\n"
                    "Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again\n"
                    "   3. No")


def test_running_subagent_widget_rows_suppress_completion():
    """chemmy, live (event 4255): three running subagents ARE work in flight — and their
    widget rows are chrome, never a summary."""
    emit = _Emit()
    t = "chemmy-fast:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/mess", state="idle")], {t: CHEMMY_SUBAGENT_REST},
              emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []
    assert "◯" not in aw.excerpt_of(CHEMMY_SUBAGENT_REST)


def test_an_active_spinner_at_the_bottom_suppresses_completion():
    """payorch, live (event 4281): '✶ Dilly-dallying…' is execution in flight, even when
    a finish-sounding report line sits just above it."""
    emit = _Emit()
    t = "payorch-live-buttons:0.0"
    _scan([_agent(t, "/opt/payment-orchestrator", state="working")],
          {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/payment-orchestrator", state="idle")],
              {t: PAYORCH_SPINNER_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_a_stale_spinner_above_a_menu_does_not_suppress_the_prompt():
    """The 4187 shape: an old spinner remnant higher in the pane, a live menu at the
    bottom. The prompt must still alert."""
    tail = "✶ Frolicking…\nsome earlier output line\n" + CHEMMY_MENU_REST
    emit = _Emit()
    r = _scan([_agent(state="waiting_owner")], {"gaika-ext-audit:0.0": tail}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]


def test_dashed_box_lines_never_reach_a_prompt_summary():
    """gaika-video, live (event 4279): the summary was a wall of ╌. The question and
    options must survive; the ruler must not."""
    emit = _Emit()
    r = _scan([_agent("gaika-video:0.0", "/opt/gaika-video", state="waiting_owner")],
              {"gaika-video:0.0": GAIKA_BOX_PROMPT}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]
    ex = emit.calls[0]["payload"]["excerpt"]
    assert "Do you want to proceed" in ex and "╌" not in ex


def test_a_mangled_running_fragment_never_completes():
    """fable, live (event 4300): the pane's last line was the column-wrapped progress
    fragment 're on scr een: runn ing' while finish vocabulary sat in scrollback above.
    Neither half may produce a completion."""
    emit = _Emit()
    t = "fable-wake-fix:0.0"
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    tail = "All tests passed. Commit done earlier.\nre on scr\neen: runn\ning"
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="idle")], {t: tail},
              emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_finish_evidence_must_be_in_the_final_lines_not_the_scrollback():
    emit = _Emit()
    t = "a:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    tail = ("Phase 1 completed successfully.\n"      # old news, higher up
            "line\nline\nline\n"
            "Now examining the remaining edge cases.")
    r = _scan([_agent(t, "/opt/mess", state="idle")], {t: tail}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []
    # and a stated finish in the CLOSING lines still completes
    r2 = _scan([_agent(t, "/opt/mess", state="idle")],
               {t: "Edge cases handled.\nFinal report saved. All done."},
               emit, now=1200.0)
    assert [e["class"] for e in r2["emitted"]] == ["completed"]


GAIKA_TRANSFIGURING_REST = 'закончится. complete" was stopped\n✢ Transfiguring…'
PAYORCH_CONDITIONAL_REST = "I'll proceed. Otherwise the non-gated remediation is complete."


def test_every_spinner_glyph_family_member_means_working():
    """gaika-video, live (event 4456): the spinner wore ✢, one glyph outside the earlier
    class, next to a QUOTED 'complete' and a stopped-shell notice. Working, not done."""
    emit = _Emit()
    t = "gaika-video:0.0"
    _scan([_agent(t, "/opt/gaika-video", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/gaika-video", state="idle")],
              {t: GAIKA_TRANSFIGURING_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_conditional_future_intent_is_not_a_completion():
    """payorch, live (event 4485): "I'll proceed. Otherwise ... is complete." awaits the
    owner's objection — a question wearing a period."""
    emit = _Emit()
    t = "payorch-live-buttons:0.0"
    _scan([_agent(t, "/opt/payment-orchestrator", state="working")],
          {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/payment-orchestrator", state="idle")],
              {t: PAYORCH_CONDITIONAL_REST}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []


def test_genuinely_working_never_alerts():
    emit = _Emit()
    for now in (1000.0, 1020.0, 1040.0):
        r = _scan([_agent(state="working")], {"gaika-ext-audit:0.0": WORKING_TAIL},
                  emit, now=now)
        assert r["emitted"] == []
    assert emit.calls == []


# ── dedup / re-arm / reminder / restart for waiting classes ────────────────
def test_an_unchanged_prompt_is_never_resent():
    emit = _Emit()
    tails = {"gaika-ext-audit:0.0": PROMPT_TAIL}
    _scan([_agent(state="waiting_owner")], tails, emit)
    r2 = _scan([_agent(state="waiting_owner")], tails, emit, now=1020.0)
    assert r2["emitted"] == [] and len(emit.calls) == 1
    assert r2["skipped"][0]["why"] == "already_notified"


def test_spinner_churn_does_not_mint_a_new_fingerprint():
    a = aw.digest_of("Waiting for input (32 seconds elapsed)")
    b = aw.digest_of("Waiting for input (95 seconds elapsed)")
    assert a == b


def test_resume_then_the_same_prompt_again_re_arms():
    emit = _Emit()
    t = "gaika-ext-audit:0.0"
    _scan([_agent(state="waiting_owner")], {t: PROMPT_TAIL}, emit)
    _scan([_agent(state="working")], {t: WORKING_TAIL}, emit, now=1050.0)  # resumed
    r = _scan([_agent(state="waiting_owner")], {t: PROMPT_TAIL}, emit, now=1100.0)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]
    assert len(emit.calls) == 2


def test_an_unresolved_owner_item_gets_one_reminder_after_the_interval(monkeypatch):
    monkeypatch.setattr(aw, "REMINDER_SECS", 600)
    emit = _Emit()
    tails = {"gaika-ext-audit:0.0": PROMPT_TAIL}
    _scan([_agent(state="waiting_owner")], tails, emit, now=1000.0)
    assert _scan([_agent(state="waiting_owner")], tails, emit,
                 now=1300.0)["emitted"] == []
    r = _scan([_agent(state="waiting_owner")], tails, emit, now=1700.0)
    assert len(r["emitted"]) == 1 and len(emit.calls) == 2


def test_restart_does_not_replay_an_already_notified_prompt():
    emit = _Emit()
    tails = {"gaika-ext-audit:0.0": BLOCKER_TAIL}
    _scan([_agent()], tails, emit)
    emit2 = _Emit()                                        # "restarted" companion
    r = _scan([_agent()], tails, emit2, now=1030.0)
    assert r["emitted"] == [] and emit2.calls == []


# ── summaries and routing ──────────────────────────────────────────────────
def test_the_excerpt_is_chrome_free_and_bounded():
    ex = aw.excerpt_of(PROMPT_TAIL)
    assert len(ex) <= 300
    for ch in "╭╮╰╯─│":
        assert ch not in ex, ex


def test_a_menu_excerpt_carries_the_question_and_options_not_the_footer():
    """event 4100's summary was the widget footer. The excerpt must say what is being
    asked: the question line and the real options."""
    tail = CHEMMY_MENU_REST + "\n Enter to select · ↑/↓ to navigate · Esc to cancel"
    ex = aw.excerpt_of(tail, cls="owner_prompt")
    assert "Wait for their signal" in ex
    assert "Stand down entirely" in ex
    assert "What should I do next" in ex
    assert "Enter to select" not in ex and "↑/↓" not in ex
    assert len(ex) <= 300


def test_invalid_marked_alerts_leave_the_default_view_but_stay_auditable():
    """Historical false positives must not confuse a woken assistant reading
    notifications; the audit path keeps them."""
    emit = _Emit()
    _scan([_agent(state="waiting_owner")], {"gaika-ext-audit:0.0": PROMPT_TAIL}, emit)
    # a real event row to retire: write one through the CTO inbox
    from core.control_plane.cto import emit as cto_emit
    ev = cto_emit("agent_watch", "task_completed", project_id="mess",
                  agent_id="x:0.0", severity="info", push=False,
                  action_taken="false completion for the test")
    eid = ev["event_id"]
    assert any(a["event_id"] == eid for a in aw.recent_alerts())
    aw.mark_invalid(eid, reason="proven false: agent was still working")
    assert not any(a["event_id"] == eid for a in aw.recent_alerts())
    hist = [a for a in aw.recent_alerts(include_invalid=True) if a["event_id"] == eid]
    assert hist and hist[0]["invalid"].startswith("proven false")


def test_the_project_comes_from_the_cwd_and_rides_on_the_event():
    emit = _Emit()
    _scan([_agent("gaika-ext-audit:0.0", "/opt/gaika-drop")],
          {"gaika-ext-audit:0.0": BLOCKER_TAIL}, emit)
    assert emit.calls[0]["project_id"] == "gaika-drop"


def test_an_unmapped_cwd_falls_back_to_owner_os_explicitly():
    emit = _Emit()
    _scan([_agent("mystery:0.0", cwd="", state="waiting_owner")],
          {"mystery:0.0": PROMPT_TAIL}, emit)
    call = emit.calls[0]
    assert call["project_id"] == ""
    assert "owner-os" in call["payload"]["project"]


def test_two_agents_route_to_their_own_projects_without_cross_talk():
    emit = _Emit()
    agents = [_agent("a1:0.0", "/opt/gaika-drop"),
              _agent("a2:0.0", "/opt/mess", state="waiting_owner")]
    _scan(agents, {"a1:0.0": BLOCKER_TAIL, "a2:0.0": PROMPT_TAIL}, emit)
    routed = {c["agent_id"]: c["project_id"] for c in emit.calls}
    assert routed == {"a1:0.0": "gaika-drop", "a2:0.0": "mess"}


# ── 2026-08-30: the SILENT STOP. A pane can be structurally idle yet stay `working`
# ── forever because its final prose matches no detector.
#
# Live: a canary finished its step and stopped to ask, wording it "Not proceeding past
# this question." No finish vocabulary, no prompt vocabulary, no menu, no blocker
# phrase — so classify held it `working` (`no_positive_finish_evidence`) and NOTHING was
# ever emitted for a pane the inventory itself called idle. The fix is structural, never
# linguistic: the inventory must already say at-rest AND the bottom region must have been
# unchanged for QUIESCENT_SECS. Loosening the regexes instead would manufacture false
# completions and false prompts fleet-wide, which is the worse failure.

CANARY_SILENT_STOP = """I appended the dated line to the report.
It is inside /root/cp-canary-v2 only and needs no scope change. But it's your call.
Not proceeding past this question."""

LONG_SHELL_TAIL = """[watch] tick 41221 ok
[watch] tick 41222 ok
[watch] tick 41223 ok"""


# (a) the exact phrasing that just failed
def test_a_silent_stop_becomes_quiescent_once_it_is_structurally_at_rest():
    fresh = aw.classify(CANARY_SILENT_STOP, state="idle", prev_cls="working")
    assert fresh["cls"] == "working", "a pane that just went quiet is not yet a stop"

    settled = aw.classify(CANARY_SILENT_STOP, state="idle", prev_cls="working",
                          quiet_secs=aw.QUIESCENT_SECS + 1)
    assert settled["cls"] == "quiescent"
    assert "at_rest_unchanged_for" in settled["reason"]


def test_a_quiescence_never_claims_completion():
    """`completed` asserts the agent SAID it finished. Quietness must never fabricate
    that — the honest verdict is that work stopped without proving it finished."""
    c = aw.classify(CANARY_SILENT_STOP, state="idle", prev_cls="working",
                    quiet_secs=aw.QUIESCENT_SECS * 10)
    assert c["cls"] != "completed"
    assert aw._EVENT_FOR["quiescent"][0] == "work_stopped_incomplete"
    assert aw._EVENT_FOR["quiescent"][0] != "task_completed"


def test_a_dwell_is_required_no_stop_is_declared_on_a_pause_between_turns():
    for q in (0.0, 1.0, aw.QUIESCENT_SECS - 1):
        assert aw.classify(CANARY_SILENT_STOP, state="idle", prev_cls="working",
                           quiet_secs=q)["cls"] == "working"


# (b) a genuine menu is untouched
def test_b_a_real_numbered_menu_is_still_owner_prompt_at_any_dwell():
    for q in (0.0, aw.QUIESCENT_SECS * 5):
        assert aw.classify(PROMPT_TAIL, state="idle", prev_cls="working",
                           quiet_secs=q)["cls"] == "owner_prompt"
        assert aw.classify(CHEMMY_MENU_REST, state="waiting_owner", prev_cls="working",
                           quiet_secs=q)["cls"] == "owner_prompt"


def test_b_a_blocker_phrase_is_still_a_blocker_at_any_dwell():
    for q in (0.0, aw.QUIESCENT_SECS * 5):
        assert aw.classify(BLOCKER_TAIL, state="idle", prev_cls="working",
                           quiet_secs=q)["cls"] == "blocker"


# (c) long-running shells and monitors keep working
def test_c_a_long_running_shell_never_becomes_quiescent():
    """`shell_running` is an ACTIVE inventory state: a monitor whose output happens to be
    unchanged is still doing its job. This is why the rule keys on the inventory's
    structural verdict and not on quietness alone."""
    c = aw.classify(LONG_SHELL_TAIL, state="shell_running", prev_cls="working",
                    quiet_secs=aw.QUIESCENT_SECS * 100)
    assert c["cls"] == "working" and c["reason"] == "inventory_state_shell_running"


def test_c_an_actively_working_pane_never_becomes_quiescent():
    c = aw.classify(WORKING_TAIL, state="working", prev_cls="working",
                    quiet_secs=aw.QUIESCENT_SECS * 100)
    assert c["cls"] == "working"


# (d) the scan emits the right event for a settled idle pane
def test_d_a_settled_idle_pane_emits_work_stopped_incomplete():
    emit = _Emit()
    t = "cp-canary:0.0"
    # first sweep: working, and the digest clock starts
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    # it goes quiet, unchanged, and only LATER does that become a stop
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
          emit, now=1010.0)
    assert not [c for c in emit.calls if c["type"] == "work_stopped_incomplete"], \
        "must not fire while the pane has only just gone quiet"

    r = _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
              emit, now=1010.0 + aw.QUIESCENT_SECS + 5)
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 1, r
    assert stops[0]["severity"] == "high"
    assert stops[0].get("owner_action_required") is False


def test_d_a_stated_finish_still_completes_rather_than_going_quiescent():
    """The completion path is untouched: a pane that SAYS it finished still completes."""
    emit = _Emit()
    t = "gaika-ext-audit:0.0"
    _scan([_agent(t, state="working")], {t: WORKING_TAIL}, emit, now=1000.0)
    _scan([_agent(t, state="idle")], {t: IDLE_TAIL}, emit,
          now=1000.0 + aw.QUIESCENT_SECS * 3)
    assert [c["type"] for c in emit.calls] == ["task_completed"]


# (e) dedupe holds — a stop does not become a wake loop
def test_e_a_settled_stop_emits_once_not_every_sweep():
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    base = 1010.0
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
          emit, now=base)
    for i in range(1, 8):
        _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
              emit, now=base + aw.QUIESCENT_SECS + 5 + i * 20)
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 1, f"one stop, not one per sweep: {len(stops)}"


def test_e_resuming_work_rearms_so_a_later_stop_is_a_fresh_event():
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
          emit, now=1010.0)
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
          emit, now=1010.0 + aw.QUIESCENT_SECS + 5)
    # back to work, then it stops again later with different closing text
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=2000.0)
    again = CANARY_SILENT_STOP + "\nA second question, same shape."
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: again}, emit, now=2010.0)
    _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: again}, emit,
          now=2010.0 + aw.QUIESCENT_SECS + 5)
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 2, "a genuinely new stop after real work is a new event"


_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def test_e_the_digest_clock_restarts_when_the_pane_text_changes():
    """Changing output must reset the dwell, or a slowly-producing pane would be called
    stopped while it is plainly still emitting."""
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    for i, w in enumerate(_WORDS):
        tail = CANARY_SILENT_STOP + f"\nstill emitting {w}"
        _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: tail}, emit,
              now=1010.0 + i * (aw.QUIESCENT_SECS - 1))
    assert not [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]


def test_e_digit_only_churn_is_deliberately_NOT_a_change():
    """Pins an interaction that is easy to misread as a bug.

    `digest_of` strips volatile digits on purpose — spinners and token counters must not
    each look like a new event. A consequence is that a pane whose ONLY change is a
    ticking number reads as unchanged, so the dwell keeps accumulating and a settled
    stop is still reported. That is correct here: a pane genuinely producing work is
    `working` or `shell_running` in the inventory and never reaches this rule at all;
    reaching it means the inventory already called the pane at rest, and a bare counter
    moving on an at-rest pane is cosmetic. Pinned so the behaviour is a decision rather
    than an accident."""
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    for i in range(6):
        tail = CANARY_SILENT_STOP + f"\nstill emitting line {i}"
        _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: tail}, emit,
              now=1010.0 + i * (aw.QUIESCENT_SECS - 1))
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 1


def test_e_the_dwell_accumulates_from_the_first_quiet_sweep_not_the_previous_one():
    """`digest_since` must SURVIVE unchanged sweeps.

    Isolates a mistake that ordinary fixtures miss: if the stamp were rewritten every
    sweep (as `ts` is), the measured quiet time would only ever be the gap between two
    consecutive sweeps. With a 20s companion poll that is always far below the
    threshold, so a pane could sit stopped forever and never once qualify.
    """
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    step = aw.QUIESCENT_SECS / 3.0            # each gap alone is far below the threshold
    for i in range(1, 6):
        _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: CANARY_SILENT_STOP},
              emit, now=1000.0 + i * step)
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 1, "accumulated quiet time must cross the threshold"


def test_e_changing_text_resets_the_dwell_even_across_long_gaps():
    """Isolates the reset itself: with gaps LONGER than the threshold, a pane whose text
    changes every sweep must still never be called stopped."""
    emit = _Emit()
    t = "cp-canary:0.0"
    _scan([_agent(t, "/root/cp-canary-v2", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    for i, w in enumerate(_WORDS):
        tail = CANARY_SILENT_STOP + f"\nstill emitting {w}"
        _scan([_agent(t, "/root/cp-canary-v2", state="idle")], {t: tail}, emit,
              now=1000.0 + (i + 1) * (aw.QUIESCENT_SECS + 60))
    assert not [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]


def test_c_an_unrecognised_inventory_state_is_never_called_stopped():
    """Forward-compatibility guard on the structural gate.

    `_QUIESCENT_STATES` is an ALLOWLIST, not the complement of the active states. A state
    this classifier has never heard of — a future 'compacting', 'rewinding', 'queued' —
    is not evidence of rest, and quiet time must not turn it into one.
    """
    c = aw.classify(CANARY_SILENT_STOP, state="compacting", prev_cls="working",
                    quiet_secs=aw.QUIESCENT_SECS * 100)
    assert c["cls"] == "working"


# ── 2026-08-30: a parked agent must not be re-announced ─────────────────────
# diamond-auction:0.0 finished its stage and parked on a read-only watch, saying
# "Remaining items are the unchanged external owner gates. Idle on the watch." Its bottom
# region was byte-identical for over two hours, yet the inventory flickered to `working`
# (a background shell / the "· N shell" footer), that cleared the notification, and the
# same unchanged pane emitted a second work_stopped_incomplete 70 minutes later.

AUCTION_PARKED = """No further non-gated verification is warranted (path
fully validated across staging 7Q-7U and live 7V).
Remaining items are the unchanged external owner gates.
Idle on the watch."""


def test_an_inventory_flicker_does_not_re_announce_an_unchanged_pane():
    """The exact Auction shape: stop, flicker to working with NO new output, stop again."""
    emit = _Emit()
    t = "diamond-auction:0.0"
    _scan([_agent(t, "/opt/diamond/auction", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: AUCTION_PARKED},
          emit, now=1010.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: AUCTION_PARKED},
          emit, now=1010.0 + aw.QUIESCENT_SECS + 5)
    first = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(first) == 1, "the stop itself is real and is said once"

    # the inventory flickers to working while the pane text does NOT move
    for i in range(3):
        _scan([_agent(t, "/opt/diamond/auction", state="shell_running")],
              {t: AUCTION_PARKED}, emit, now=2000.0 + i * 30)
        _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: AUCTION_PARKED},
              emit, now=2000.0 + i * 30 + aw.QUIESCENT_SECS + 5)
    again = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(again) == 1, f"an unchanged pane must not be re-announced: {len(again)}"


def test_real_progress_still_re_arms_so_a_later_stop_is_a_fresh_event():
    """No regression: when the agent actually produces new output, the next stop is a
    genuinely new event and must be announced."""
    emit = _Emit()
    t = "diamond-auction:0.0"
    _scan([_agent(t, "/opt/diamond/auction", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: AUCTION_PARKED},
          emit, now=1010.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: AUCTION_PARKED},
          emit, now=1010.0 + aw.QUIESCENT_SECS + 5)
    # it genuinely works again — the pane text MOVES — then stops on something new
    # Deliberately NOT finish vocabulary: "finished" would classify `completed` and emit
    # task_completed, which is a different (and also correct) path. This test is about the
    # re-arm, so the second stop must be the same class as the first.
    moved = AUCTION_PARKED + "\nPicked the next gate up; still on the same watch."
    _scan([_agent(t, "/opt/diamond/auction", state="working")], {t: moved}, emit, now=3000.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: moved}, emit, now=3010.0)
    _scan([_agent(t, "/opt/diamond/auction", state="idle")], {t: moved},
          emit, now=3010.0 + aw.QUIESCENT_SECS + 5)
    stops = [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]
    assert len(stops) == 2, "real progress then a new stop IS a new event"


def test_a_new_question_on_a_parked_pane_still_wakes():
    """Suppressing the repeat must not suppress a genuinely new prompt: a different
    digest is a different fact, whatever the class."""
    emit = _Emit()
    t = "diamond-auction:0.0"
    _scan([_agent(t, "/opt/diamond/auction", state="working")], {t: WORKING_TAIL},
          emit, now=1000.0)
    _scan([_agent(t, "/opt/diamond/auction", state="waiting_owner")], {t: PROMPT_TAIL},
          emit, now=1100.0)
    asked = [c for c in emit.calls if c["type"] == "agent_prompt_needs_response"]
    assert len(asked) == 1, "a real question still wakes"


# ── a numbered SENTENCE is not a decision menu (event 15817) ──────────────────────────
# `_MENU_RE` searched space-joined text for "1. ... 2. ..." within 300 chars, so an agent
# writing an ordinary numbered summary was classified owner_prompt and woke the owner at
# severity high. 15817 was this supervisor's own turn summary; agent_status showed
# pending=None throughout, so there was never a prompt to answer.

_PROSE_15817 = (
    "Three premises corrected by measurement (now in report): 1. Refusal is "
    "no_open_work:no_active_task, not allowed-roots - allowed-roots belongs to "
    "agent_resume, different API. 2. PRE_CLEAR_MANIFEST.md does not exist.\n"
)

_REAL_MENU = "Do you want to proceed?\n\u276f 1. Yes\n  2. No, keep the gate closed\n"
_REAL_MENU_NO_YESNO = (
    "Which strategy should I use?\n"
    "  1. Rebuild the index from scratch\n"
    "  2. Patch the existing rows\n"
    "  3. Leave it and report\n"
)


def test_numbered_prose_is_not_a_menu():
    assert aw._MENU_RE.search(aw._bottom_lines_text(_PROSE_15817)) is None


def test_real_menu_still_matches():
    assert aw._MENU_RE.search(aw._bottom_lines_text(_REAL_MENU)) is not None


def test_option_menu_without_yes_no_vocabulary_still_matches():
    """Event 4088's shape: a real choice menu with none of the yes/no words."""
    assert aw._MENU_RE.search(aw._bottom_lines_text(_REAL_MENU_NO_YESNO)) is not None


def test_menu_read_from_line_preserving_view():
    """Space-joining the region is what destroyed the line anchor in the first place."""
    assert aw._MENU_RE.search(aw._bottom_region(_REAL_MENU)) is None
    assert aw._MENU_RE.search(aw._bottom_lines_text(_REAL_MENU)) is not None
