"""A finished, parked agent must not be poked as `quiescent` — event 57111 (2026-09-22).

main105-backup-repair reported "Завершено и верифицировано (последний успех 22 Sep
03:06, следующий прогон 23 Sep 02:00). Припаркован." and was then woken four times in an
hour with "continue with the next safe step", answering "Припаркован." each time. Three
things in the pane reader combined to produce that loop:

  * `_UI_LINE_RE` stripped EVERY `●` line as a subagent-widget row, and `●` is also the
    CLI's marker for the agent's own prose — its statements vanished;
  * the composer echo "❯ continue with the next safe" / "  step" lost only its first
    line, so the wrapped "step" survived as the agent's "final line";
  * "parked" was not finish vocabulary, so even the surviving text proved nothing.

These pin all three, and the control cases that keep them narrow.
"""
import sys

sys.path.insert(0, "/root/ai-dev-runtime")

from core import agent_watch as aw  # noqa: E402

# Live pane shape, 2026-09-22 07:55 local (ANSI stripped). Two-space wrapping is the CLI's.
MAIN105_PARKED = """  Блокеров нет; owner-side пункты
  прежние. Припаркован до 23 Sep
  02:00 — сбой оповестит OnFailure
  notifier.
✻ Churned for 52s · done 6:49 AM
❯ continue with the next safe
  step
● Завершено и верифицировано
  (последний успех 22 Sep 03:06,
  следующий прогон 23 Sep 02:00).
  Припаркован.
✻ Sautéed for 5s · done 6:51 AM
❯ continue with the next safe
  step
● Припаркован.
✻ Crunched for 5s · done 7:52 AM
──────────────────────────────────
❯ 
──────────────────────────────────
  [CAVEMAN]
  ⏵⏵ auto mode on (shift+tab to
  ⧉  backup-incident
"""


def _lines(tail):
    return aw._meaningful_lines(tail)


# ── the agent's own words are evidence ──────────────────────────────────────

def test_agent_prose_marked_with_a_bullet_is_kept():
    lines = _lines(MAIN105_PARKED)
    assert "● Завершено и верифицировано" in lines
    assert "● Припаркован." in lines


def test_widget_rows_and_tool_call_headers_are_still_chrome():
    tail = ("◯ general-purpose 2m 30s · ↓ 122.9k\n"
            "● general-purpose 2m 14s · ↓ 97.2k\n"
            "◉ reviewer 1m 02s · ↓ 12.0k\n"
            "● Bash(git commit -m 'complete the migration')\n"
            "  ⎿  [main abc123] complete the migration\n"
            "● Read(reports/final report.md)\n"
            "● Update-Config(path: x)\n"
            "● All checks pass; parked until the 02:00 run.\n")
    lines = _lines(tail)
    assert lines == ["⎿  [main abc123] complete the migration",
                     "● All checks pass; parked until the 02:00 run."]


def test_a_tool_call_header_wearing_finish_vocabulary_is_not_a_finish():
    """Control: keeping `●` prose must not let `● Bash(… complete …)` become a finish."""
    tail = "● Bash(git commit -m 'complete the migration')\n"
    assert _lines(tail) == []


# ── the supervisor's typed instruction is not agent output ──────────────────

def test_the_wrapped_composer_echo_is_dropped_whole():
    lines = _lines(MAIN105_PARKED)
    assert "step" not in lines
    assert not any(ln.startswith("❯") for ln in lines)


def test_echo_ends_at_the_first_unindented_or_blank_line():
    tail = ("❯ continue with the next safe\n  step\n  and then stop\n"
            "● Done.\n\n❯ another typed line\n\n  an indented agent line after a blank\n")
    assert _lines(tail) == ["● Done.", "an indented agent line after a blank"]


def test_a_selected_menu_option_is_still_part_of_the_question():
    """`❯ 1. Yes` is the CLI's cursor on a menu, not the composer — untouched."""
    tail = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    lines = _lines(tail)
    assert "❯ 1. Yes" in lines and "2. No" in lines


# ── a stated park is a finish ───────────────────────────────────────────────

def test_a_parked_agent_classifies_completed_not_quiescent():
    r = aw.classify(MAIN105_PARKED, state="idle", prev_cls="working", quiet_secs=400)
    assert r == {"cls": "completed", "reason": "stated_finish_at_rest"}


def test_a_repeated_parked_reply_stays_completed():
    tail = MAIN105_PARKED.replace("──────────────────────────────────\n❯ \n",
                                  "❯ continue with the next safe\n  step\n● Припаркован.\n"
                                  "✻ Churned for 5s · done 7:55 AM\n"
                                  "──────────────────────────────────\n❯ \n", 1)
    r = aw.classify(tail, state="idle", prev_cls="working", quiet_secs=400)
    assert r["cls"] == "completed"


def test_parked_in_english_is_a_finish_too():
    tail = "● Backup verified. Parked until the 02:00 run tomorrow.\n"
    r = aw.classify(tail, state="idle", prev_cls="working", quiet_secs=400)
    assert r["cls"] == "completed"


def test_new_failure_evidence_still_outranks_a_park():
    """The park is a finish only while nothing contradicts it: a crash report below it
    is a crash, and open work below it is continuation."""
    crashed = MAIN105_PARKED + "Traceback (most recent call last):\n  MemoryError\n"
    assert aw.classify(crashed, state="idle", prev_cls="working",
                       quiet_secs=400)["cls"] == "crashed"
    reopened = MAIN105_PARKED.replace("● Припаркован.\n",
                                      "● Припаркован.\n  ☐ re-run the failed verification\n", 1)
    assert aw.classify(reopened, state="idle", prev_cls="working",
                       quiet_secs=400)["cls"] != "completed"


def test_quiet_without_a_stated_finish_is_still_quiescent():
    """Control: completion is never inferred from quietness. A pane whose last words
    say nothing about finishing keeps the structural `quiescent` verdict."""
    tail = "❯ continue with the next safe\n  step\n● Looking at the second replica now.\n"
    r = aw.classify(tail, state="idle", prev_cls="working", quiet_secs=400)
    assert r["cls"] == "quiescent"
