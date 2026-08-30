"""Work evidence — the MESS 2026-08-06 blind spot.

Reproduces the real scenario before asserting the fix: the agent shipped goal 2, wrote a
report declaring goal 1 audited but `IMPLEMENTATION NOT STARTED`, committed, and went idle
— while the stage pointer never moved. Every existing observer stayed silent. These tests
fail against the old behaviour (no observer at all) and pass only when the report, the
partial completion and the abandoned goal each reach the CTO inbox exactly once.
"""
from __future__ import annotations

import subprocess

import pytest

from core import work_evidence as we


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


MESS_REPORT = """# MESS — responsive navigation + auto-update programme

**Date:** 2026-08-06 · **Branch:** `fable-0.1.91-realdevice-ux`

## Part 1 — Responsive navigation / menu (GOAL 2 + owner addendum) — **DONE**

One component renders one list of actions in two presentations.

## Part 2 — Auto-update (GOAL 1) — **AUDIT COMPLETE / IMPLEMENTATION NOT STARTED**

The updater was audited end to end. The implementation was NOT STARTED.
"""

PLAIN_REPORT = """# Weekly inventory

Nothing conclusive yet; numbers below are raw counts.
"""


def _project(tmp_path, report_text=MESS_REPORT, name="MESS_AUTO_UPDATE_2026-08-06.md"):
    root = tmp_path / "mess"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / name).write_text(report_text)
    return {"mess-qa-automation:0.0": {"cwd": str(root), "project": "mess"}}


class _Emitter:
    """Captures what would reach the CTO inbox."""

    def __init__(self):
        self.events = []

    def __call__(self, source, type, **kw):
        self.events.append({"source": source, "type": type, **kw})
        return {"event_id": len(self.events), "pushed": bool(kw.get("owner_action_required"))}

    def types(self):
        return [e["type"] for e in self.events]


# ── classification ─────────────────────────────────────────────────────────
def test_a_report_claiming_done_and_not_started_is_a_partial_completion():
    cls = we.classify_report(MESS_REPORT)
    assert cls["done"] is True and cls["not_started"] is True
    assert cls["audit_only"] is True and cls["partial"] is True


def test_a_report_with_no_completion_markers_is_not_partial():
    cls = we.classify_report(PLAIN_REPORT)
    assert cls["partial"] is False and cls["done"] is False


# ── THE regression ─────────────────────────────────────────────────────────
def test_the_mess_scenario_raises_partial_completion_and_stopped_work(tmp_path):
    """Stage pointer unchanged, agent idle, report says half the work never started."""
    emit = _Emitter()
    out = we.scan(_project(tmp_path), emit_fn=emit, state_fn=lambda t: "idle")

    assert we.EVENT_PARTIAL in emit.types(), "a partial completion must be owner-visible"
    assert we.EVENT_STOPPED in emit.types(), "an agent stopping mid-task must be owner-visible"
    assert out["emitted_count"] >= 2

    partial = next(e for e in emit.events if e["type"] == we.EVENT_PARTIAL)
    assert partial["severity"] == "high" and partial["owner_action_required"] is True
    assert partial["payload"]["stage_pointer_moved"] is False       # the whole point
    assert partial["payload"]["markers"]["not_started"] is True
    assert "MESS" in partial["payload"]["headline"]

    stopped = next(e for e in emit.events if e["type"] == we.EVENT_STOPPED)
    assert stopped["owner_action_required"] is True
    assert "not started" in stopped["payload"]["reason"]
    assert stopped["payload"]["agent_state"] == "idle"


def test_the_same_report_is_announced_once_not_every_tick(tmp_path):
    projects = _project(tmp_path)
    emit = _Emitter()
    we.scan(projects, emit_fn=emit, state_fn=lambda t: "idle")
    first = len(emit.events)
    assert first >= 2
    for _ in range(3):
        we.scan(projects, emit_fn=emit, state_fn=lambda t: "idle")
    assert len(emit.events) == first, "a re-read of the same bytes is not news"


def test_a_materially_changed_report_is_news_again(tmp_path):
    projects = _project(tmp_path)
    emit = _Emitter()
    we.scan(projects, emit_fn=emit, state_fn=lambda t: "idle")
    before = len(emit.events)
    root = list(projects.values())[0]["cwd"]
    import pathlib
    p = pathlib.Path(root) / "reports" / "MESS_AUTO_UPDATE_2026-08-06.md"
    p.write_text(MESS_REPORT + "\n## Part 2 update — implementation DONE\n")
    we.scan(projects, emit_fn=emit, state_fn=lambda t: "idle")
    assert len(emit.events) > before


def test_a_working_agent_with_a_plain_report_raises_the_report_only(tmp_path):
    """Not a file watcher and not an alarm: a new report from a working agent is news
    once, at info severity, with no owner action demanded."""
    emit = _Emitter()
    we.scan(_project(tmp_path, PLAIN_REPORT, "INVENTORY.md"), emit_fn=emit,
            state_fn=lambda t: "working")
    assert emit.types() == [we.EVENT_REPORT]
    assert emit.events[0]["severity"] == "info"
    assert emit.events[0]["owner_action_required"] is False


def test_an_idle_agent_with_an_open_ledger_task_is_reported_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(we, "_open_task", lambda target, conn=None: {"id": "task-77"})
    emit = _Emitter()
    we.scan(_project(tmp_path, PLAIN_REPORT, "STATUS.md"), emit_fn=emit,
            state_fn=lambda t: "idle")
    assert we.EVENT_STOPPED in emit.types()
    stopped = next(e for e in emit.events if e["type"] == we.EVENT_STOPPED)
    assert stopped["payload"]["open_task"] == "task-77"
    assert "ledger task is still open" in stopped["payload"]["reason"]


# ── commits without a stage-pointer change ─────────────────────────────────
def _git(root, *args):
    subprocess.run(["git", "-C", str(root)] + list(args), check=True, capture_output=True, text=True)


def test_new_commits_are_reported_even_when_no_pointer_moved(tmp_path):
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "R.md").write_text(PLAIN_REPORT)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first")
    projects = {"a:0.0": {"cwd": str(root), "project": "proj"}}

    emit = _Emitter()
    we.scan(projects, emit_fn=emit, state_fn=lambda t: "working")   # records the head cursor
    (root / "feature.txt").write_text("x\n")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ship the feature")

    emit2 = _Emitter()
    we.scan(projects, emit_fn=emit2, state_fn=lambda t: "working")
    assert we.EVENT_COMMITS in emit2.types()
    ev = next(e for e in emit2.events if e["type"] == we.EVENT_COMMITS)
    assert any("ship the feature" in c for c in ev["payload"]["commits"])
    assert ev["payload"]["stage_pointer_moved"] is False

    emit3 = _Emitter()
    we.scan(projects, emit_fn=emit3, state_fn=lambda t: "working")
    assert we.EVENT_COMMITS not in emit3.types(), "the same commits are not re-announced"


# ── activation must not replay history ─────────────────────────────────────
def test_first_sight_of_a_project_does_not_announce_its_back_catalogue(tmp_path):
    """Live check that forced this: the first pass over /opt/mess emitted 43 events for
    reports that were weeks old. History is recorded as seen, never announced."""
    import os
    import time
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    old = time.time() - 40 * 86400
    for i in range(12):
        p = root / "reports" / f"OLD_{i}.md"
        p.write_text(MESS_REPORT)
        os.utime(p, (old, old))
    (root / "reports" / "TODAY.md").write_text(MESS_REPORT)

    emit = _Emitter()
    out = we.scan({"a:0.0": {"cwd": str(root), "project": "proj"}}, emit_fn=emit,
                  state_fn=lambda t: "idle")
    assert out["backfilled"] == 12
    assert emit.events, "today's report is still news"
    assert all(e["payload"]["report"].endswith("TODAY.md") for e in emit.events)


def test_a_busy_first_scan_is_capped_and_reports_what_it_suppressed(tmp_path, monkeypatch):
    monkeypatch.setattr(we, "COLD_START_MAX_REPORTS", 2)
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    for i in range(6):
        (root / "reports" / f"R{i}.md").write_text(MESS_REPORT)   # all recent
    emit = _Emitter()
    out = we.scan({"a:0.0": {"cwd": str(root), "project": "proj"}}, emit_fn=emit,
                  state_fn=lambda t: "idle")
    assert out["backfilled"] == 4                      # suppression is counted, not silent
    assert len({e["payload"]["report"] for e in emit.events}) == 2


def test_the_newest_report_is_read_first_so_a_cap_cannot_hide_it(tmp_path, monkeypatch):
    """The cap used to apply to an alphabetical listing, which dropped today's report."""
    import os
    import time
    monkeypatch.setattr(we, "MAX_REPORTS_PER_SCAN", 2)
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    old = time.time() - 3600
    for name in ("AAA.md", "BBB.md", "CCC.md"):
        p = root / "reports" / name
        p.write_text(PLAIN_REPORT)
        os.utime(p, (old, old))
    newest = root / "reports" / "ZZZ_TODAY.md"          # alphabetically last, newest
    newest.write_text(MESS_REPORT)

    emit = _Emitter()
    out = we.scan({"a:0.0": {"cwd": str(root), "project": "proj"}}, emit_fn=emit,
                  state_fn=lambda t: "idle")
    assert any("ZZZ_TODAY.md" in e["payload"]["report"] for e in emit.events)
    assert out["skipped"] and out["skipped"][0]["not_read"] == 2


def test_one_scan_wakes_the_owner_at_most_once(tmp_path):
    """Several findings in one sweep all reach the inbox; only the first may push."""
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    for name in ("A.md", "B.md", "C.md"):
        (root / "reports" / name).write_text(MESS_REPORT)
    emit = _Emitter()
    we.scan({"a:0.0": {"cwd": str(root), "project": "proj"}}, emit_fn=emit,
            state_fn=lambda t: "idle")
    owner_events = [e for e in emit.events if e.get("owner_action_required")]
    assert len(owner_events) >= 2, "multiple findings still reach the inbox"
    pushes = [e for e in owner_events if e.get("push") is None]
    assert len(pushes) == 1, "exactly one of them may wake the owner"
    assert all(e.get("push") is False for e in owner_events[1:])


def test_the_real_wake_path_enqueues_a_single_notification(tmp_path):
    from core.control_plane.api import _c
    root = tmp_path / "proj"
    (root / "reports").mkdir(parents=True)
    for name in ("A.md", "B.md", "C.md"):
        (root / "reports" / name).write_text(MESS_REPORT)
    we.scan({"a:0.0": {"cwd": str(root), "project": "proj"}}, state_fn=lambda t: "idle")
    conn, own = _c(None)
    try:
        n = conn.execute("SELECT count(*) FROM notification").fetchone()[0]
        ev = conn.execute("SELECT count(*) FROM event WHERE source='work_evidence'").fetchone()[0]
    finally:
        if own:
            conn.close()
    assert ev >= 2 and n == 1, f"{ev} inbox events but {n} wakes"


# ── fail-closed ────────────────────────────────────────────────────────────
def test_an_unreadable_project_is_recorded_not_silently_treated_as_quiet(tmp_path):
    emit = _Emitter()
    out = we.scan({"x:0.0": {"cwd": str(tmp_path / "missing"), "project": "x"}},
                  emit_fn=emit, state_fn=lambda t: "idle")
    assert out["emitted"] == []
    assert out["skipped"] and "unreadable" in out["skipped"][0]["reason"]


def test_scan_never_writes_into_the_observed_project(tmp_path):
    projects = _project(tmp_path)
    root = list(projects.values())[0]["cwd"]
    import os
    before = {p: os.stat(os.path.join(dp, p)).st_mtime
              for dp, _, fs in os.walk(root) for p in fs}
    we.scan(projects, emit_fn=_Emitter(), state_fn=lambda t: "idle")
    after = {p: os.stat(os.path.join(dp, p)).st_mtime
             for dp, _, fs in os.walk(root) for p in fs}
    assert before == after, "the observer must not modify the project it observes"


# ── real inbox wiring ──────────────────────────────────────────────────────
def test_events_reach_the_cto_inbox_with_owner_action_for_partial_work(tmp_path):
    """No stub: the default emit path records durable CTO events."""
    from core.control_plane import cto
    we.scan(_project(tmp_path), state_fn=lambda t: "idle")
    events = cto.cto_brief_since("t")["events"]
    types = [e["type"] for e in events]
    assert we.EVENT_PARTIAL in types and we.EVENT_STOPPED in types
    partial = next(e for e in events if e["type"] == we.EVENT_PARTIAL)
    assert partial["severity"] == "high" and partial["owner_action_required"] in (1, True)


# ── completion scope: prose markers must describe CURRENT state, not history ──────────
# Regression for Owner OS event 15754. `classify_report` regexed the WHOLE document, so an
# append-only log that NARRATES "DONE", "NOT STARTED" and "BLOCKED" across a thousand
# historical notes reported all three as live claims — permanently, because an append-only
# file can never stop matching. That was 51% of a week's work_stopped_incomplete events.

_FILLER = ("Ordinary narrative describing the day's work in detail. " * 40 + "\n")


def _long(head: str = "", tail: str = "") -> str:
    body = _FILLER * int((we.LONG_REPORT_BYTES * 2) / len(_FILLER) + 2)
    return f"# Handoff\n\n{head}\n{body}\n{tail}\n"


def test_short_report_is_still_read_whole():
    """The ordinary case must not change at all."""
    text = "# R\n\nBLOCKED ON the provider.\n"
    assert len(text) <= we.LONG_REPORT_BYTES
    cls = we.classify_report(text)
    assert cls["scope_basis"] == "whole_report"
    assert cls["blocked"] is True and cls["incomplete"] is True


def test_history_in_a_long_log_is_not_a_current_claim():
    """The ARBITRAGE2/CANARY shape: markers buried in history, clean tail."""
    text = _long(head="Earlier we were BLOCKED ON review and it was NOT STARTED.",
                 tail="Everything since has proceeded normally.")
    assert len(text) > we.LONG_REPORT_BYTES
    cls = we.classify_report(text)
    assert cls["scope_basis"] == "tail"
    assert cls["blocked"] is False
    assert cls["not_started"] is False
    assert cls["incomplete"] is False


def test_current_claim_in_a_long_log_is_still_seen():
    """Narrowing the scope must not blind the classifier to a real, present blocker."""
    cls = we.classify_report(_long(tail="Work halted: BLOCKED ON the owner decision."))
    assert cls["blocked"] is True and cls["incomplete"] is True


def test_structured_status_declaration_beats_position():
    """A declared status is authoritative wherever it sits — structured over prose."""
    cls = we.classify_report(_long(head="Status: BLOCKED ON the provider",
                                   tail="Narrative continues quietly."))
    assert cls["scope_basis"] == "status_declarations+tail"
    assert cls["blocked"] is True and cls["incomplete"] is True


def test_scope_never_starts_mid_line():
    scope, basis = we.completion_scope(_long(tail="end"))
    assert basis == "tail"
    assert not scope.startswith("Ordinary narrative describing the day") or scope.count("\n")
    assert len(scope) < we.LONG_REPORT_BYTES
