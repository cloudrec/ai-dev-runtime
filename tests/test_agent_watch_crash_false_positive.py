"""Harness text is not agent output, and `killed` is not always a crash.

Event 10002 declared `owner-os-opus-windows:0.0` CRASHED (critical, owner action
required) while that pane was demonstrably alive — the very next event for it was
`agent_waiting_input`. The stored evidence was a Claude Code background-task
notice that had scrolled through the pane:

    "__orphan_summary" are internal scan markers, not tasks.
    They may have been stopped (via the UI, Monitor timeout, or agent teardown)

`_CRASH_RE` matched the bare word `killed` in that harness prose. This is the same
contamination the module already guards against for chrome — its own comment
records the watcher's maintenance pane being flagged blocked "because its
scrollback QUOTED a blocker sentence".
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw


NOTICE = """<task-notification>
<task-id>bpegdjkiz</task-id>
<status>stopped</status>
<summary>3 background shell command task(s) from the previous session have no
completion record. They may have been stopped (via the UI, Monitor timeout, or
agent teardown), or killed when the previous process exited. Task ids beginning
with "__orphan_summary" are internal scan markers, not tasks.</summary>
</task-notification>"""

REMINDER = """<system-reminder>
This is an automated background-task event, NOT USER INPUT.
The process may have been killed; a core dumped message could appear here.
</system-reminder>"""


# ── the exact false positive ───────────────────────────────────────────────

def test_the_notice_that_caused_event_10002_is_not_crash_evidence():
    assert aw._CRASH_RE.search(aw._bottom_region(NOTICE)) is None


def test_a_harness_block_contributes_no_lines_at_all():
    """Whole-block stripping: the sentence that matched sat mid-block with no
    marker of its own, so line-level filtering would not have caught it."""
    assert aw._meaningful_lines(NOTICE) == []
    assert aw._meaningful_lines(REMINDER) == []


def test_a_live_pane_carrying_a_notice_classifies_as_working_not_crashed():
    out = aw.classify(alive=True, is_agent=True, state="working",
                      tail=NOTICE + "\nreal agent output line\n")
    assert out["cls"] != "crashed"


def test_real_agent_output_around_a_harness_block_survives():
    tail = "agent line before\n" + NOTICE + "\nagent line after\n"
    kept = aw._meaningful_lines(tail)
    assert "agent line before" in kept and "agent line after" in kept
    assert not any("orphan_summary" in k for k in kept)


def test_an_unclosed_harness_block_does_not_swallow_the_rest_forever():
    """A truncated capture must not blind the watcher to everything below it."""
    kept = aw._meaningful_lines("<system-reminder>\nnoise\n</system-reminder>\nreal output")
    assert kept == ["real output"]


# ── `killed` needs process context ─────────────────────────────────────────

@pytest.mark.parametrize("prose", [
    "the task was killed earlier today",
    "they may have been stopped or killed when the process exited",
    "I killed the idea of rewriting the parser",
    "tasks killed by the user are recorded",
])
def test_ordinary_prose_containing_killed_is_not_a_crash(prose):
    assert aw._CRASH_RE.search(prose) is None


@pytest.mark.parametrize("real", [
    "Traceback (most recent call last):",
    "Segmentation fault (core dumped)",
    "Killed by signal 9",
    "Killed",
    "out of memory: killed process 1234",
    "oom-killer invoked",
    "process killed unexpectedly",
    "claude exited with status 1",
])
def test_genuine_crash_signatures_still_detected(real):
    assert aw._CRASH_RE.search(real) is not None


def test_a_genuine_crash_still_classifies_as_crashed():
    out = aw.classify(alive=True, is_agent=True, state="idle",
                      tail="Traceback (most recent call last):\n  File x\nRuntimeError\n")
    assert out["cls"] == "crashed"
    assert out["reason"] == "crash_text"


def test_a_dead_process_is_still_crashed_regardless_of_text():
    """The structural signal is untouched: not alive means crashed."""
    out = aw.classify(alive=False, is_agent=True, state="", tail="all fine here")
    assert out["cls"] == "crashed"
    assert out["reason"] == "process_gone"
