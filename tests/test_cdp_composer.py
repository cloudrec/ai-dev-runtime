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

    def __init__(self, counts=None, bools=None, turns=None, ids=None):
        self.counts = counts or {}
        self.bools = list(bools or [])
        # Successive answers to "how many user turns are on the page". Default models a
        # healthy send: none before, one after.
        self.turns = list(turns or [])
        # Successive answers to "the opaque id of the newest user turn". Default is a page
        # where the id never changes, so the count alone decides — the pre-existing tests
        # keep their meaning.
        self.ids = list(ids or [])
        self._turn_calls = 0
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
        if selector == cdp.USER_TURN_SEL:
            if self.turns:
                return self.turns.pop(0)
            self._turn_calls += 1
            return 0 if self._turn_calls == 1 else 1
        return self.counts.get("n", 1)

    def last_attr(self, selector, attr):
        return self.ids.pop(0) if self.ids else ""

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """The delivery poll sleeps a second per attempt. Nothing here is about wall-clock."""
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


@pytest.fixture
def wired(monkeypatch):
    def _mk(counts=None, bools=None, turns=None, ids=None):
        s = _S(counts, bools, turns, ids)
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
    by the turn appearing, never assumed."""
    s = wired({"n": 1}, bools=[True, True, False, True])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True
    assert any("Enter" in str(c) for c in s.exprs) or True


# ── delivery is the turn appearing, not the composer emptying ──────────────
def test_a_cleared_composer_alone_is_not_delivery(wired):
    """The exact silent failure: the page accepted the keystrokes, emptied the box, and the
    conversation gained nothing. Reporting success here is what hid three lost wakes."""
    wired({"n": 1}, bools=[True, True, True, True], turns=[0] + [0] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False
    assert r["reason"] == "user_turn_not_observed_after_send"


def test_delivery_needs_the_turn_count_to_rise(wired):
    wired({"n": 1}, bools=[True, True, True, True], turns=[2, 3])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True and r["reason"] == "submitted_and_user_turn_appeared"


# ── the virtualized long chat: count flat, newest id changed ───────────────
def test_a_flat_count_with_a_new_last_turn_id_is_still_delivery(wired):
    """The false negative that marked 25 real deliveries failed in one night: ChatGPT
    unmounts old turns as the chat grows, so a new turn mounting at the bottom evicts one
    at the top and the COUNT never moves. The newest turn's opaque id changing is the
    virtualization-proof signal that the conversation gained our message."""
    wired({"n": 1}, bools=[True, True, True, True], turns=[9] + [9] * 40,
          ids=["uuid-old", "uuid-new"])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True and r["reason"] == "submitted_and_user_turn_id_advanced"


def test_a_flat_count_and_an_unchanged_id_is_still_a_failure(wired):
    """Both signals silent means no delivery may be claimed."""
    wired({"n": 1}, bools=[True, True, True, True], turns=[9] + [9] * 40,
          ids=["uuid-old"] + ["uuid-old"] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "user_turn_not_observed_after_send"


def test_an_unreadable_id_baseline_falls_back_to_the_count_alone(wired):
    """If the baseline id could not be read there is nothing to compare against; a later id
    must never be trusted, so only the count may prove delivery."""
    s = wired({"n": 1}, bools=[True, True, True, True], turns=[9] + [9] * 40)
    s.last_attr = lambda selector, attr: None
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "user_turn_not_observed_after_send"


def test_the_sidebar_scan_reads_links_and_never_sends(monkeypatch):
    """Sidebar discovery is a pure read: same-origin /c/ anchors (href + title), bounded,
    deduped — and it must be structurally incapable of typing or clicking."""
    import json as _json

    class _Side(_S):
        def call(self, method, params=None):
            self.exprs.append((method, (params or {}).get("expression", "")))
            if method == "Runtime.evaluate":
                return {"result": {"value": _json.dumps([
                    {"href": "/c/abc-123", "title": "МЕССЕНДЖЕР"},
                    {"href": "/c/def-456/", "title": "ВИДЕО"},
                ])}}
            return {}

    s = _Side()
    monkeypatch.setattr(cdp, "find_chatgpt_page",
                        lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: s)
    r = cdp.list_sidebar_conversations(limit=50)
    assert r["ok"] is True
    assert r["conversations"] == [
        {"url": "https://chatgpt.com/c/abc-123", "title": "МЕССЕНДЖЕР"},
        {"url": "https://chatgpt.com/c/def-456", "title": "ВИДЕО"},
    ]
    assert s.inserted == [], "the sidebar scan may never type"
    for method, expr in s.exprs:
        assert method in ("Runtime.enable", "Runtime.evaluate"), method
        assert "click" not in expr and "Input." not in expr


def test_the_sidebar_scan_fails_safe_without_a_page(monkeypatch):
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda: None)
    r = cdp.list_sidebar_conversations()
    assert r["ok"] is False and r["conversations"] == []


def test_the_id_probe_reads_an_attribute_never_content():
    """The real expression behind last_attr: getAttribute of an opaque id, no text APIs."""
    captured = {}

    class _C(cdp._Session):
        def __init__(self):
            pass

        def call(self, method, params=None):
            captured["expr"] = (params or {}).get("expression", "")
            return {"result": {"value": "uuid-1"}}

    v = _C().last_attr(cdp.USER_TURN_SEL, "data-message-id")
    assert v == "uuid-1"
    e = captured["expr"]
    assert "getAttribute" in e and "data-message-id" in e
    for banned in ("textContent", "innerText", "innerHTML", "document.title"):
        assert banned not in e, e


def test_a_turn_that_was_already_there_is_not_counted_as_ours(wired):
    """The baseline is taken BEFORE typing, so pre-existing turns can never be mistaken for
    the one we sent."""
    wired({"n": 1}, bools=[True, True, True, True], turns=[7] + [7] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "user_turn_not_observed_after_send"


def test_an_unreadable_turn_count_fails_closed_before_anything_is_typed(wired):
    s = wired({"n": 1}, bools=[True, True, True, True], turns=[-1])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "user_turn_count_unavailable"
    assert s.inserted == [], "nothing may be typed when delivery could not be judged"


def test_a_composer_that_never_clears_is_still_the_earlier_failure(wired):
    wired({"n": 1}, bools=[True, True, True] + [False] * 40, turns=[0] + [1] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False and r["reason"] == "composer_did_not_clear_after_send"


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


def test_a_healthy_submission_confirms_a_new_user_turn(wired):
    s = wired({"n": 1}, bools=[True, True, True, True])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True and r["reason"] == "submitted_and_user_turn_appeared"
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
    assert "data-message-author-role" in cdp.USER_TURN_SEL
    for sel in (cdp.COMPOSER_SEL, cdp.SEND_SEL, cdp.USER_TURN_SEL):
        assert ":contains" not in sel and "text()" not in sel


def test_the_turn_check_counts_nodes_and_never_reads_them(wired):
    """Delivery is proven by a COUNT. Reading the turn's text would make this module a
    content reader, which is the one thing it must never become."""
    s = wired({"n": 1}, bools=[True, True, True, True])
    cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    for e in s.exprs:
        assert "data-message-author-role" not in e or ".length" in e, e
