"""Wake companion: one delivery path, canonical target resolved fresh on every tick.

The keyboard/xdotool fallback is gone by design — it typed into whatever window happened to
be focused, which is exactly how a phrase could land in the wrong chat, and it could never
verify a keystroke arrived. These tests pin the companion to the only safe shape: ask the
bridge, submit through `cdp_composer.submit_phrase` with the conversation the bridge
resolved THIS tick, acknowledge only on verified delivery.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest

spec = importlib.util.spec_from_file_location(
    "wake_companion", "/root/ai-dev-runtime/tools/wake_companion.py")
wc = importlib.util.module_from_spec(spec)
sys.modules["wake_companion"] = wc
spec.loader.exec_module(wc)


class _Bridge:
    """Fake wake_bridge: scripted pending answers, records acknowledgements."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.acked = []

    def pending_wake(self):
        return self.answers.pop(0) if self.answers else {"pending": False,
                                                         "reason": "nothing_to_wake_for"}

    def acknowledge(self, event_id):
        self.acked.append(event_id)


@pytest.fixture
def composer(monkeypatch):
    """Stub cdp_composer capturing every submission; result is settable per test."""
    calls = []
    mod = types.ModuleType("cdp_composer")
    mod.result = {"ok": True, "reason": "submitted_and_user_turn_appeared"}

    def submit_phrase(conversation, phrase, *, source, event_id, actionable=False):
        calls.append({"conversation": conversation, "phrase": phrase,
                      "source": source, "event_id": event_id, "actionable": actionable})
        return mod.result

    mod.submit_phrase = submit_phrase
    mod.calls = calls
    monkeypatch.setitem(sys.modules, "cdp_composer", mod)
    return mod


def _pending(event_id, conversation, actionable=False):
    return {"pending": True, "event_id": event_id, "conversation": conversation,
            "phrase": "PHRASE", "actionable": actionable}


def test_nothing_pending_means_nothing_submitted(composer):
    b = _Bridge([{"pending": False, "reason": "nothing_to_wake_for"}])
    r = wc.tick(b)
    assert r["acted"] is False
    assert composer.calls == []
    assert b.acked == []


def test_the_submission_uses_the_conversation_the_bridge_resolved(composer):
    b = _Bridge([_pending(7, "https://chatgpt.com/c/bound-target")])
    r = wc.tick(b)
    assert composer.calls[0]["conversation"] == "https://chatgpt.com/c/bound-target"
    assert composer.calls[0]["phrase"] == "PHRASE"
    assert r["ok"] is True and b.acked == [7]


def test_a_rebind_between_ticks_reaches_the_new_chat_with_no_restart(composer):
    """The stale-chat regression: the target is re-resolved every tick, never cached, so
    the tick after a rebind submits into the NEW conversation."""
    b = _Bridge([_pending(1, "https://chatgpt.com/c/old-one"),
                 _pending(2, "https://chatgpt.com/c/new-one")])
    wc.tick(b)
    wc.tick(b)
    assert [c["conversation"] for c in composer.calls] == [
        "https://chatgpt.com/c/old-one", "https://chatgpt.com/c/new-one"]


def test_an_unverified_delivery_is_never_acknowledged(composer):
    composer.result = {"ok": False, "reason": "user_turn_not_observed_after_send"}
    b = _Bridge([_pending(9, "https://chatgpt.com/c/a")])
    r = wc.tick(b)
    assert r["ok"] is False
    assert b.acked == [], "acknowledge only on verified delivery"


def test_a_missing_composer_fails_closed_without_acknowledging(monkeypatch):
    monkeypatch.setitem(sys.modules, "cdp_composer", None)  # import raises
    b = _Bridge([_pending(3, "https://chatgpt.com/c/a")])
    r = wc.tick(b)
    assert r["acted"] is True and r["ok"] is False
    assert "cdp_unavailable" in r["reason"]
    assert b.acked == []


def test_the_actionable_class_is_carried_through_to_the_claim(composer):
    b = _Bridge([_pending(5, "https://chatgpt.com/c/a", actionable=True)])
    wc.tick(b)
    assert composer.calls[0]["actionable"] is True


def test_there_is_no_keyboard_fallback_path():
    """The xdotool path is not dormant — it is gone. Typing into the focused window is how
    a phrase lands in the wrong chat."""
    import inspect
    src = inspect.getsource(wc)
    for banned in ("xdotool", "windowactivate", "getactivewindow", "ctrl+l"):
        assert banned not in src, f"keyboard fallback resurfacing: {banned}"
    assert not hasattr(wc, "submit")
