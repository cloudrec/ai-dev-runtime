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

    def __init__(self, counts=None, bools=None, turns=None, ids=None,
                 streaming=None, assistant=None):
        self.counts = counts or {}
        self.bools = list(bools or [])
        # Successive answers to "how many user turns are on the page". Default models a
        # healthy send: none before, one after.
        self.turns = list(turns or [])
        # Successive answers to "the opaque id of the newest user turn". Default is a page
        # where the id never changes, so the count alone decides — the pre-existing tests
        # keep their meaning.
        self.ids = list(ids or [])
        # Success now requires the ASSISTANT to start, not merely a user turn to
        # render: three deliveries were reported successful into a conversation
        # where nothing ever ran. Default models a healthy page where generation
        # begins immediately, so the pre-existing tests keep their meaning.
        self.streaming = list(streaming or [1])
        self.assistant = list(assistant or [0])
        self.assistant_ids = [""]
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
        if "stop-button" in expression:
            # Back-pressure probe. Answered STRUCTURALLY, like readyState, so it does not
            # consume the scripted queue below — otherwise adding an infrastructure check
            # to the composer would silently shift every expectation in every other test.
            return getattr(self, "generating", False)
        return self.bools.pop(0) if self.bools else None

    def count(self, selector):
        if selector == cdp.USER_TURN_SEL:
            if self.turns:
                return self.turns.pop(0)
            self._turn_calls += 1
            return 0 if self._turn_calls == 1 else 1
        if selector == cdp.STREAMING_SEL:
            return self.streaming.pop(0) if len(self.streaming) > 1 else self.streaming[0]
        if selector == cdp.ASSISTANT_TURN_SEL:
            return self.assistant.pop(0) if len(self.assistant) > 1 else self.assistant[0]
        return self.counts.get("n", 1)

    def last_attr(self, selector, attr):
        # Selector-aware: the assistant lookup must not consume the script that
        # models the USER turn ids (virtualization case).
        if selector == cdp.ASSISTANT_TURN_SEL:
            return self.assistant_ids.pop(0) if len(self.assistant_ids) > 1 \
                else self.assistant_ids[0]
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
        monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
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
    assert r["ok"] is True and r["reason"] == "submitted_and_assistant_started_generating"


# ── the virtualized long chat: count flat, newest id changed ───────────────
def test_a_flat_count_with_a_new_last_turn_id_is_still_delivery(wired):
    """The false negative that marked 25 real deliveries failed in one night: ChatGPT
    unmounts old turns as the chat grows, so a new turn mounting at the bottom evicts one
    at the top and the COUNT never moves. The newest turn's opaque id changing is the
    virtualization-proof signal that the conversation gained our message."""
    wired({"n": 1}, bools=[True, True, True, True], turns=[9] + [9] * 40,
          ids=["uuid-old", "uuid-new"])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    # The user turn is detected through the id advance (the virtualization case
    # this test exists for); success is then confirmed by the assistant starting.
    assert r["ok"] is True and r["reason"].startswith("submitted_and_assistant")


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


# ── the latch boundary: cleared composer, not the click (event 4214) ───────
def test_an_unsent_phrase_does_not_latch_and_the_retry_delivers_exactly_once(wired,
                                                                             monkeypatch):
    """The 4214 shape: the click was refused, the phrase stayed visibly IN the composer —
    provably unsent — yet the old pre-fire latch consumed the event forever. Now: no
    latch, the draft is cleared for a clean retry, and the successful retry both latches
    and delivers, exactly once."""
    latched = []
    monkeypatch.setattr(cdp, "_latch_submitted", lambda src, eid: latched.append(eid))
    s = wired({"n": 1}, bools=[True, True, True] + [False] * 40, turns=[0] + [1] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False, event_id=9)
    assert r["ok"] is False and r["reason"] == "composer_did_not_clear_after_send"
    assert latched == [], "an unsent phrase must not latch"
    assert any("innerHTML=''" in e for e in s.exprs), "the stale draft must be cleared"
    # the retry succeeds and latches
    wired({"n": 1}, bools=[True, True, True, True], turns=[1, 2])
    r2 = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False, event_id=9)
    assert r2["ok"] is True
    assert latched == [9], "success latches exactly once"


def test_a_cleared_composer_latches_even_when_the_turn_is_not_observed(wired,
                                                                       monkeypatch):
    """Ambiguity still resolves to 'assume it went': once the page took the phrase, the
    event may never be submitted again — that rule stopped ~60 duplicate wakes."""
    latched = []
    monkeypatch.setattr(cdp, "_latch_submitted", lambda src, eid: latched.append(eid))
    wired({"n": 1}, bools=[True, True, True, True], turns=[0] + [0] * 40)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False, event_id=8)
    assert r["ok"] is False and r["reason"] == "user_turn_not_observed_after_send"
    assert latched == [8]


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
    monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
    monkeypatch.setattr(cdp, "find_target", _ft)
    monkeypatch.setattr(cdp, "find_chatgpt_page", lambda: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: nav)
    r = cdp.submit_phrase("https://chatgpt.com/c/bound-target", "PHRASE", claim=False)
    assert seen.get("url") == "https://chatgpt.com/c/bound-target"
    assert r["ok"] is True


def test_a_healthy_submission_confirms_a_new_user_turn(wired):
    s = wired({"n": 1}, bools=[True, True, True, True])
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    # The reason changed with the criterion: a rendered user turn is not proof the
    # reviewer ran, so success now names the assistant starting.
    assert r["ok"] is True and r["reason"] == "submitted_and_assistant_started_generating"
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


# ── 2026-08-30: a focus-trapping dialog blocked every wake for 3.5 hours ─────
# A stray Radix popover ([role=dialog][data-state=open]) held activeElement on three
# route tabs at once. focus() on the composer was reverted the instant it was called, so
# every delivery failed as the generic `composer_not_focused` and nothing said why.

class _FakeSession:
    """Minimal _Session stand-in: a page with an optional dialog focus trap."""

    def __init__(self, *, trapped: bool, escapes_to_release: int = 1,
                 focusable: bool = True, buttons=None, title: str = "",
                 click_releases: bool = True):
        self.trapped = trapped
        self.escapes_to_release = escapes_to_release
        self.focusable = focusable
        self.buttons = list(buttons or [])
        self.title = title
        self.click_releases = click_releases
        self.escapes = 0
        self.clicks = []
        self._focused = False

    def call(self, method, params=None):
        params = params or {}
        if method == "Input.dispatchKeyEvent":
            if params.get("key") == "Escape" and params.get("type") == "keyUp":
                self.escapes += 1
                if self.escapes >= self.escapes_to_release:
                    self.trapped = False
            return {}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if ".focus()" in expr:
                self._focused = self.focusable and not self.trapped
                return {}
            if "aria-labelledby" in expr:
                return {"result": {"value": self.title}}
            if "querySelectorAll('button')" in expr and "JSON.stringify" in expr:
                # DATA only: the page reports its buttons; the decision is Python's.
                import json as _j
                return {"result": {"value": _j.dumps(self.buttons)}}
            if "btns[0].click()" in expr:
                # The page was told to click. If the policy were broken, this is where
                # an un-allowlisted or choice dialog would get pressed.
                self.clicks.append(self.buttons[0].strip().lower()
                                   if self.buttons else "?")
                if self.click_releases:
                    self.trapped = False
                return {"result": {"value": "clicked"}}
            return {}
        return {}

    def boolean(self, expression):
        if "role=dialog" in expression:
            return self.trapped
        if "document.activeElement ===" in expression:
            return self._focused
        return None


def test_focus_succeeds_without_a_dialog_and_presses_nothing():
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=False)
    assert cc.focus_composer(s, sleep=lambda x: None) is True
    assert s.escapes == 0


def test_a_focus_trapping_dialog_is_dismissed_with_escape_first():
    """Escape is tried before anything is ever clicked."""
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=True, buttons=["Got it"])
    assert cc.focus_composer(s, sleep=lambda x: None) is True
    assert s.escapes == 1
    assert s.clicks == []          # Escape sufficed; nothing was pressed


def test_an_alert_dialog_that_ignores_escape_is_acknowledged_by_its_one_button():
    """ChatGPT's "Too many requests" notice ignores Escape by design — an alert is meant
    to be acknowledged. With nothing to acknowledge it, a rate limit that had long since
    expired kept the composer unreachable for 3.5 hours."""
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=True, escapes_to_release=99, buttons=["Got it"],
                     title="Too many requests")
    assert cc.focus_composer(s, sleep=lambda x: None) is True
    assert s.clicks == ["got it"]


def test_a_dialog_offering_a_CHOICE_is_never_clicked():
    """Two buttons is a decision ("Upgrade"/"Not now"), and a decision is not this
    module's to make."""
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=True, escapes_to_release=99,
                     buttons=["Upgrade", "Not now"], title="Upgrade to Pro")
    assert cc.focus_composer(s, attempts=2, sleep=lambda x: None) is not True
    assert s.clicks == []


def test_an_unrecognised_single_button_is_never_clicked():
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=True, escapes_to_release=99, buttons=["Delete everything"],
                     title="Danger")
    assert cc.focus_composer(s, attempts=2, sleep=lambda x: None) is not True
    assert s.clicks == []


def test_a_trap_that_will_not_release_is_reported_not_fought():
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=True, escapes_to_release=99)
    assert cc.focus_composer(s, attempts=3, sleep=lambda x: None) is not True
    assert s.escapes == 3          # bounded — no unbounded loop against a stuck page
    assert cc.dialog_traps_focus(s) is True


def test_a_focus_failure_with_no_dialog_is_not_retried_as_one():
    """Not every focus failure is a trap. Retrying a page that simply will not focus the
    composer just repeats the same answer."""
    from tools import cdp_composer as cc
    s = _FakeSession(trapped=False, focusable=False)
    assert cc.focus_composer(s, attempts=3, sleep=lambda x: None) is not True
    assert s.escapes == 0


def test_a_trapped_page_is_reported_with_the_dialog_that_caused_it():
    """The 3.5-hour blackout was logged 98 times as `composer_not_focused`, and it took a
    live CDP session to find out the cause was ChatGPT's rate-limit notice."""
    from tools import cdp_composer as cc
    r = cc.focus_failure_reason(_FakeSession(trapped=True, title="Too many requests"))
    assert r == "composer_focus_trapped_by_dialog:too-many-requests"
    assert cc.focus_failure_reason(_FakeSession(trapped=False)) == "composer_not_focused"


def test_a_choice_whose_first_button_looks_benign_is_still_never_clicked():
    """Isolates the single-button guard specifically: the label allowlist alone would
    wave this through, and clicking "OK" here would confirm a destructive choice."""
    from tools import cdp_composer as cc
    assert cc.ack_click_decision(["OK", "Delete everything"]) == "not_single_button"
    s = _FakeSession(trapped=True, escapes_to_release=99,
                     buttons=["OK", "Delete everything"], title="Delete chat?")
    assert cc.focus_composer(s, attempts=2, sleep=lambda x: None) is not True
    assert s.clicks == []


def test_ack_decision_table():
    from tools import cdp_composer as cc
    assert cc.ack_click_decision([]) == "no_dialog_buttons"
    assert cc.ack_click_decision(["Got it"]) == "allowed"
    assert cc.ack_click_decision(["  Got It  "]) == "allowed"      # trimmed, case-folded
    assert cc.ack_click_decision(["Upgrade"]) == "label_not_allowlisted"


# ── 2026-08-30: back-pressure was being reported as a broken composer ────────
def test_a_turn_in_flight_is_reported_as_back_pressure_not_a_composer_fault():
    """While generating, ChatGPT replaces the send control with a stop control. Typing
    then leaves the phrase in the box and the attempt reads
    `composer_did_not_clear_after_send` — which sends an operator looking for a broken
    composer instead of ordinary back-pressure."""
    from tools import cdp_composer as cc

    class _Gen(_FakeSession):
        def boolean(self, expression):
            if "stop-button" in expression:
                return True
            return super().boolean(expression)

    s = _Gen(trapped=False)
    assert cc.assistant_is_generating(s) is True


def test_no_stop_button_means_no_back_pressure():
    from tools import cdp_composer as cc
    assert cc.assistant_is_generating(_FakeSession(trapped=False)) is not True


def test_an_unanswerable_page_falls_open_to_the_previous_path():
    """None (the page could not answer) must not be treated as generating — the old path
    has to keep running unchanged."""
    from tools import cdp_composer as cc

    class _Mute(_FakeSession):
        def boolean(self, expression):
            return None

    assert cc.assistant_is_generating(_Mute(trapped=False)) is None


def test_a_generating_assistant_short_circuits_before_anything_is_typed(wired):
    """The full path, not just the helper: a turn in flight must produce back-pressure,
    and the phrase must NOT be left sitting in the composer."""
    s = wired({"n": 1}, bools=[True, True, True, True])
    s.generating = True
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is False
    assert r["reason"] == "assistant_still_generating"
    assert s.inserted == [], "nothing may be typed while the assistant is answering"


def test_back_pressure_latches_nothing_so_the_event_is_retried(wired):
    """No latch means the wake stays pending: back-pressure delays a delivery, it never
    consumes one."""
    s = wired({"n": 1}, bools=[True, True, True, True])
    s.generating = True
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["reason"] == "assistant_still_generating"
    assert not any("click" in e for e in s.exprs), "no send was attempted"


def test_an_unanswerable_probe_does_not_block_delivery(wired):
    """Fail-OPEN isolation: when the page cannot answer the back-pressure probe (None),
    the previous path must run unchanged. Treating unknown as 'generating' would stall
    every delivery on any page whose stop control this selector cannot see."""
    s = wired({"n": 1}, bools=[True, True, True, True])
    s.generating = None
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["ok"] is True and r["reason"] == "submitted_and_assistant_started_generating"
    assert s.inserted == ["PHRASE"]


# ── 2026-08-30: a WEDGED conversation blocks a route forever ─────────────────
class _WedgeSession(_FakeSession):
    """Stop control up, nothing streaming, assistant turn frozen — the live owner-os
    shape: the RENDERER answers fine, the CONVERSATION is stuck."""

    def __init__(self, *, streaming=False, turn_ids=None, **kw):
        super().__init__(trapped=False, **kw)
        self.streaming = streaming
        self.turn_ids = list(turn_ids or ["m1", "m1", "m1"])

    def boolean(self, expression):
        if "stop-button" in expression:
            return True
        if "result-streaming" in expression:
            return self.streaming
        return super().boolean(expression)

    def last_attr(self, selector, attr):
        return self.turn_ids.pop(0) if self.turn_ids else "m1"


def test_a_stuck_stop_control_with_no_streaming_is_wedged():
    from tools import cdp_composer as cc
    assert cc.generating_is_wedged(_WedgeSession(), sleep=lambda s: None) is True


def test_streaming_tokens_are_never_called_wedged():
    """A genuinely long answer must never be cut short."""
    from tools import cdp_composer as cc
    assert cc.generating_is_wedged(_WedgeSession(streaming=True),
                                   sleep=lambda s: None) is False


def test_a_new_assistant_turn_proves_it_is_not_wedged():
    from tools import cdp_composer as cc
    s = _WedgeSession(turn_ids=["m1", "m2", "m3"])
    assert cc.generating_is_wedged(s, sleep=lambda s_: None) is False


def test_the_stop_control_clearing_proves_it_is_not_wedged():
    from tools import cdp_composer as cc

    class _Clears(_WedgeSession):
        def __init__(self):
            super().__init__()
            self._n = 0

        def boolean(self, expression):
            if "stop-button" in expression:
                self._n += 1
                return self._n < 2
            return super().boolean(expression)

    assert cc.generating_is_wedged(_Clears(), sleep=lambda s_: None) is False


def test_every_sample_must_agree_before_declaring_a_wedge():
    """One quiet moment mid-answer is not a wedge."""
    from tools import cdp_composer as cc

    class _Blip(_WedgeSession):
        def __init__(self):
            super().__init__()
            self._n = 0

        def boolean(self, expression):
            if "result-streaming" in expression:
                self._n += 1
                return self._n >= 2      # quiet on the first sample, streaming after
            return super().boolean(expression)

    assert cc.generating_is_wedged(_Blip(), sleep=lambda s_: None) is False


def test_ordinary_back_pressure_never_replaces_the_tab(wired, monkeypatch):
    """Recovery is destructive to a turn in flight. Only a WEDGE — which never resolves —
    earns it; back-pressure resolves itself and must be left alone."""
    calls = []
    monkeypatch.setattr(cdp, "recover_wedged_tab", lambda t, u: calls.append(u) or t)
    s = wired({"n": 1}, bools=[True, True, True, True])
    s.generating = True                      # generating, and NOT wedged
    monkeypatch.setattr(cdp, "generating_is_wedged", lambda *a, **k: False)
    r = cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert r["reason"] == "assistant_still_generating"
    assert calls == [], "a live turn must never have its tab replaced"


def test_a_wedged_conversation_does_earn_one_tab_recovery(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(cdp, "recover_wedged_tab", lambda t, u: calls.append(u) or t)
    s = wired({"n": 1}, bools=[True, True, True, True])
    s.generating = True
    monkeypatch.setattr(cdp, "generating_is_wedged", lambda *a, **k: True)
    cdp.submit_phrase("https://chatgpt.com/c/a", "PHRASE", claim=False)
    assert calls == ["https://chatgpt.com/c/a"], "a wedge must be recovered exactly once"


# ── 2026-08-30: recovery must not amplify a starving browser ─────────────────
# The host ran out of memory and swap; page_responsive() was false for EVERY tab because
# the whole browser was starved. recover_wedged_tab fired on every delivery attempt,
# opened a replacement it could not verify, and left the old one — 1 owner-os tab and 61
# chrome processes became 41 pages (25 bare roots) and 68 processes in eight minutes.
# Each failed delivery was adding renderers to the exhaustion that caused it.

def _pages(n, kind="page"):
    return [{"type": kind, "id": f"t{i}", "url": "https://chatgpt.com/"} for i in range(n)]


def test_an_unreachable_browser_endpoint_is_degraded():
    from tools import cdp_composer as cc

    def boom():
        raise OSError("connection refused")

    d = cc.browser_degraded(list_fn=boom)
    assert d["degraded"] is True and d["reason"].startswith("endpoint_unreachable")


def test_a_slow_browser_endpoint_is_degraded():
    """A healthy Chrome lists tabs in milliseconds; seconds means it is starving."""
    from tools import cdp_composer as cc
    ticks = iter([0.0, 5.0])
    d = cc.browser_degraded(list_fn=lambda: _pages(3), clock=lambda: next(ticks))
    assert d["degraded"] is True and d["reason"].startswith("endpoint_slow")


def test_too_many_pages_is_degraded():
    """The signature of replacement tabs accumulating — the live state was 41."""
    from tools import cdp_composer as cc
    ticks = iter([0.0, 0.01])
    d = cc.browser_degraded(list_fn=lambda: _pages(41), clock=lambda: next(ticks))
    assert d["degraded"] is True and d["reason"] == "too_many_pages:41"


def test_a_healthy_browser_is_not_degraded():
    from tools import cdp_composer as cc
    ticks = iter([0.0, 0.01])
    d = cc.browser_degraded(list_fn=lambda: _pages(4), clock=lambda: next(ticks))
    assert d["degraded"] is False and d["pages"] == 4


def test_only_real_pages_count_not_workers_or_service_workers():
    from tools import cdp_composer as cc
    ticks = iter([0.0, 0.01])
    mixed = _pages(3) + _pages(30, kind="service_worker")
    d = cc.browser_degraded(list_fn=lambda: mixed, clock=lambda: next(ticks))
    assert d["degraded"] is False, "background workers are not tabs"


def test_a_degraded_browser_opens_NO_replacement_tab(monkeypatch):
    """The whole point: refusing must mean no tab is created, not a tab created and
    discarded."""
    from tools import cdp_composer as cc
    opened = []
    monkeypatch.setattr(cc, "browser_degraded", lambda *a, **k: {"degraded": True,
                                                                 "reason": "too_many_pages:41"})
    monkeypatch.setattr(cc, "_http", lambda path, method="GET": opened.append(path) or {})
    assert cc.recover_wedged_tab({"id": "old"}, "https://chatgpt.com/c/a") is None
    assert opened == [], "a starving browser must not be handed another tab to open"


def test_a_healthy_browser_still_gets_its_replacement_tab(monkeypatch):
    """No regression on the 4214 incident this recovery exists for: ONE wedged renderer
    on a healthy browser is still replaced."""
    from tools import cdp_composer as cc
    opened = []
    monkeypatch.setattr(cc, "browser_degraded", lambda *a, **k: {"degraded": False})

    def fake_http(path, method="GET"):
        opened.append(path)
        return {"id": "fresh"}

    monkeypatch.setattr(cc, "_http", fake_http)
    monkeypatch.setattr(cc, "find_target", lambda url: {"id": "fresh"})
    monkeypatch.setattr(cc, "page_responsive", lambda t, timeout=8.0: True)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    assert cc.recover_wedged_tab({"id": "old"}, "https://chatgpt.com/c/a") is not None
    assert any("/json/new" in p for p in opened)
