"""Wake companion submission: no fixed coordinates, fail closed at every step."""
from __future__ import annotations

import importlib.util
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "wake_companion", "/root/ai-dev-runtime/tools/wake_companion.py")
wc = importlib.util.module_from_spec(spec)
sys.modules["wake_companion"] = wc
spec.loader.exec_module(wc)


class _X:
    """Records every xdotool call so the test can assert on what was attempted."""

    def __init__(self, active="W1", frames=None):
        self.calls = []
        self.active = active
        self.frames = list(frames or ["a", "b", "c"])

    def __call__(self, *args, timeout=15):
        self.calls.append(args)
        class R:
            stdout = ""
        if args and args[0] == "search":
            R.stdout = "W1\n"
        if args and args[0] == "getactivewindow":
            R.stdout = self.active
        return R

    def frame(self):
        return self.frames.pop(0) if self.frames else "z"


@pytest.fixture
def x(monkeypatch):
    stub = _X()
    monkeypatch.setattr(wc, "_x", stub)
    monkeypatch.setattr(wc, "_frame_signature", stub.frame)
    monkeypatch.setattr(wc, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    return stub


def test_no_fixed_coordinates_are_ever_used(x):
    """Clicking a hardcoded pixel was the fragile part: a layout change sent the phrase
    nowhere and a click cannot be verified."""
    wc.submit("PHRASE", "https://chatgpt.com/c/abc")
    flat = [" ".join(c) for c in x.calls]
    assert not any("mousemove" in f or "click" in f for f in flat), flat


def test_it_refuses_when_the_window_is_not_focused(monkeypatch):
    stub = _X(active="SOMETHING-ELSE")
    monkeypatch.setattr(wc, "_x", stub)
    monkeypatch.setattr(wc, "_frame_signature", stub.frame)
    monkeypatch.setattr(wc, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    assert wc.submit("PHRASE", "https://chatgpt.com/c/abc") is False
    assert not any("Return" in " ".join(c) for c in stub.calls), \
        "nothing may be submitted into an unfocused window"


def test_it_refuses_when_typing_changed_nothing(monkeypatch):
    """Identical frames before and after typing mean the keystrokes went nowhere; pressing
    Enter would submit into the void and report success."""
    stub = _X(frames=["same", "same"])
    monkeypatch.setattr(wc, "_x", stub)
    monkeypatch.setattr(wc, "_frame_signature", stub.frame)
    monkeypatch.setattr(wc, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    assert wc.submit("PHRASE", "https://chatgpt.com/c/abc") is False


def test_it_clears_the_line_after_refusing_to_submit(monkeypatch):
    """A refused attempt must not leave half-typed text sitting in the composer."""
    stub = _X(frames=["same", "same"])
    monkeypatch.setattr(wc, "_x", stub)
    monkeypatch.setattr(wc, "_frame_signature", stub.frame)
    monkeypatch.setattr(wc, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    wc.submit("PHRASE", "https://chatgpt.com/c/abc")
    flat = [" ".join(c) for c in stub.calls]
    assert any("ctrl+a" in f for f in flat) and any("Delete" in f for f in flat)


def test_it_does_not_claim_success_when_the_screen_never_changed(monkeypatch):
    stub = _X(frames=["a", "b", "b"])   # typing changed it, Return did not
    monkeypatch.setattr(wc, "_x", stub)
    monkeypatch.setattr(wc, "_frame_signature", stub.frame)
    monkeypatch.setattr(wc, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    assert wc.submit("PHRASE", "https://chatgpt.com/c/abc") is False


def test_a_healthy_submission_navigates_to_the_bound_conversation(x):
    ok = wc.submit("PHRASE", "https://chatgpt.com/c/target-123")
    flat = [" ".join(c) for c in x.calls]
    assert ok is True
    assert any("target-123" in f for f in flat), "must navigate to the BOUND conversation"
    assert any("PHRASE" in f for f in flat)


def test_only_the_fixed_phrase_is_ever_typed(x):
    wc.submit("THE ONLY PHRASE", "https://chatgpt.com/c/abc")
    typed = [c for c in x.calls if c and c[0] == "type"]
    payloads = [c[-1] for c in typed]
    assert "THE ONLY PHRASE" in payloads
    assert all(p == "THE ONLY PHRASE" or p.startswith("https://") for p in payloads), payloads
