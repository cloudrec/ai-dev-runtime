"""A CDP timeout after the send is not proof the wake was lost.

2026-10-28/29, route `pl`: events 74724, 74760, 74872 and 74896 ended
`cdp_error:WebSocketTimeoutException` while ChatGPT, busy starting its answer, stopped
answering CDP. ChatGPT then sent its next agent command for each — the wakes had landed —
but the ledger said "not delivered": three went unacknowledged, and 74760 (exception
before the composer-cleared latch) was offered again and reached the chat twice.

Loads the composer from THIS checkout (the older suites hard-code the live path).
"""
from __future__ import annotations

import importlib.util
import os

import pytest

os.environ.setdefault("WAKE_ASSISTANT_SETTLE_SECS", "0")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_posthoc", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)

# The shared fake page, loaded by PATH (other suites put the live checkout first).
_fspec = importlib.util.spec_from_file_location(
    "cdp_composer_fake_page_posthoc", os.path.join(_HERE, "tests", "test_cdp_composer.py"))
_fake = importlib.util.module_from_spec(_fspec)
_fspec.loader.exec_module(_fake)
_S = _fake._S

URL = "https://chatgpt.com/c/gaika-pl-chat"
REASON = "cdp_error:WebSocketTimeoutException"


class WebSocketTimeoutException(Exception):
    """Same class name as websocket-client's, so the recorded reason matches live."""


class _Hung(_S):
    """The attempt's own session: healthy until the send is dispatched, then the
    renderer stops answering on the `boom` probe."""

    def __init__(self, boom: str, **kw):
        super().__init__({"n": 1}, [True] * 6, **kw)
        self.boom = boom
        self.clicked = False

    def call(self, method, params=None):
        if method == "Input.insertText" and self.boom == "insert":
            raise WebSocketTimeoutException("timed out")
        if method == "Runtime.evaluate" and ".click()" in (params or {}).get("expression", ""):
            self.clicked = True
        return super().call(method, params)

    def boolean(self, expression):
        if self.clicked and self.boom == "boolean":
            raise WebSocketTimeoutException("timed out")
        return super().boolean(expression)

    def count(self, selector):
        if self.clicked and self.boom == "count" and selector == cdp.USER_TURN_SEL:
            raise WebSocketTimeoutException("timed out")
        return super().count(selector)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    # the send intent is durable: each test gets its own control-plane DB
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    monkeypatch.setattr(cdp, "POSTHOC_VERIFY_SECS", 0)
    monkeypatch.setattr(cdp, "_record_delivery", lambda src, eid, res, **k: res)


@pytest.fixture
def wire(monkeypatch):
    def _mk(first, second):
        latched, opened = [], []
        sessions = [first, second]

        def _session(ws):
            opened.append(ws)
            s = sessions.pop(0)
            if isinstance(s, Exception):
                raise s
            return s

        monkeypatch.setattr(cdp, "_latch_submitted",
                            lambda source, event_id: latched.append(event_id))
        monkeypatch.setattr(cdp, "browser_degraded",
                            lambda *a, **k: {"degraded": False, "reason": "ok", "pages": 3})
        monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
        monkeypatch.setattr(cdp, "find_target",
                            lambda url: {"webSocketDebuggerUrl": "ws://x", "url": URL,
                                         "id": "t1"})
        monkeypatch.setattr(cdp, "_Session", _session)
        return latched, opened
    return _mk


def _after(turns=0, assistant=0):
    """The fresh post-hoc session: what the page shows once it answers again."""
    s = _S({"n": 1}, [], turns=[turns] * 4, streaming=[1], assistant=[assistant])
    return s


def test_timeout_before_latch_with_the_assistant_started_is_a_delivery(wire):
    """The 74760 shape. Fails without the fix: `ok` False, nothing latched, so the
    event is offered again into a chat that already answered it."""
    latched, opened = wire(_Hung("boolean"), _after(turns=1, assistant=1))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=74760, claim=False)
    assert r["ok"] is True, r
    assert r["reason"] == f"submitted_and_assistant_started_after_{REASON}"
    assert latched == [74760]
    assert len(opened) == 2, "a fresh session must have looked"


def test_timeout_after_latch_with_the_assistant_started_is_a_delivery(wire):
    """The 74724/74872/74896 shape: latched, then the verification probe hung."""
    latched, _ = wire(_Hung("count"), _after(turns=1, assistant=1))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=74896, claim=False)
    assert r["ok"] is True, r
    assert r["reason"].endswith(REASON)
    assert 74896 in latched


def test_a_landed_user_turn_without_an_answer_latches_but_is_not_a_delivery(wire):
    """In the chat, so never resent; but the assistant never started, so not proven."""
    latched, _ = wire(_Hung("boolean"), _after(turns=1, assistant=0))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=7, claim=False)
    assert r == {"ok": False, "reason": REASON}
    assert latched == [7]


def test_a_virtualized_chat_proves_the_turn_by_id(wire):
    """Flat count, newest user-turn id changed — the same proof the main path uses."""
    first = _Hung("boolean", ids=["u-old"])
    after = _S({"n": 1}, [], turns=[0] * 4, ids=["u-new"] * 4, streaming=[1], assistant=[0])
    latched, _ = wire(first, after)
    r = cdp.submit_phrase(URL, "PHRASE", event_id=8, claim=False)
    assert r["ok"] is False and latched == [8]


def test_no_evidence_after_dispatch_is_indeterminate_and_never_resent(wire):
    """Changed 2026-11-18 (D1, event 88629). This used to stay retryable, and the retry
    re-sent a wake that had in fact been clicked. Dispatched with no proof either way is
    now indeterminate: latched, never re-sent blindly, settled later by reconciliation.
    The reason is unchanged; the attempt's own composer-cleared latch was not reached."""
    latched, _ = wire(_Hung("boolean"), _after(turns=0, assistant=0))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=9, claim=False)
    assert r == {"ok": False, "reason": REASON}
    assert latched == []
    wb = cdp._wb()
    assert wb.send_intent(9)["state"] == "indeterminate" and wb.was_submitted(9)


def test_a_page_that_still_does_not_answer_is_indeterminate(wire):
    latched, _ = wire(_Hung("boolean"), WebSocketTimeoutException("still hung"))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=10, claim=False)
    assert r == {"ok": False, "reason": REASON}
    wb = cdp._wb()
    assert wb.send_intent(10)["state"] == "indeterminate" and wb.was_submitted(10)


def test_an_existing_latch_is_never_undone_by_a_failed_recheck(wire):
    latched, _ = wire(_Hung("count"), WebSocketTimeoutException("still hung"))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=11, claim=False)
    assert r == {"ok": False, "reason": REASON}
    assert latched == [11], "the attempt's own latch stands"


def test_a_timeout_before_the_send_never_opens_a_recheck(wire):
    """Nothing was dispatched, so nothing can have landed: no second session, no latch,
    and the reason is byte-identical to before."""
    latched, opened = wire(_Hung("insert"), _after(turns=1, assistant=1))
    r = cdp.submit_phrase(URL, "PHRASE", event_id=12, claim=False)
    assert r == {"ok": False, "reason": REASON}
    assert latched == [] and len(opened) == 1


def test_an_empty_answer_after_the_timeout_is_still_a_failure(wire):
    """The recheck goes through the same settle check as the ordinary path."""
    after = _S({"n": 1}, [], turns=[1] * 4, streaming=[0], assistant=[1])
    after.assistant_len = 0
    latched, _ = wire(_Hung("boolean"), after)
    r = cdp.submit_phrase(URL, "PHRASE", event_id=13, claim=False)
    assert r["ok"] is False
    assert r["reason"].startswith("assistant_started_but_produced_nothing")
    assert latched == [13]



def test_a_pre_send_timeout_names_the_step_and_keeps_the_reason(wire, capsys):
    """2026-10-29..31: 35 `cdp_error:WebSocketTimeoutException` failures on the shared
    pl/gaika chat recorded no step, so where the page stopped answering was unknowable.
    The result must stay byte-identical (callers match it exactly); the step goes to the
    companion's journal."""
    latched, _ = wire(_Hung("insert"), _after())
    r = cdp.submit_phrase(URL, "PHRASE", event_id=76362, claim=False)
    assert r == {"ok": False, "reason": REASON}
    assert latched == []
    assert f"cdp {REASON} at step insert_phrase (event 76362" in capsys.readouterr().out


def test_a_post_send_timeout_names_the_verify_step(wire, capsys):
    wire(_Hung("boolean"), _after(turns=1, assistant=1))
    cdp.submit_phrase(URL, "PHRASE", event_id=76390, claim=False)
    assert "at step post_send_verify (event 76390" in capsys.readouterr().out
