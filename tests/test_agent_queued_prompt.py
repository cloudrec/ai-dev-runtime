"""A queued message is not a delivered one (the standing-agent defect).

The owner's report: Owner_OS.agent_send/agent_answer returned submitted=true
while the Claude UI showed the text only as a queued message
("Press up to edit queued messages") and never started processing it, so the
agent stood still after every review. Sending again stacked another queued
message.

Cause: `_deliver` proved delivery with `rc_enter == 0` — the exit code of the
tmux Enter keystroke. That proves the keystroke reached tmux, not that Claude
read anything. Claude Code queues input that arrives mid-turn, and queuing
returns 0 exactly like executing does. It is the same class of defect the
continuation watchdog was built for on a different path, still present on the
path the reviewer actually uses.
"""
from __future__ import annotations

import pytest

from core import agent_control as ac


BUSY_PANE = """
● Running the full suite now.

· Concocting… (2m 28s)

──────────────────────────────
❯
──────────────────────────────
"""

QUEUED_PANE = """
● Working on the previous request.

  Press up to edit queued messages
──────────────────────────────
❯
──────────────────────────────
"""

IDLE_PANE = """
● Done. 45/45 tests pass.

──────────────────────────────
❯
──────────────────────────────
"""


# ── the detector ───────────────────────────────────────────────────────────

def test_the_queue_hint_is_recognised():
    assert ac.looks_queued(QUEUED_PANE) is True
    assert ac.looks_queued("Press up to edit queued message") is True
    assert ac.looks_queued(IDLE_PANE) is False
    assert ac.looks_queued("") is False


# ── the reported defect, reproduced ────────────────────────────────────────

def test_a_queued_message_is_not_reported_as_submitted(monkeypatch):
    """THE regression: Enter returns 0, the pane says the message is queued, and
    the delivery must NOT claim success."""
    monkeypatch.setattr(ac, "_pane_is_live_agent", lambda t: {"target": t})
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: None)
    monkeypatch.setattr(ac, "_record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(ac, "audit", lambda *a, **k: None)
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def fake_tmux(args, stdin=None):
        calls["n"] += 1
        if args[0] == "capture-pane":
            wide = "-30" in args
            return (0, QUEUED_PANE if wide else "before-vs-after differs", "")
        return (0, "", "")                       # load-buffer / paste / Enter all succeed

    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    out = ac._deliver("s:0.0", "next block please", "agent_send", "k1")
    assert out["queued"] is True
    assert out["submitted"] is False             # was True before the fix
    assert out["delivered"] is False
    assert "queue" in out["queued_reason"]


def test_an_executed_message_is_still_reported_as_submitted(monkeypatch):
    """The fix must not turn every delivery into a refusal."""
    monkeypatch.setattr(ac, "_pane_is_live_agent", lambda t: {"target": t})
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: None)
    monkeypatch.setattr(ac, "_record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(ac, "audit", lambda *a, **k: None)
    monkeypatch.setattr(ac.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ac, "_tmux",
                        lambda args, stdin=None: (0, IDLE_PANE, "")
                        if args[0] == "capture-pane" else (0, "", ""))
    out = ac._deliver("s:0.0", "go", "agent_send", "k2")
    assert out["submitted"] is True
    assert out["queued"] is False


# ── one executable prompt, never an accumulating queue ─────────────────────

def test_send_refuses_while_the_agent_is_mid_turn(monkeypatch):
    monkeypatch.setattr(ac, "_pane_tail", lambda t, lines=12: BUSY_PANE)
    monkeypatch.setattr(ac, "audit", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(ac, "_deliver", lambda **kw: sent.append(kw) or {"submitted": True})
    out = ac.agent_send("gaika-server:0.0", "next block")
    assert out["submitted"] is False
    assert out["refused"] == "working"
    assert "queued behind the active turn" in out["reason"]
    assert sent == [], "nothing may be pasted onto a busy pane"


def test_send_refuses_when_a_queue_is_already_pending(monkeypatch):
    """The compounding case: one queued message must not become two."""
    monkeypatch.setattr(ac, "_pane_tail", lambda t, lines=12: QUEUED_PANE)
    monkeypatch.setattr(ac, "audit", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(ac, "_deliver", lambda **kw: sent.append(kw) or {"submitted": True})
    out = ac.agent_send("gaika-server:0.0", "another block")
    assert out["refused"] == "queued_messages_pending"
    assert sent == []


def test_send_proceeds_when_the_agent_can_actually_take_it(monkeypatch):
    monkeypatch.setattr(ac, "_pane_tail", lambda t, lines=12: IDLE_PANE)
    sent = []
    monkeypatch.setattr(ac, "_deliver",
                        lambda **kw: sent.append(kw) or {"submitted": True, "queued": False})
    out = ac.agent_send("gaika-server:0.0", "next block")
    assert out["submitted"] is True
    assert len(sent) == 1


def test_the_escape_hatch_is_explicit(monkeypatch):
    """A caller that genuinely wants to stack behind the turn must say so."""
    monkeypatch.setattr(ac, "_pane_tail", lambda t, lines=12: BUSY_PANE)
    sent = []
    monkeypatch.setattr(ac, "_deliver",
                        lambda **kw: sent.append(kw) or {"submitted": True})
    ac.agent_send("s:0.0", "queue it", allow_queue=True)
    assert len(sent) == 1


def test_an_unreadable_pane_does_not_block_the_control_plane(monkeypatch):
    """Refusing on unknown would make the plane unusable exactly when needed;
    the post-send queue check still reports the truth."""
    def boom(*_a, **_k):
        raise RuntimeError("tmux gone")
    monkeypatch.setattr(ac, "_pane_tail", boom)
    assert ac._busy_for_send("s:0.0") == ""


def test_answering_a_prompt_is_not_gated_by_busy(monkeypatch):
    """agent_answer responds to a live prompt — the pane is *supposed* to be
    sitting there waiting, and gating it would break prompt resolution."""
    import inspect
    src = inspect.getsource(ac.agent_answer)
    assert "_busy_for_send" not in src


# ── the classifier gap that made the gate ineffective ──────────────────────

def test_claude_working_is_detected_where_classify_state_says_idle():
    """classify_state() reported `idle` for a pane actively running a test
    suite, so gating on it alone still stacked messages onto a working agent."""
    assert ac.classify_state(True, True, BUSY_PANE) == "idle"   # the gap itself
    assert ac.claude_is_working(BUSY_PANE) is True              # what closes it


@pytest.mark.parametrize("tail", [
    "· Concocting… (2m 28s)",
    "✳ Thinking… (12s)",
    "  ⎿  running (esc to interrupt)",
])
def test_the_working_indicator_is_recognised_in_its_usual_forms(tail):
    assert ac.claude_is_working(tail) is True


def test_a_spinner_in_scrollback_does_not_refuse_forever(monkeypatch):
    """The same text scrolls harmlessly through history; only the live status
    region may decide the agent is busy."""
    scrolled = ("· Concocting… (2m 28s)\n" + "\n".join(f"output line {i}"
                                                       for i in range(40))
                + "\n● Done.\n❯\n")
    assert ac.claude_is_working(scrolled) is False
