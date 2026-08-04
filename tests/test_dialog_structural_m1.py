"""M1 — SHAPE-INDEPENDENT dialog detection (2026-08-04 targeted review of f9c06ee).

The finding: `_DIALOG_RE` is a denylist of KNOWN dialog phrasings, so an unseen
wording evades it. The review's live probe

    Allow this tool to run?
    > approve / deny

returned `looks_like_dialog() is False` — the pane classified `idle`, and the
watchdog would have pressed Enter or pasted onto it, ANSWERING a permission
prompt. Claude Code's own shapes were covered; a third-party CLI's were not.

Fix: a second, wording-independent detector matching the STRUCTURE of a prompt —
a short question at the pane tip plus a small set of mutually exclusive choices
(slash pair, bullet/numbered/arrow options), or an explicit permission choice
with no question mark at all. Language-agnostic by construction: the Russian
cases below use phrasings that appear in NO pattern in the codebase.

Sections 1–3 FAIL on pre-fix `9fbb7f4`. Section 4 pins that ordinary agent output
(prose, task footers, bullet lists, a live turn that happens to ask something)
is still NOT a dialog — over-refusal only delays a poke, but blanket refusal
would stall every agent.
"""
from __future__ import annotations

import pytest

from core import agent_control as ac
from core import agent_continuation_watchdog as cw
from core import commander_autopilot as ap


NOW = 1_700_000_000.0
SAFE_STEP = "continue with the next safe step"

# ── the exact live probe from the review, plus unseen wordings ───────────────
M1_LIVE_PROBE = "Allow this tool to run?\n> approve / deny"

EN_UNSEEN = [
    M1_LIVE_PROBE,
    "Allow this tool to run?\n  1) approve\n  2) deny",
    "Grant access to the keychain?\n❯ yes / no",
    "Overwrite the existing artifact?\n  • keep\n  • replace",
    "Authorize the connection?\n> allow / deny",
]

# Russian phrasings that match NO pattern in _DIALOG_RE (no «продолжить?»,
# no «разрешить?», no «вы уверены»).
RU_UNSEEN = [
    "Разрешить запуск инструмента?\n> разрешить / запретить",
    "Дать доступ к файлу?\n  • да\n  • нет",
    "Открыть сетевое соединение?\n  1) принять\n  2) отклонить",
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setattr(cw, "ENABLED", True)
    yield


def _confirmed_prev():
    return {"idle_since_ts": NOW - cw.IDLE_CONFIRM_SECS - 5, "last_state": "idle"}


def _decide(tail, pending="", proactive=False):
    agent = {"target": "proj:0.0", "session": "proj", "alive": True, "is_agent": True,
             "state": "idle", "_tail": tail, "claude_cwd": "/opt/proj"}
    return cw.decide(agent=agent, cfg={}, pending=pending, state="idle",
                     prev_target=_confirmed_prev(), now_ts=NOW, eligible=True,
                     continuation=SAFE_STEP, proactive=proactive, conv_count=0)


# ═════════════ 1. the live probe and other unseen English wordings ═══════════
def test_the_reviews_live_probe_is_detected():
    """Pre-fix: looks_like_dialog is False and classify_state is 'idle'."""
    assert ac.looks_like_dialog(M1_LIVE_PROBE) is True
    assert ac.classify_state(True, True, M1_LIVE_PROBE) == "waiting_owner"


@pytest.mark.parametrize("tail", EN_UNSEEN)
def test_unseen_english_prompt_shapes_are_detected(tail):
    assert ac.looks_like_dialog(tail) is True, tail
    assert ac.classify_state(True, True, tail) == "waiting_owner", tail


# ═════════════ 2. same structure, Russian, no known phrase present ═══════════
@pytest.mark.parametrize("tail", RU_UNSEEN)
def test_unseen_russian_prompt_shapes_are_detected(tail):
    from core.agent_control import _DIALOG_RE, _dialog_scan_text
    assert _DIALOG_RE.search(_dialog_scan_text(tail)) is None, "not an unseen wording"
    assert ac.looks_like_dialog(tail) is True, tail
    assert ac.classify_state(True, True, tail) == "waiting_owner", tail


def test_structural_detection_survives_styling_and_frames():
    styled = "\x1b[1mGrant access to the keychain?\x1b[0m\n\x1b[2m> yes / no\x1b[0m"
    boxed = "┌──────────────┐\n│ Дать доступ к файлу? │\n│ ❯ да / нет │\n└──────────────┘"
    for t in (styled, boxed):
        assert ac.looks_like_dialog(t) is True, t


def test_permission_choice_without_a_question_mark_is_a_dialog():
    assert ac.looks_like_dialog("❯ allow / deny") is True
    assert ac.looks_like_dialog("> разрешить / запретить") is True


# ═════════════ 3. the consumers refuse: watchdog and autopilot ═══════════════
@pytest.mark.parametrize("tail", EN_UNSEEN + RU_UNSEEN)
def test_watchdog_never_types_on_an_unseen_dialog_shape(tail):
    """Pre-fix: `deliver` — the continuation was pasted, and its Enter answers
    the prompt."""
    d = _decide(tail, "", proactive=True)
    assert d["action"] == "skip", (tail, d)
    assert d["reason"] in ("dialog_open_never_auto_answer",
                           "waiting_owner_supervisor"), (tail, d)


@pytest.mark.parametrize("tail", [M1_LIVE_PROBE, RU_UNSEEN[0]])
def test_autopilot_never_pokes_an_unseen_dialog_shape(tail):
    reg = {"cp-canary:0.0": {"root": "/tmp", "next_step": SAFE_STEP,
                             "autonomous_safe": True}}
    d = ap.evaluate("cp-canary:0.0", state="idle",
                    tail=tail + "\n3 tasks (1 done, 0 in progress, 2 open)",
                    registry=reg)
    assert d["decision"] in ("skip_dialog_open", "skip_other_state"), (tail, d)


# ═════════════ 4. anti-overcorrection: ordinary output is not a dialog ═══════
NOT_DIALOGS = [
    "I checked the config. Should we also add retries? I'll continue with the safe step.",
    "❯ ready\nrepo clean",
    "3 tasks (1 done, 0 in progress, 2 open)\n❯ ",
    "- fixed the parser\n- added a test\n- ran the suite",
    "Wrote reports/X.md (219 lines)\n  - section 1\n  - section 2",
    "",
]


@pytest.mark.parametrize("tail", NOT_DIALOGS)
def test_ordinary_pane_output_is_not_a_dialog(tail):
    assert ac.looks_like_dialog(tail) is False, tail


def test_a_working_pane_that_asks_something_is_still_working():
    """Precedence pin: a live turn printing a question must not become
    waiting_owner — that would stall a genuinely busy agent."""
    tail = "Allow this tool to run?\n> approve / deny\n✻ Pondering… (8s · thinking)\n"
    assert ac.classify_state(True, True, tail) == "working"


def test_clean_idle_pane_still_reaches_delivery():
    d = _decide("❯ ready\nrepo clean", "", proactive=True)
    assert d["action"] == "deliver" and d["step_text"] == SAFE_STEP


def test_long_prose_ending_in_a_question_is_not_a_prompt():
    """A paragraph is not a dialog: the question line length cap keeps narrative
    text from stalling every poke."""
    prose = ("I reviewed the whole delivery path and the ledger rows line up, but I am "
             "not sure whether the retry window should be widened before the next "
             "canary rotation or after the owner reviews the report?")
    assert ac.looks_like_dialog(prose) is False
