"""`user_turn_not_observed_after_send` when the assistant visibly started.

After the 2026-10 UI change, 15 of 48 wake attempts since the companion restart ended
`user_turn_not_observed_after_send`, all on the two chats the single browser tab is
navigated between. The user-turn proof (count risen / newest id changed) is one signal;
the assistant starting a NEW turn is an independent one, and the verifier ignored it.
Back-pressure guarantees nothing was generating before the send, so the assistant
starting afterwards proves the backend accepted the message.

Loads the composer from THIS checkout (the older suites hard-code the live path).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from core import wake_bridge as wb

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_evidence", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

CHAT = "https://chatgpt.com/c/evidence-test-chat"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    wb.bind_chat(CHAT)
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    # the post-start settle window is wall-clock; it is not what these tests measure
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    yield


class Page:
    """A fake page after the 2026-10 UI change. The user-turn count stays FLAT and turns
    carry no readable id, so the user-turn proof can never fire; what the assistant side
    does is scripted per poll."""

    def __init__(self, *, stream=None, asst=None, asst_ids=None, user=None):
        # each list is the successive answers; the last value repeats
        self.seq = {"stream": list(stream or [0]), "asst": list(asst or [5]),
                    "asst_id": list(asst_ids or [""]), "user": list(user or [7])}

    def _next(self, k):
        q = self.seq[k]
        return q.pop(0) if len(q) > 1 else q[0]

    def call(self, method, params=None):
        return {}

    def boolean(self, e):
        e = e or ""
        if "readyState" in e or "__owComposer" in e:
            return True
        if getattr(cdp, "_STOP_BUTTON_SEL", "\0") in e:
            # the stop control: first answer is the pre-send back-pressure probe
            v = self._next("stream")
            return None if v < 0 else bool(v)
        if cdp.STREAMING_CONTENT_SEL in e:
            return False
        if "activeElement" in e:
            return True
        if "?.length" in e:
            return False                 # composer empty before insert
        if "endsWith" in e:
            return False
        if "length === 0" in e:
            return True                  # composer cleared after send (latch)
        if "length > 0" in e:
            return True
        if "aria-disabled" in e or cdp.SEND_SEL in e:
            return True
        if "dialog" in e.lower():
            return False
        return None

    def count(self, sel):
        if sel == cdp.USER_TURN_SEL:
            return self._next("user")
        if sel == cdp.STREAMING_SEL:
            return self._next("stream")
        if sel == cdp.ASSISTANT_TURN_SEL:
            return self._next("asst")
        if sel == cdp.COMPOSER_SEL:
            return 1
        return 1

    def last_len(self, sel):
        return 999

    def last_attr(self, sel, attr):
        if sel == cdp.ASSISTANT_TURN_SEL:
            return self._next("asst_id")
        return ""

    def close(self):
        pass


def _wire(monkeypatch, page):
    monkeypatch.setattr(cdp, "browser_degraded", lambda *a, **k: {"degraded": False})
    monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
    monkeypatch.setattr(cdp, "find_target", lambda url: {"webSocketDebuggerUrl": "ws://x"})
    monkeypatch.setattr(cdp, "_Session", lambda ws: page)
    monkeypatch.setattr(cdp, "composer_is_stable", lambda s: True, raising=False)
    monkeypatch.setattr(cdp, "focus_composer", lambda s: True, raising=False)


def _decide(eid):
    d = wb.should_wake(event_id=eid, severity="critical")
    wb.record(d, event_id=eid, severity="critical")


def _send(eid):
    _decide(eid)
    return cdp.submit_phrase(CHAT, "P", source="companion", event_id=eid)


def test_flat_user_turns_and_silent_assistant_still_fail_honestly(monkeypatch):
    """The control: no evidence on either side is still a failure, latched, not resent."""
    _wire(monkeypatch, Page())
    r = _send(301)
    assert r == {"ok": False, "reason": "user_turn_not_observed_after_send"}
    assert wb.was_submitted(301) is True


def test_stop_control_appearing_proves_the_turn_landed(monkeypatch):
    # baseline 0 (pre-send), 0, 0 during the first polls, then the stop control shows
    _wire(monkeypatch, Page(stream=[0, 0, 0, 1]))
    r = _send(302)
    assert r["ok"] is True, r
    assert r["reason"].startswith("submitted_and_assistant")
    assert wb.last_delivery(302)["delivered"] is True


def test_new_assistant_turn_proves_the_turn_landed(monkeypatch):
    _wire(monkeypatch, Page(asst=[5, 5, 6]))
    r = _send(303)
    assert r["ok"] is True and r["reason"].startswith("submitted_and_assistant"), r


def test_assistant_id_changing_under_a_flat_count_proves_it(monkeypatch):
    """Virtualized long chat: assistant count flat, newest assistant id changes."""
    _wire(monkeypatch, Page(asst_ids=["a1", "a1", "a2"]))
    r = _send(304)
    assert r["ok"] is True, r


def test_an_unknown_pre_send_stop_state_makes_the_control_no_evidence(monkeypatch):
    """Presence is not appearance: if the page could not say whether the stop control was
    up before our send, its being up afterwards proves nothing about our message."""
    _wire(monkeypatch, Page(stream=[-1, 1]))
    r = _send(305)
    assert r == {"ok": False, "reason": "user_turn_not_observed_after_send"}


def test_unreadable_assistant_side_is_no_evidence(monkeypatch):
    _wire(monkeypatch, Page(stream=[-1], asst=[-1], asst_ids=[None]))
    r = _send(306)
    assert r == {"ok": False, "reason": "user_turn_not_observed_after_send"}


def test_a_late_user_turn_inside_the_wider_window_is_seen(monkeypatch):
    """A re-rendering turn list after navigation: the turn shows on poll 15 — past the
    old fixed 10 s, inside USER_TURN_SECS."""
    assert cdp.USER_TURN_SECS >= 15
    _wire(monkeypatch, Page(user=[7] * 15 + [8], stream=[0] * 16 + [1]))
    r = _send(307)
    assert r["ok"] is True, r


def test_evidence_never_resends(monkeypatch):
    """Whatever the verdict, the phrase is inserted exactly once per attempt."""
    inserted = []
    page = Page(stream=[0, 0, 1])
    orig = page.call

    def call(method, params=None):
        if method == "Input.insertText":
            inserted.append((params or {}).get("text"))
        return orig(method, params)
    page.call = call
    _wire(monkeypatch, page)
    _send(308)
    assert inserted == ["P"]


def test_unknown_pre_send_state_voids_count_and_id_evidence_too(monkeypatch):
    """Review 2026-10-28: right after a navigation the turn list can still be mounting, so
    a risen assistant count or a re-keyed newest id is not ours unless the page could
    first say nothing was generating."""
    _wire(monkeypatch, Page(stream=[-1], asst=[5, 6], asst_ids=["a1", "a2"]))
    r = _send(309)
    assert r == {"ok": False, "reason": "user_turn_not_observed_after_send"}


@pytest.mark.parametrize("fresh, retry, expect", [
    (None, None, "tab_not_replaced"),
    ({"id": "FRESH"}, {"ok": False, "reason": "composer_ambiguous_or_absent:0"},
     "tab_replaced retry=composer_ambiguous_or_absent:0"),
    ({"id": "FRESH"}, {"ok": True, "reason": "submitted_and_assistant_started_generating"},
     "tab_replaced retry=submitted_and_assistant_started_generating"),
])
def test_absent_composer_recovery_outcome_is_logged(monkeypatch, capsys, fresh, retry, expect):
    """2026-10-28: only the final reason reaches wake_delivery, so a failed tab
    replacement and an absent composer on the FRESH tab were indistinguishable."""
    calls = []

    def attempt(*a, **k):
        calls.append(1)
        return ({"ok": False, "reason": "composer_ambiguous_or_absent:0"} if len(calls) == 1
                else dict(retry))
    monkeypatch.setattr(cdp, "_attempt", attempt)
    monkeypatch.setattr(cdp, "find_target", lambda u: {"id": "OLD"})
    monkeypatch.setattr(cdp, "recover_wedged_tab", lambda t, u: fresh)
    monkeypatch.setattr(cdp, "_record_delivery", lambda *a, **k: a[2])
    out = cdp.submit_phrase(CHAT, "P", claim=False, event_id=310)
    line = capsys.readouterr().out
    assert f"composer recovery for event 310: composer_ambiguous_or_absent:0 -> {expect}" in line
    assert "old=OLD" in line
    assert len(calls) == (2 if fresh else 1), "still exactly one retry"
    if fresh:
        assert out.get("after_composer_absent_recovery") is True
