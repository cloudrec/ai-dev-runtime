"""The submit half of delivery — the 2026-09-24 transport bug.

Reproduced on security-demo and hostsecure: `agent_send` returned
`delivered=true, submitted=true, pane_changed=true` while the whole message sat
visibly in Claude's input box, `agent_status` stayed `waiting_input`, and no turn
started — identically in a freshly started session, because the fault is in the
transport, not the session.

Two causes, both proven at the byte level against a reader pane on 2026-09-24:

  1. `paste-buffer` without `-r` REWRITES the payload: every LF becomes CR. The
     reader received `b'line one\\rline two\\rline three'` — a multi-line message
     delivered as a burst of Enters.
  2. the submit Enter followed the paste by ~18 ms, inside the window Claude Code
     uses to recognise a paste, so it was absorbed into the pasted block as one more
     newline instead of submitting. tmux still returned 0, and `delivered`/`submitted`
     were computed from that return code alone.

After the fix the same reader saw `b'line one\\nline two\\nline three'` and, 275 ms
later, a separate `b'\\r'`.
"""
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import agent_control as ac  # noqa: E402

PANE = "probe:0.0"


class _Tmux:
    """Records argv; answers captures from a queue; notes call ORDER for the delay."""

    def __init__(self, pending_after=("",), capture="pane text"):
        self.calls = []
        self.capture = capture
        self.pending_after = list(pending_after)

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        cmd = args[0]
        if cmd == "capture-pane":
            if "-e" in args:                      # the composer read
                nxt = (self.pending_after.pop(0) if len(self.pending_after) > 1
                       else self.pending_after[0])
                return (0, f"❯ {nxt}" if nxt else "❯ ", "")
            return (0, self.capture, "")
        return (0, "", "")

    def argv(self, cmd):
        return [c for c, _ in self.calls if c and c[0] == cmd]

    def order(self):
        return [c[0] for c, _ in self.calls]


@pytest.fixture()
def wired(monkeypatch):
    slept = []
    monkeypatch.setattr(ac, "_pane_is_live_agent",
                        lambda t: {"target": PANE, "claude_cwd": "/opt/x"})
    monkeypatch.setattr(ac.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ac, "_record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: None)
    monkeypatch.setattr(ac, "audit", lambda *a, **k: None)
    return slept


# ── cause 1: the payload must not be rewritten ──────────────────────────────

def test_the_paste_never_rewrites_newlines_into_enters(wired, monkeypatch):
    t = _Tmux()
    monkeypatch.setattr(ac, "_tmux", t)
    ac._deliver_locked(PANE, "line one\nline two\nline three", "agent_send", "k1")
    paste = t.argv("paste-buffer")[0]
    assert "-r" in paste, "without -r tmux turns every newline into a carriage return"


def test_the_message_still_travels_as_a_buffer_never_as_key_names(wired, monkeypatch):
    t = _Tmux()
    monkeypatch.setattr(ac, "_tmux", t)
    text = "line one\nline two with 'quotes' and $VARS"
    ac._deliver_locked(PANE, text, "agent_send", "k2")
    load = [(c, s) for c, s in t.calls if c[0] == "load-buffer"]
    assert load and load[0][1] == text.encode()
    for keys in t.argv("send-keys"):
        assert keys[-1] == "Enter" and text not in " ".join(keys)


# ── cause 2: the submit keystroke must land outside the paste window ────────

def test_the_submit_waits_for_the_paste_to_settle(wired, monkeypatch):
    t = _Tmux()
    monkeypatch.setattr(ac, "_tmux", t)
    ac._deliver_locked(PANE, "hello", "agent_send", "k3")
    order = t.order()
    assert order.index("paste-buffer") < order.index("send-keys")
    assert ac._SUBMIT_SETTLE_SECS in wired, "no settle between the paste and the Enter"
    assert ac._SUBMIT_SETTLE_SECS >= 0.1


def test_the_settle_is_tunable_for_a_slow_host():
    import importlib
    import os
    os.environ["AGENT_SEND_SUBMIT_SETTLE_SECS"] = "0.75"
    try:
        importlib.reload(ac)
        assert ac._SUBMIT_SETTLE_SECS == 0.75
    finally:
        del os.environ["AGENT_SEND_SUBMIT_SETTLE_SECS"]
        importlib.reload(ac)


# ── the honest verdict ──────────────────────────────────────────────────────

def test_text_still_in_the_composer_is_not_a_successful_send(wired, monkeypatch):
    """The exact reported symptom: tmux returns 0, the message is still on screen."""
    t = _Tmux(pending_after=("hello there",))      # never consumed, even after the retry
    monkeypatch.setattr(ac, "_tmux", t)
    r = ac._deliver_locked(PANE, "hello there", "agent_send", "k4")
    assert r["submitted"] is False and r["delivered"] is False
    assert r["prompt_consumed"] is False and r["submit_retried"] is True
    assert "NOT" in r["reason"] and "submitted" in r["reason"]


def test_a_second_enter_is_pressed_once_and_rescues_a_missed_submit(wired, monkeypatch):
    t = _Tmux(pending_after=("hello there", ""))   # consumed on the retry
    monkeypatch.setattr(ac, "_tmux", t)
    r = ac._deliver_locked(PANE, "hello there", "agent_send", "k5")
    assert r["submitted"] is True and r["submit_retried"] is True
    assert len(t.argv("send-keys")) == 2, "exactly one retry — a bare Enter cannot duplicate"


def test_a_consumed_prompt_needs_no_retry(wired, monkeypatch):
    t = _Tmux(pending_after=("",))
    monkeypatch.setattr(ac, "_tmux", t)
    r = ac._deliver_locked(PANE, "hello", "agent_send", "k6")
    assert r["submitted"] is True and r["submit_retried"] is False
    assert len(t.argv("send-keys")) == 1


def test_someone_elses_text_in_the_box_still_counts_as_ours_submitted(wired, monkeypatch):
    """A box holding something that is not our message means ours is not pending."""
    t = _Tmux(pending_after=("a different instruction",))
    monkeypatch.setattr(ac, "_tmux", t)
    r = ac._deliver_locked(PANE, "hello there", "agent_send", "k7")
    assert r["prompt_consumed"] is True and r["submitted"] is True


def test_a_queued_message_is_still_never_reported_as_delivered(wired, monkeypatch):
    t = _Tmux(pending_after=("",), capture="Press up to edit queued messages")
    monkeypatch.setattr(ac, "_tmux", t)
    r = ac._deliver_locked(PANE, "hello", "agent_send", "k8")
    assert r["queued"] is True and r["delivered"] is False and r["submitted"] is False


def test_an_unreadable_composer_does_not_manufacture_a_failure(monkeypatch):
    """The retry has already run by then; an unreadable pane is not proof either way,
    and every other reader here treats it the same."""
    def boom(*a, **k):
        raise OSError("capture failed")
    monkeypatch.setattr(ac, "pending_input_text", boom)
    assert ac._prompt_consumed(PANE, "hello") is True


def test_the_first_line_is_what_the_composer_can_show():
    assert ac._first_line("line one\nline two") == "line one"
    assert ac._first_line("  spaced   out  \nmore") == "spaced out"
    assert ac._first_line("") == ""
