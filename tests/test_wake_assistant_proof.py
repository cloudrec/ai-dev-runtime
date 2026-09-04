"""A user turn appearing is not proof the reviewer ran (the false positive).

Reported by the owner: events 9968 and 9973 were recorded as
`submitted_and_user_turn_appeared`, yet on the conversation side no assistant
turn had started — the owner had to type manually and found both agents stopped.
Inspecting the bound conversation confirmed it: three deliveries recorded as
successful into a chat that held two user turns.

The old criterion proved the PAGE rendered our text. It did not prove the
backend accepted it, that it persisted, or that the assistant ever ran. A wake
exists to make the reviewer run, so that is what success has to mean.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime/tools")
import cdp_composer as cc  # noqa: E402


class _FakeSession:
    """Scripted DOM. Each counter/attr is a list consumed one call at a time,
    so a test can describe how the page evolves after the send."""

    def __init__(self, *, streaming=(), assistant_counts=(), assistant_ids=()):
        self._streaming = list(streaming)
        self._acounts = list(assistant_counts)
        self._aids = list(assistant_ids)

    def _next(self, seq, default):
        if not seq:
            return default
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def count(self, selector):
        if selector == cc.STREAMING_SEL:
            return self._next(self._streaming, 0)
        if selector == cc.ASSISTANT_TURN_SEL:
            return self._next(self._acounts, 0)
        return 0

    def last_len(self, selector):
        """Rendered length of the newest matching turn. Default 999 = a real
        answer, so the settle check treats these proofs as genuine deliveries;
        a test that cares about an EMPTY turn sets `assistant_len`."""
        return getattr(self, "assistant_len", 999)

    def last_attr(self, selector, attr):
        if selector == cc.ASSISTANT_TURN_SEL:
            return self._next(self._aids, None)
        return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(cc, "ASSISTANT_START_SECS", 4)
    monkeypatch.setattr(cc.time, "sleep", lambda _s: None)


# ── the false positive, reproduced ─────────────────────────────────────────

def test_a_user_turn_with_no_assistant_response_is_a_failure():
    """THE regression: the message landed, nothing ran, and the old code called
    that success."""
    s = _FakeSession(streaming=[0], assistant_counts=[2], assistant_ids=["same"])
    out = cc._await_assistant(s, asst_before=2, asst_id_before="same")
    assert out["ok"] is False
    assert "assistant_never_started" in out["reason"]
    assert "user_turn_landed" in out["reason"]


def test_the_old_reason_string_is_gone_from_the_success_path():
    """`submitted_and_user_turn_appeared` must no longer be returned as success
    anywhere — it is the exact claim that proved false."""
    import inspect
    src = inspect.getsource(cc._attempt)
    assert '"submitted_and_user_turn_appeared"' not in src


# ── what genuine success looks like ────────────────────────────────────────

def test_streaming_control_counts_as_generation_started():
    s = _FakeSession(streaming=[1])
    out = cc._await_assistant(s, asst_before=0, asst_id_before=None)
    assert out["ok"] is True
    assert out["reason"] == "submitted_and_assistant_started_generating"


def test_a_new_assistant_turn_counts():
    s = _FakeSession(streaming=[0], assistant_counts=[3])
    out = cc._await_assistant(s, asst_before=2, asst_id_before=None)
    assert out["ok"] is True
    assert out["reason"] == "submitted_and_assistant_responded"


def test_a_changed_assistant_id_counts_under_virtualization():
    """A long chat unmounts old turns, so the count can stay flat while a new
    assistant turn arrives — the same trick the user-turn check already uses."""
    s = _FakeSession(streaming=[0], assistant_counts=[2], assistant_ids=["new-id"])
    out = cc._await_assistant(s, asst_before=2, asst_id_before="old-id")
    assert out["ok"] is True
    assert out["reason"] == "submitted_and_assistant_turn_advanced"


def test_a_late_start_within_the_window_still_succeeds():
    """A slow assistant is not a failure; only silence is."""
    s = _FakeSession(streaming=[0, 0, 1], assistant_counts=[2], assistant_ids=["same"])
    out = cc._await_assistant(s, asst_before=2, asst_id_before="same")
    assert out["ok"] is True


def test_failure_is_reported_not_retried_by_resending():
    """The message is already in the chat and the submission is latched, so the
    recovery is an honest failure that the closed-loop watchdog escalates — never
    a resend, which would duplicate the turn."""
    import inspect
    src = inspect.getsource(cc._await_assistant)
    # It observes; it never types. Resending would duplicate a turn that is
    # already in the chat and already latched.
    assert "Input.insertText" not in src
    assert "dispatchKeyEvent" not in src
    # And it reports the failure rather than swallowing it.
    s = _FakeSession(streaming=[0], assistant_counts=[1], assistant_ids=["same"])
    out = cc._await_assistant(s, asst_before=1, asst_id_before="same")
    assert out["ok"] is False and out["reason"]
