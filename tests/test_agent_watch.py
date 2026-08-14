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


def test_a_vanished_working_pane_is_a_critical_crash():
    emit = _Emit()
    _scan([_agent(state="working")], {"gaika-ext-audit:0.0": WORKING_TAIL}, emit)
    r = _scan([], {}, emit, now=1100.0)          # pane gone from inventory
    assert [e["class"] for e in r["emitted"]] == ["crashed"]
    assert emit.calls[0]["type"] == "agent_process_failed"
    assert emit.calls[0]["severity"] == "critical"


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


def test_an_excluded_target_never_emits(monkeypatch):
    """The watcher-maintenance pane (event 4096): explicitly excluded, narrow and
    auditable — never observed, whatever its screen says."""
    monkeypatch.setenv("AGENT_WATCH_EXCLUDE_TARGETS", "fable-wake-fix:0.0")
    emit = _Emit()
    t = "fable-wake-fix:0.0"
    _scan([_agent(t, "/root/ai-dev-runtime", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
              {t: PROMPT_TAIL}, emit, now=1100.0)
    assert r["emitted"] == [] and emit.calls == []
    assert any(s["why"] == "excluded_target" for s in r["skipped"])
    # nor does its disappearance count as a crash
    r2 = _scan([], {}, emit, now=1200.0)
    assert r2["emitted"] == [] and emit.calls == []


def test_exclusion_is_per_target_not_per_directory(monkeypatch):
    """Another agent in the same repo stays fully observable."""
    monkeypatch.setenv("AGENT_WATCH_EXCLUDE_TARGETS", "fable-wake-fix:0.0")
    emit = _Emit()
    t = "runtime-helper:0.0"
    r = _scan([_agent(t, "/root/ai-dev-runtime", state="waiting_owner")],
              {t: PROMPT_TAIL}, emit)
    assert [e["class"] for e in r["emitted"]] == ["owner_prompt"]


def test_finish_words_with_background_activity_still_do_not_complete():
    """Defense in depth beyond exclusion: a maintenance-shaped rest screen — finish
    vocabulary plus running background shells — is continuation, not completion."""
    emit = _Emit()
    t = "some-agent:0.0"
    _scan([_agent(t, "/opt/mess", state="working")], {t: WORKING_TAIL}, emit)
    r = _scan([_agent(t, "/opt/mess", state="idle")], {t: MAINTENANCE_REST}, emit,
              now=1100.0)
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
