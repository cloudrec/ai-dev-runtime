"""A conversation at ChatGPT's length cap must never read as an ordinary delivery.

2026-10-28: the owner-os and payment-orchestrator chats showed "You've reached the
maximum length for this conversation ... Start new chat" in an `aside[role=alert]`,
with the composer still editable. Nothing in the composer looked at it, so every
wake into those chats was recorded as a plain `submitted_and_assistant_started_generating`
and nobody could tell from the delivery ledger that the routes needed a new chat. The
notice comes and goes, and those chats kept answering (33 and 27 deliveries in six
hours), so it is carried into the verdict, not used to refuse.

Loads the composer from THIS checkout (the older suites hard-code the live path).
"""
from __future__ import annotations

import importlib.util
import os

import pytest

os.environ.setdefault("WAKE_ASSISTANT_SETTLE_SECS", "0")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_full", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

# The shared fake page, loaded by PATH: other suites put the live checkout first on
# sys.path, and `from tests.test_cdp_composer import _S` then picked up the live copy.
_fspec = importlib.util.spec_from_file_location(
    "cdp_composer_fake_page", os.path.join(_HERE, "tests", "test_cdp_composer.py"))
_fake = importlib.util.module_from_spec(_fspec)
_fspec.loader.exec_module(_fake)
_S = _fake._S

URL = "https://chatgpt.com/c/owner-os-chat"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    monkeypatch.setattr(cdp, "_latch_submitted", lambda *a, **k: None)
    monkeypatch.setattr(cdp, "_record_delivery", lambda src, eid, res, **k: res)


@pytest.fixture
def page(monkeypatch):
    def _mk(*, full=False, bools=None, turns=None, streaming=None, assistant=None):
        s = _S({"n": 1}, bools if bools is not None else [True] * 6, turns,
               None, streaming, assistant)
        s.conversation_full = full
        monkeypatch.setattr(cdp, "browser_degraded",
                            lambda *a, **k: {"degraded": False, "reason": "ok", "pages": 3})
        monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
        monkeypatch.setattr(cdp, "find_chatgpt_page",
                            lambda url=None: {"webSocketDebuggerUrl": "ws://x", "url": URL,
                                              "id": "t1"})
        monkeypatch.setattr(cdp, "find_target",
                            lambda url: {"webSocketDebuggerUrl": "ws://x", "url": URL,
                                         "id": "t1"})
        monkeypatch.setattr(cdp, "_Session", lambda ws: s)
        return s
    return _mk


def test_control_a_normal_chat_delivers_with_the_plain_reason(page):
    page(full=False)
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is True, r
    assert r["reason"] == "submitted_and_assistant_started_generating"
    assert "conversation_full" not in r


def test_a_capped_chat_delivery_is_never_recorded_as_a_plain_success(page):
    """Fails without the fix: the reason was the bare success string."""
    page(full=True)
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is True, r
    assert r["conversation_full"] is True
    assert r["reason"] == ("submitted_and_assistant_started_generating"
                           "+conversation_full_notice")


def test_the_notice_is_not_a_refusal_the_phrase_is_still_sent(page):
    """The capped chats kept answering; refusing on the notice would silence them."""
    s = page(full=True)
    cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert s.inserted == ["PHRASE"]


def test_a_notice_that_appears_after_the_send_is_caught(page):
    page(full=[False, True])
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r.get("conversation_full") is True, r


def test_a_failed_send_on_a_capped_chat_names_the_cap(page):
    """Composer cleared, no user turn, assistant never moved: on a capped chat the cap
    is the likely cause and the ledger must say so, still as a failure."""
    page(full=True, bools=[True, True, True] + [True] * 40, turns=[0] + [0] * 40,
         streaming=[0], assistant=[0])
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is False
    assert r["reason"] == "conversation_full:user_turn_not_observed_after_send"


def test_pre_send_reasons_are_never_rewritten(page):
    """submit_phrase's recovery and chat_registry match these exactly."""
    page(full=True, bools=[False] * 10)          # focus never confirmed
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is False
    assert not r["reason"].startswith("conversation_full"), r


def test_an_unreadable_notice_probe_changes_nothing(page):
    """FAIL-OPEN to the old path: None (page cannot answer) is not a notice."""
    page(full=None)
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is True
    assert r["reason"] == "submitted_and_assistant_started_generating"


def test_a_probe_that_raises_after_send_cannot_turn_success_into_cdp_error(page):
    s = page(full=False)
    calls = {"n": 0}
    real = s.boolean

    def boolean(expr):
        if "maximum length for this conversation" in expr:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("socket closed")
        return real(expr)
    s.boolean = boolean
    r = cdp.submit_phrase(URL, "PHRASE", claim=False)
    assert r["ok"] is True, r


def test_the_probe_reads_only_alert_chrome():
    """The notice is UI chrome: the expression scopes to [role=alert] and returns a
    boolean, never page text."""
    js = cdp._CONVERSATION_FULL_JS
    assert "[role=alert]" in js
    assert "innerText" not in js and "document.body" not in js
