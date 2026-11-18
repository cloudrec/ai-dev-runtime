"""A wake send whose outcome is ambiguous is never re-sent blindly, and never called
delivered without evidence (D1/D2, 2026-11-18 Full Cycle acceptance).

Event 88629: attempt 1 clicked send at ~07:15:00Z, then the page stopped answering CDP
(`cdp_error:WebSocketTimeoutException` at `post_send_verify`). The latch (`wake_submitted`)
is set only once the composer is SEEN cleared, so nothing recorded that the phrase may be
in the chat, and attempt 2 re-sent it at 07:21:30Z. 88652 and 88629 also show the other
half: a send that landed stays "not delivered" forever when the page is silent during
verification, because nothing ever looks again.

Every page here is a synthetic fake; no browser, chat or live DB is touched.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

os.environ.setdefault("WAKE_ASSISTANT_SETTLE_SECS", "0")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_composer_send_intent", os.path.join(_HERE, "tools", "cdp_composer.py"))
cdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdp)
_fspec = importlib.util.spec_from_file_location(
    "cdp_composer_fake_page_send_intent", os.path.join(_HERE, "tests", "test_cdp_composer.py"))
_fake = importlib.util.module_from_spec(_fspec)
_fspec.loader.exec_module(_fake)
_S = _fake._S

URL = "https://chatgpt.com/c/send-intent-chat"
TIMEOUT = "cdp_error:WebSocketTimeoutException"


class WebSocketTimeoutException(Exception):
    """Same class name as websocket-client's, so the recorded reason matches live."""


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cdp, "ASSISTANT_SETTLE_SECS", 0)
    monkeypatch.setattr(cdp, "POSTHOC_VERIFY_SECS", 0)
    monkeypatch.setattr(cdp, "browser_degraded",
                        lambda *a, **k: {"degraded": False, "reason": "ok", "pages": 3})
    monkeypatch.setattr(cdp, "page_responsive", lambda t, timeout=8.0: True)
    monkeypatch.setattr(cdp, "find_target",
                        lambda url: {"webSocketDebuggerUrl": "ws://x", "url": URL, "id": "t1"})
    from core import wake_bridge as wb
    monkeypatch.setattr(wb, "RECONCILE_MIN_AGE_SECS", 0, raising=False)
    monkeypatch.setattr(wb, "RECONCILE_INTERVAL_SECS", 0, raising=False)
    wb.bind_chat(URL)
    yield


@pytest.fixture
def wb():
    from core import wake_bridge as _w
    return _w


def _db():
    return sqlite3.connect(os.environ["CONTROL_PLANE_DB"])


def _state(eid):
    from core import wake_bridge as _w
    i = _w.send_intent(eid)
    return i and i["state"]


def _deliveries(eid):
    with _db() as c:
        return c.execute("SELECT delivered, reason, source FROM wake_delivery WHERE event_id=? "
                         "ORDER BY id", (eid,)).fetchall()


class _Page(_S):
    """A fake page that also answers the send-evidence probe (three booleans about our
    own marker), and can stop answering CDP at a chosen moment."""

    def __init__(self, *a, boom: str = "", evidence=None, **kw):
        super().__init__(*a, **kw)
        self.boom = boom
        self.clicked = False
        self.evidence = evidence
        self.intent_at_insert = None
        self.probed = []
        self.states_at_insert = {}

    def call(self, method, params=None):
        expr = (params or {}).get("expression", "")
        if method == "Input.insertText":
            try:
                self.intent_at_insert = _state(_EID[0])
                self.states_at_insert = {e: _state(e) for e in _WATCH}
            except AttributeError:            # a checkout without send intents
                self.intent_at_insert = None
            if self.boom == "insert":
                raise WebSocketTimeoutException("timed out")
        if method == "Runtime.evaluate" and ".click()" in expr:
            self.clicked = True
            if self.boom == "click":
                raise WebSocketTimeoutException("timed out")
        if method == "Runtime.evaluate" and "e.textContent=''" in expr:
            self.composer_cleared = True     # the draft guard cleared our stranded phrase
        if method == "Runtime.evaluate" and "compareDocumentPosition" in expr:
            ev = self.evidence
            if getattr(self, "composer_cleared", False) and isinstance(ev, dict):
                ev = {k: ({**v, "in_composer": False} if isinstance(v, dict) else v)
                      for k, v in ev.items()}
            if isinstance(ev, dict) and all(isinstance(k, int) for k in ev):
                # per event: the probe names its marker
                ev = next((v for k, v in ev.items()
                           if json.dumps(f"[Owner OS wake] event={k} ") in expr), None)
            self.probed.append(expr)
            if ev is None:
                return {}
            return {"result": {"value": json.dumps(ev)}}
        return super().call(method, params)

    def boolean(self, expression):
        if self.clicked and self.boom == "after_click":
            raise WebSocketTimeoutException("timed out")
        return super().boolean(expression)


_EID = [0]
_WATCH: list = []


def _wire(monkeypatch, *sessions):
    queue = list(sessions)
    opened = []

    def _session(ws):
        opened.append(ws)
        s = queue.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    monkeypatch.setattr(cdp, "_Session", _session)
    return opened


def _send(eid):
    _EID[0] = eid
    return cdp.submit_phrase(URL, f"[Owner OS wake] event={eid} trigger=x. PHRASE",
                             event_id=eid, claim=False)


def _hung(boom="after_click"):
    return _Page({"n": 1}, [True] * 6, boom=boom)


def _after(**evidence):
    """The page once it answers again: no new turn, nothing streaming, and the evidence."""
    ev = {"found": False, "answered": False, "in_composer": False, **evidence}
    return _Page({"n": 1}, [], turns=[0] * 4, streaming=[0], assistant=[0], evidence=ev)


# ── D1: the 88629 shape ─────────────────────────────────────────────────────────────────
def test_the_intent_is_durable_before_anything_is_typed(monkeypatch):
    page = _Page({"n": 1}, [True] * 6)
    _wire(monkeypatch, page)
    r = _send(101)
    assert r["ok"] is True, r
    assert page.intent_at_insert == "intent", "committed before Input.insertText"
    assert _state(101) == "delivered"


def test_a_timeout_after_the_click_with_a_silent_page_is_never_sent_again(monkeypatch, wb):
    """88629. Fails on the old code: nothing latched, so the next tick re-sent it."""
    _wire(monkeypatch, _hung(), WebSocketTimeoutException("still hung"))
    r = _send(88629)
    assert r == {"ok": False, "reason": TIMEOUT}, r
    assert wb.was_submitted(88629), "latched: every no-resend rule applies"
    second = _Page({"n": 1}, [True] * 6)
    _wire(monkeypatch, second)
    r2 = _send(88629)
    assert second.inserted == [], "nothing typed on the second attempt"
    assert r2["reason"].startswith("send_intent_refused:"), r2
    assert _state(88629) == "indeterminate"


def test_a_timeout_after_the_click_with_no_evidence_either_way_is_indeterminate(monkeypatch,
                                                                                 wb):
    """The page answers again, but shows neither our turn nor our phrase in the composer:
    the send may have been lost or not; it is NOT resent and NOT called delivered."""
    _wire(monkeypatch, _hung(), _after())
    r = _send(102)
    assert r["ok"] is False
    assert _state(102) == "indeterminate" and wb.was_submitted(102)
    assert all(d == 0 for d, _, _ in _deliveries(102)) or _deliveries(102) == []


def test_a_timeout_on_the_click_itself_is_indeterminate_not_unsent(monkeypatch, wb):
    """The click may have reached the page before the socket died."""
    _wire(monkeypatch, _hung("click"), WebSocketTimeoutException("still hung"))
    _send(103)
    assert _state(103) == "indeterminate" and wb.was_submitted(103)


def test_our_phrase_still_in_the_composer_proves_it_was_not_sent(monkeypatch, wb):
    """Sending empties the composer; the phrase still sitting there, in no user turn, is
    proof. Retryable, exactly once more, and the retry goes through."""
    _wire(monkeypatch, _hung(), _after(in_composer=True))
    r = _send(104)
    assert r == {"ok": False, "reason": TIMEOUT}, r
    assert _state(104) == "not_sent" and not wb.was_submitted(104)
    _wire(monkeypatch, _Page({"n": 1}, [True] * 6))
    assert _send(104)["ok"] is True
    assert _state(104) == "delivered"
    assert wb.send_intent(104)["attempts"] == 2


def test_our_phrase_in_the_composer_but_also_in_a_turn_is_not_unsent(monkeypatch, wb):
    _wire(monkeypatch, _hung(), _after(in_composer=True, found=True))
    _send(105)
    assert _state(105) == "indeterminate" and wb.was_submitted(105)


def test_a_timeout_before_the_click_is_proven_not_sent(monkeypatch, wb):
    _wire(monkeypatch, _hung("insert"))
    r = _send(106)
    assert r == {"ok": False, "reason": TIMEOUT}
    assert _state(106) == "not_sent" and not wb.was_submitted(106)


def test_a_composer_that_answered_still_full_is_proven_not_sent(monkeypatch, wb):
    page = _Page({"n": 1}, [True, True, True] + [False] * 40, turns=[0] + [1] * 40)
    _wire(monkeypatch, page)
    r = _send(107)
    assert r == {"ok": False, "reason": "composer_did_not_clear_after_send"}
    assert _state(107) == "not_sent"


def test_a_composer_that_never_answered_is_not_proof_of_anything(monkeypatch, wb):
    """`None` from the page used to read as "still holds text, provably not sent"."""
    page = _Page({"n": 1}, [True, True, True] + [None] * 40, turns=[0] + [1] * 40)
    _wire(monkeypatch, page)
    r = _send(108)
    assert r == {"ok": False, "reason": "composer_did_not_clear_after_send"}
    assert _state(108) == "indeterminate" and wb.was_submitted(108)


def test_a_result_dict_is_unchanged_for_callers(monkeypatch):
    _wire(monkeypatch, _Page({"n": 1}, [True] * 6))
    r = _send(109)
    assert set(r) <= {"ok", "reason", "conversation_full"}, r


# ── crash, restart, duplicate ───────────────────────────────────────────────────────────
def test_a_crash_after_the_intent_blocks_a_resend_and_is_made_indeterminate(monkeypatch, wb):
    """The process dies between the intent and any outcome (simulated: the intent is
    written and nothing else). A restarted companion must not send it again."""
    assert wb.begin_send(110, source="companion", conversation=URL)["ok"]
    page = _Page({"n": 1}, [True] * 6)
    _wire(monkeypatch, page)
    assert _send(110)["reason"] == "send_intent_refused:send_already_intent"
    assert page.inserted == []
    now = wb.send_intent(110)["ts"]
    assert wb.unresolved_sends(now=now + wb.INTENT_STALE_SECS - 1) == []
    due = wb.unresolved_sends(now=now + wb.INTENT_STALE_SECS + 1)
    assert [(d["event_id"], d["stale_converted"]) for d in due] == [(110, True)]
    assert _state(110) == "indeterminate" and wb.was_submitted(110)


def test_a_crashed_send_is_not_offered_again(monkeypatch, wb):
    d = wb.should_wake(event_id=111, severity="critical", now=1000.0)
    wb.record(d, event_id=111, severity="critical", now=1000.0)
    assert wb.pending_wake(now=1001.0)["event_id"] == 111
    wb.begin_send(111, source="companion", conversation=URL)
    assert wb.pending_wake(now=1002.0).get("event_id") != 111
    wb.finish_send(111, "not_sent", "proven")
    assert wb.pending_wake(now=1003.0)["event_id"] == 111, "only a proven not_sent returns"


def test_two_attempts_racing_for_one_event_send_once(wb):
    a = wb.begin_send(112, source="companion", conversation=URL)
    b = wb.begin_send(112, source="operator", conversation=URL)
    assert a["ok"] is True and b["ok"] is False


def test_a_latched_event_cannot_get_a_new_intent(wb):
    wb.mark_submitted(113, source="companion")
    assert wb.begin_send(113, conversation=URL)["reason"] == "send_already_latched"


def test_no_durable_record_means_no_send(monkeypatch, wb):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(wb, "begin_send", boom)
    page = _Page({"n": 1}, [True] * 6)
    _wire(monkeypatch, page)
    r = _send(114)
    assert r == {"ok": False, "reason": "send_intent_unavailable:OperationalError"}
    assert page.inserted == []


def test_moves_never_go_backwards(wb):
    wb.begin_send(115, conversation=URL)
    assert wb.finish_send(115, "delivered", "ok")
    assert not wb.finish_send(115, "not_sent", "x")
    assert not wb.finish_send(115, "indeterminate", "x")
    assert not wb.retract_unsent(115, "x")
    assert _state(115) == "delivered"


def test_a_send_seen_taken_is_never_not_sent(wb):
    wb.begin_send(116, conversation=URL)
    wb.mark_submitted(116, source="companion")       # composer seen cleared
    assert not wb.finish_send(116, "not_sent", "x")


# ── D2: reconciliation (late receipt) ───────────────────────────────────────────────────
def _indeterminate(monkeypatch, eid, actionable=True):
    from core import wake_bridge as wb_
    d = wb_
    dec = wb_.should_wake(event_id=eid, severity="high", now=1000.0)
    wb_.record({**dec, "actionable": actionable}, event_id=eid, severity="high",
               event_type="task_completed", agent_id="fc-acc:0.0", now=1000.0)
    _wire(monkeypatch, _hung(), WebSocketTimeoutException("still hung"))
    _send(eid)
    assert _state(eid) == "indeterminate"
    return d


def test_a_late_receipt_is_recorded_as_delivered(monkeypatch, wb):
    """The 88652 shape: the assistant did answer; the ledger now says so, with proof."""
    _indeterminate(monkeypatch, 120)
    from core import closed_loop_wake as clw
    clw.register_delivery(event_id=120, target="fc-acc:0.0", verified=False)
    _wire(monkeypatch, _after(found=True, answered=True))
    out = cdp.reconcile_sends()
    assert out == [{"event_id": 120, "verdict": "delivered", "converted": False}]
    assert _state(120) == "delivered"
    assert (1, "reconciled_assistant_answered", "reconcile") in _deliveries(120)
    with _db() as c:
        assert c.execute("SELECT acknowledged FROM wake_audit WHERE event_id=120 "
                         "AND decision='wake'").fetchone()[0] == 1
        assert c.execute("SELECT delivery_verified FROM wake_loop_watch "
                         "WHERE event_id=120").fetchone()[0] == 1


def test_a_landed_turn_without_an_answer_is_not_delivered(monkeypatch, wb):
    _indeterminate(monkeypatch, 121)
    _wire(monkeypatch, _after(found=True))
    assert cdp.reconcile_sends()[0]["verdict"] == "landed"
    assert _state(121) == "landed" and wb.was_submitted(121)
    assert not any(d for d, _, _ in _deliveries(121))
    _wire(monkeypatch, _after(found=True, answered=True))
    assert cdp.reconcile_sends()[0]["verdict"] == "delivered"


def test_reconciliation_can_prove_not_sent_and_reoffer_once(monkeypatch, wb):
    _indeterminate(monkeypatch, 122)
    _wire(monkeypatch, _after(in_composer=True))
    assert cdp.reconcile_sends()[0]["verdict"] == "not_sent"
    assert _state(122) == "not_sent" and not wb.was_submitted(122)
    assert wb.pending_wake(now=1001.0)["event_id"] == 122
    with _db() as c:
        moves = c.execute("SELECT from_state, to_state, reason FROM wake_send_intent_audit "
                          "WHERE event_id=122 ORDER BY id").fetchall()
    assert moves[-1] == ("indeterminate", "not_sent", "reconciled_phrase_still_in_composer")


@pytest.mark.parametrize("page", ["no_tab", "silent", "unreadable", "nowhere"])
def test_without_evidence_reconciliation_changes_nothing(monkeypatch, wb, page):
    _indeterminate(monkeypatch, 123)
    if page == "no_tab":
        monkeypatch.setattr(cdp, "find_target", lambda url: None)
    elif page == "silent":
        _wire(monkeypatch, WebSocketTimeoutException("hung"))
    elif page == "unreadable":
        _wire(monkeypatch, _Page({"n": 1}, [], evidence=None))
    else:
        _wire(monkeypatch, _after())
    out = cdp.reconcile_sends()
    assert out[0]["verdict"] in ("unchanged", "no_page")
    assert _state(123) == "indeterminate" and wb.was_submitted(123)
    assert not any(d for d, _, _ in _deliveries(123))


def test_unreadable_evidence_is_never_evidence():
    class P:
        def __init__(self, v):
            self.v = v

        def call(self, m, p=None):
            return {"result": {"value": self.v}}
    assert cdp.send_evidence(P(json.dumps({"found": 1, "answered": 0,
                                           "in_composer": 0})), 1) is None
    assert cdp.send_evidence(P("not json"), 1) is None
    assert cdp.send_evidence(P(None), 1) is None
    ok = {"found": True, "answered": False, "in_composer": False}
    assert cdp.send_evidence(P(json.dumps(ok)), 1) == ok


def test_the_probe_asks_only_about_our_own_marker():
    seen = []

    class P:
        def call(self, m, p=None):
            seen.append(p["expression"])
            return {}
    cdp.send_evidence(P(), 4242)
    assert json.dumps("[Owner OS wake] event=4242 ") in seen[0]
    assert "JSON.stringify({found:" in seen[0], "only three booleans come back"


def test_reconciliation_is_bounded(monkeypatch, wb):
    _indeterminate(monkeypatch, 124)
    monkeypatch.setattr(wb, "RECONCILE_MAX_LOOKS", 2)
    for _ in range(4):
        _wire(monkeypatch, _after())
        cdp.reconcile_sends()
    assert wb.send_intent(124)["looks"] == 2
    monkeypatch.setattr(wb, "RECONCILE_INTERVAL_SECS", 3600)
    assert wb.unresolved_sends() == []


def test_an_unresolved_send_still_ages_out_visibly(monkeypatch, wb):
    """Explicit unresolved-notification record: an indeterminate send that is never
    proven is abandoned with a reason, as every latched send is."""
    _indeterminate(monkeypatch, 125)
    out = wb.record_abandoned_wakes(now=1000.0 + wb.MAX_WAKE_AGE_SECS + 10)
    assert [o["event_id"] for o in out] == [125]
    assert out[0]["reason"] == "submitted_delivery_unproven"


def test_a_died_attempt_gets_its_watch_from_the_companion(monkeypatch, wb):
    """A crash skipped `tick`'s watch registration; reconciliation adds it, unverified."""
    import importlib
    import sys
    sys.path.insert(0, os.path.join(_HERE, "tools"))
    comp = importlib.import_module("wake_companion")
    dec = wb.should_wake(event_id=126, severity="high", now=1000.0)
    wb.record({**dec, "actionable": True}, event_id=126, severity="high",
              event_type="task_completed", agent_id="fc-acc:0.0", now=1000.0)
    wb.begin_send(126, source="companion", conversation=URL)
    with _db() as c:
        c.execute("UPDATE wake_send_intent SET ts=ts-? WHERE event_id=126",
                  (wb.INTENT_STALE_SECS + 5,))
    monkeypatch.setitem(sys.modules, "cdp_composer", cdp)
    _wire(monkeypatch, _after())
    out = comp.reconcile_sends(wb)
    assert out == [{"event_id": 126, "verdict": "unchanged", "converted": True}]
    with _db() as c:
        row = c.execute("SELECT target, delivery_verified FROM wake_loop_watch "
                        "WHERE event_id=126").fetchone()
    assert row == ("fc-acc:0.0", 0)


# ── reconciliation right before the next wake to the same chat ─────────────────────────
OTHER = "https://chatgpt.com/c/another-chat"


def _healthy(evidence=None):
    return _Page({"n": 1}, [True] * 6, evidence=evidence)


@pytest.fixture
def watch():
    _WATCH.clear()
    yield _WATCH
    _WATCH.clear()


def test_an_earlier_send_is_proven_before_the_next_wake_pushes_it_out(monkeypatch, wb,
                                                                       watch):
    """The 88803 shape: an indeterminate send, then the next wake to the same chat. The
    earlier one is settled on that page BEFORE the new phrase is typed."""
    _indeterminate(monkeypatch, 130)
    watch.append(130)
    page = _healthy({130: {"found": True, "answered": True, "in_composer": False}})
    _wire(monkeypatch, page)
    assert _send(131)["ok"] is True
    assert page.states_at_insert[130] == "delivered", "settled before typing"
    assert (1, "reconciled_assistant_answered", "reconcile_presend") in _deliveries(130)
    assert _state(131) == "delivered"


def test_a_stranded_phrase_is_proven_unsent_before_the_composer_guard_clears_it(
        monkeypatch, wb, watch):
    _indeterminate(monkeypatch, 132)
    watch.append(132)
    page = _healthy({132: {"found": False, "answered": False, "in_composer": True}})
    _wire(monkeypatch, page)
    assert _send(133)["ok"] is True
    assert page.states_at_insert[132] == "not_sent"
    assert not wb.was_submitted(132), "offered again, once, through every filter"


def test_a_landed_send_is_recorded_but_never_resent(monkeypatch, wb):
    _indeterminate(monkeypatch, 134)
    page = _healthy({134: {"found": True, "answered": False, "in_composer": False}})
    _wire(monkeypatch, page)
    _send(135)
    assert _state(134) == "landed" and wb.was_submitted(134)


def test_only_sends_to_the_same_chat_are_looked_at(monkeypatch, wb):
    _indeterminate(monkeypatch, 136)
    with _db() as c:
        c.execute("UPDATE wake_send_intent SET conversation=? WHERE event_id=136", (OTHER,))
    page = _healthy({136: {"found": True, "answered": True, "in_composer": False}})
    _wire(monkeypatch, page)
    _send(137)
    assert page.probed == [] and _state(136) == "indeterminate"


def test_the_event_being_sent_is_not_reconciled_against_itself(wb):
    wb.begin_send(138, conversation=URL)
    wb.finish_send(138, "indeterminate", "x")
    assert wb.unresolved_sends_in(URL, exclude=138) == []
    assert [u["event_id"] for u in wb.unresolved_sends_in(URL, exclude=139)] == [138]


def test_no_evidence_leaves_it_alone_and_the_new_wake_still_goes(monkeypatch, wb):
    _indeterminate(monkeypatch, 140)
    page = _healthy(None)                  # probe unreadable
    _wire(monkeypatch, page)
    assert _send(141)["ok"] is True
    assert _state(140) == "indeterminate" and len(page.probed) == 1
    assert wb.send_intent(140)["looks"] >= 1


def test_a_reconcile_failure_never_blocks_the_new_wake(monkeypatch, wb):
    _indeterminate(monkeypatch, 142)

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(wb, "unresolved_sends_in", boom)
    _wire(monkeypatch, _healthy())
    assert _send(143)["ok"] is True
    assert _state(142) == "indeterminate"


def test_the_presend_look_is_bounded(monkeypatch, wb):
    for e in (150, 151, 152, 153, 154):
        _indeterminate(monkeypatch, e)
    monkeypatch.setattr(cdp, "PRESEND_RECONCILE_LIMIT", 2)
    page = _healthy({})
    _wire(monkeypatch, page)
    _send(155)
    assert len(page.probed) == 2


def test_the_look_comes_before_the_draft_guard_clears_our_stranded_phrase(monkeypatch,
                                                                          wb, watch):
    """Order matters: the guard clears a stranded phrase of ours before typing. Looked at
    after that, the proof is gone and the earlier event stays indeterminate forever."""
    _indeterminate(monkeypatch, 160)
    watch.append(160)
    page = _healthy({160: {"found": False, "answered": False, "in_composer": True}})
    page.composer_dirty = True
    page.composer_is_ours = True
    _wire(monkeypatch, page)
    assert _send(161)["ok"] is True
    assert page.composer_cleared is True
    assert page.states_at_insert[160] == "not_sent"
