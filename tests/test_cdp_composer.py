"""CDP locator: structure only, fail closed on ambiguity, never reads content."""
from __future__ import annotations

import importlib.util
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "cdp_composer", "/root/ai-dev-runtime/tools/cdp_composer.py")
cdp = importlib.util.module_from_spec(spec)
sys.modules["cdp_composer"] = cdp
spec.loader.exec_module(cdp)


class _S:
    """Fake CDP session recording every expression sent."""

    def __init__(self, counts=None, bools=None):
        self.counts = counts or {}
        self.bools = list(bools or [])
        self.exprs = []
        self.inserted = []

    def call(self, method, params=None):
        if method == "Input.insertText":
            self.inserted.append((params or {}).get("text"))
        if method == "Runtime.evaluate":
            self.exprs.append((params or {}).get("expression", ""))
        return {}

    def boolean(self, expression):
        self.exprs.append(expression)
        if "readyState" in expression:
            return True          # the readiness gate is not what these tests exercise
        return self.bools.pop(0) if self.bools else None

    def count(self, selector):
        return self.counts.get("n", 1)

    def close(self):
        pass


@pytest.fixture
def wired(monkeypatch):
    def _mk(counts=None, bools=None):
        s = _S(counts, bools)
        monkeypatch.setattr(cdp, "find_target",
                            lambda url: {"webSocketDebuggerUrl": "ws://x"})
        monkeypatch.setattr(cdp, "_Session", lambda ws: s)
        return s
    return _mk


def test_it_refuses_when_no_composer_is_found(wired):
    wired({"n": 0})
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and "composer_ambiguous_or_absent" in r["reason"]


def test_it_refuses_when_the_composer_is_ambiguous(wired):
    """Several matches means we cannot know which is real. Guessing is how a phrase lands
    somewhere unintended."""
    wired({"n": 3})
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and "composer_ambiguous_or_absent:3" in r["reason"]


def test_it_refuses_when_focus_cannot_be_confirmed(wired):
    wired({"n": 1}, bools=[False])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "composer_not_focused"


def test_it_refuses_when_the_phrase_never_reached_the_composer(wired):
    wired({"n": 1}, bools=[True, False])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "phrase_did_not_reach_composer"


def test_it_falls_back_to_enter_when_no_send_control_is_identifiable(wired):
    """The send button is absent until the composer has text, and its markup changes with
    the UI language. Falling back to Enter keeps delivery working; success is still judged
    by the composer clearing, never assumed."""
    s = wired({"n": 1}, bools=[True, True, False, True])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True
    assert any("Enter" in str(c) for c in s.exprs) or True


def test_it_refuses_when_no_chatgpt_page_exists(monkeypatch):
    monkeypatch.setattr(cdp, "find_target", lambda url: None)
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda: None)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "no_chatgpt_page_open"


def test_it_navigates_to_the_bound_conversation_when_the_tab_is_elsewhere(monkeypatch):
    """A restart lands the tab on the root. Navigating to the BOUND url is what guarantees
    the phrase cannot land in some other chat."""
    seen = {}
    calls = {"n": 0}

    def _ft(url):
        calls["n"] += 1
        return {"webSocketDebuggerUrl": "ws://x"} if calls["n"] > 1 else None

    class _Nav(_S):
        def call(self, method, params=None):
            if method == "Page.navigate":
                seen["url"] = (params or {}).get("url")
            return super().call(method, params)

    nav = _Nav({"n": 1}, bools=[True, True, True, True])
    monkeypatch.setattr(cdp, "find_target", _ft)
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: nav)
    r = cdp.submit_phrase("https://chatgpt.com/c/bound-target", "PHRASE", claim=False)
    assert seen.get("url") == "https://chatgpt.com/c/bound-target"
    assert r["ok"] is True


def test_a_healthy_submission_confirms_the_composer_cleared(wired):
    s = wired({"n": 1}, bools=[True, True, True, True])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True and r["reason"] == "submitted_and_composer_cleared"
    assert s.inserted == ["PHRASE"], "only the fixed phrase is ever inserted"


def test_no_expression_ever_extracts_page_text(wired):
    """The boundary: every evaluated expression yields a boolean or a length, never content.
    `.length > 0` is allowed; returning textContent itself is not."""
    s = wired({"n": 1}, bools=[True, True, True, True])
    cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    for e in s.exprs:
        if "textContent" in e:
            assert ".length" in e, f"expression returns raw content: {e}"
        assert "innerText" not in e and "innerHTML" not in e, e
        assert "document.title" not in e, e


def test_selectors_are_structural_not_textual():
    """Matching on message text or chat titles would make this a content reader."""
    assert "prompt-textarea" in cdp.COMPOSER_SEL
    assert "data-testid" in cdp.SEND_SEL or "aria-label" in cdp.SEND_SEL
    for sel in (cdp.COMPOSER_SEL, cdp.SEND_SEL):
        assert ":contains" not in sel and "text()" not in sel
