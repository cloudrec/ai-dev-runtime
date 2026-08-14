"""CDP composer locator — structure only, never content.

Every expression evaluated here returns a BOOLEAN, a COUNT, or an OPAQUE message-id
attribute (`data-message-id`, a UUID the page assigns — metadata, not content). Nothing
returns message text, chat titles, history, or any other page content, and nothing is
logged beyond those values. The one string ever sent into the page is the fixed wake phrase.

Why CDP at all: AT-SPI does not expose Chrome's tree in this headless setup (Chrome registers
on the a11y bus but reports childCount = -1), and clicking a hardcoded pixel silently failed —
three "successful" wakes had no verification behind them. CDP is used strictly to answer
structural questions: does exactly one composer exist, is it focused, is the send button
present and enabled.

Fail closed everywhere: ambiguity (zero or several matches) is a refusal, never a guess.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

# Structural selectors: element identity and ARIA attributes only. No text, no message nodes.
COMPOSER_SEL = "#prompt-textarea, div[contenteditable='true'][id='prompt-textarea']"
# ChatGPT renders the send control only once the composer is non-empty, so it is looked for
# AFTER the phrase is inserted, never before. Structural attributes only — no label text
# matching, which would break the moment the UI is in another language.
SEND_SEL = ("button[data-testid='send-button'], form button[type='submit'], "
            "[data-testid*='send']")
# The message the page decided to keep. A cleared composer only proves the page ACCEPTED the
# keystrokes locally — it is emptied optimistically, before the turn is committed, so three
# earlier "deliveries" cleared the composer and left no message behind. The turn node is the
# first structure that exists only once the conversation actually gained the message, so
# delivery is judged by its COUNT rising, never by the composer emptying.
USER_TURN_SEL = "[data-message-author-role='user']"


def _http(path: str):
    with urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}{path}", timeout=8) as r:
        return json.loads(r.read().decode())


def find_target(conversation_url: str) -> Optional[dict]:
    """The page whose URL is the bound conversation.

    Matching is on the URL — the rotatable pointer the owner set — which is deliberately the
    only page-derived string this module ever compares. Titles are ignored precisely because
    a chat title is content.
    """
    want = (conversation_url or "").rstrip("/")
    for t in _http("/json/list"):
        if t.get("type") != "page":
            continue
        if (t.get("url") or "").rstrip("/").startswith(want):
            return t
    return None


def find_chatgpt_page() -> Optional[dict]:
    """Any open ChatGPT page, matched on host only — never on title or content."""
    for t in _http("/json/list"):
        if t.get("type") != "page":
            continue
        u = t.get("url") or ""
        if u.startswith("https://chatgpt.com") or u.startswith("https://chat.openai.com"):
            return t
    return None


class _Session:
    """Minimal CDP client. Sends commands, returns only what the caller asks for."""

    def __init__(self, ws_url: str):
        import websocket
        # Chrome rejects a WebSocket carrying an Origin header (403). Suppressing the header
        # client-side is preferable to loosening Chrome with --remote-allow-origins, which
        # would widen what may connect to a browser holding an authenticated session.
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result") or {}

    def boolean(self, expression: str) -> Optional[bool]:
        """Evaluate an expression that MUST yield a boolean. Anything else is refused."""
        r = self.call("Runtime.evaluate",
                      {"expression": expression, "returnByValue": True})
        val = ((r.get("result") or {}).get("value"))
        return val if isinstance(val, bool) else None

    def count(self, selector: str) -> int:
        r = self.call("Runtime.evaluate",
                      {"expression": f"document.querySelectorAll({selector!r}).length",
                       "returnByValue": True})
        v = ((r.get("result") or {}).get("value"))
        return int(v) if isinstance(v, (int, float)) else -1

    def last_attr(self, selector: str, attr: str) -> Optional[str]:
        """The named ATTRIBUTE of the last element matching the selector — an opaque id,
        never text. Returns None when unreadable, "" when absent; both fail closed at the
        caller, which must then rely on the count alone."""
        expr = (f"(function(){{const n=document.querySelectorAll({selector!r});"
                f"if(!n.length)return '';"
                f"return n[n.length-1].getAttribute({attr!r})||'';}})()")
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        v = ((r.get("result") or {}).get("value"))
        return v if isinstance(v, str) else None

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _record_delivery(source: str, event_id: Optional[int], res: dict,
                     conversation: str = "") -> dict:
    """Persist the outcome of an attempt — the failures above all, since those are the ones
    that must stay unacknowledged and be retried. The conversation the attempt resolved to
    is recorded alongside, so "which chat did this send go to" is answerable from state."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/root/ai-dev-runtime")
        from core import wake_bridge as _wb
        _wb.record_delivery(source, event_id=event_id, delivered=bool(res.get("ok")),
                            reason=str(res.get("reason", "")), conversation=conversation)
    except Exception:  # noqa: BLE001 — a missing recorder must never turn into a false success
        pass
    return res


def submit_phrase(conversation_url: str, phrase: str, *, source: str = "unknown",
                  event_id: Optional[int] = None, claim: bool = True,
                  actionable: bool = False) -> dict:
    """Locate the composer structurally, verify it, insert the phrase, send, verify DELIVERY.

    Returns {"ok": bool, "reason": str}. Every refusal names its cause so a silent failure is
    impossible — the previous implementation reported success three times without ever
    confirming a keystroke landed.

    `ok` now means the bound conversation gained a new user turn. A cleared composer is
    necessary but NOT sufficient: the page empties it optimistically, so a send that failed
    after acceptance looked identical to one that worked.
    """
    # FAIL CLOSED at the single choke point. Any caller — companion, operator, a script —
    # must claim the slot, so no path can submit outside the global cooldown.
    if claim:
        try:
            import sys as _sys
            _sys.path.insert(0, "/root/ai-dev-runtime")
            from core import wake_bridge as _wb
            c = _wb.claim_send(source, event_id=event_id, actionable=actionable)
            if not c.get("allowed"):
                return {"ok": False, "reason": f"not_claimed:{c.get('reason')}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"claim_unavailable:{type(e).__name__}"}

    # The claim is spent from here on, so every outcome past this line is a delivery attempt
    # and is recorded as one — success and failure alike.
    return _record_delivery(source, event_id, _attempt(conversation_url, phrase,
                                                       source=source, event_id=event_id),
                            conversation=conversation_url)


def _latch_submitted(source: str, event_id: Optional[int]) -> None:
    """Record that the phrase was FIRED, before we know whether it landed."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/root/ai-dev-runtime")
        from core import wake_bridge as _wb
        _wb.mark_submitted(event_id, source=source)
    except Exception:  # noqa: BLE001
        pass


def _attempt(conversation_url: str, phrase: str, *, source: str = "unknown",
             event_id: Optional[int] = None) -> dict:
    """The submission itself, once the slot is claimed. Split out so that every exit path
    below is recorded by exactly one caller instead of each remembering to do it."""
    target = find_target(conversation_url)
    if not target:
        # The page may simply be on another ChatGPT URL (a restart lands on the root). Take
        # the ChatGPT tab and navigate it to the BOUND conversation. The URL is the owner's
        # pointer, not content, and navigating is what guarantees the phrase cannot land in
        # some other chat.
        target = find_chatgpt_page()
        if not target:
            return {"ok": False, "reason": "no_chatgpt_page_open"}
        s0 = None
        try:
            s0 = _Session(target["webSocketDebuggerUrl"])
            s0.call("Page.enable")
            s0.call("Page.navigate", {"url": conversation_url})
        finally:
            if s0:
                s0.close()
        import time
        for _ in range(15):
            time.sleep(2)
            target = find_target(conversation_url)
            if target:
                break
        if not target:
            return {"ok": False, "reason": "could_not_open_bound_conversation"}
    s = None
    try:
        s = _Session(target["webSocketDebuggerUrl"])
        s.call("Runtime.enable")

        import time as _t
        for _ in range(12):
            if s.boolean('document.readyState === "complete"') is True and \
                    s.count(COMPOSER_SEL) == 1:
                break
            _t.sleep(2)
        n = s.count(COMPOSER_SEL)
        if n != 1:
            return {"ok": False, "reason": f"composer_ambiguous_or_absent:{n}"}
        # The baseline the delivery proof is measured against. Taken BEFORE anything is
        # typed, so a turn that was already on screen can never be mistaken for ours.
        turns_before = s.count(USER_TURN_SEL)
        if turns_before < 0:
            return {"ok": False, "reason": "user_turn_count_unavailable"}
        # Second, virtualization-proof baseline: the OPAQUE id of the newest user turn.
        # ChatGPT unmounts old turns as the conversation grows, so in a long chat a new
        # turn mounting at the bottom can evict one at the top and leave the COUNT flat —
        # which made 25 genuine deliveries in a row read as failures. The newest turn is
        # always mounted, so its id CHANGING is proof a new turn arrived even when the
        # count never moves. An id is metadata; no content is read.
        last_id_before = s.last_attr(USER_TURN_SEL, "data-message-id")
        # Focus by element identity, then CONFIRM focus. A focus call that silently fails is
        # exactly how the phrase went nowhere before.
        s.call("Runtime.evaluate",
               {"expression": f"document.querySelector({COMPOSER_SEL!r}).focus()"})
        focused = s.boolean(
            f"document.activeElement === document.querySelector({COMPOSER_SEL!r})")
        if focused is not True:
            return {"ok": False, "reason": "composer_not_focused"}

        # The ONLY string ever sent into the page.
        s.call("Input.insertText", {"text": phrase})

        # Confirm the composer is now non-empty WITHOUT reading what it contains.
        nonempty = s.boolean(
            f"document.querySelector({COMPOSER_SEL!r}).textContent.length > 0")
        if nonempty is not True:
            return {"ok": False, "reason": "phrase_did_not_reach_composer"}

        # The send control appears only now that the composer holds text.
        enabled = s.boolean(
            f"(function(){{const b=document.querySelector({SEND_SEL!r});"
            f"return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';}})()")
        # LATCH FIRST. Everything below can send the phrase, and everything below can also
        # fail to observe that it did. Whichever way the verification lands, this event must
        # never be offered for submission a second time.
        _latch_submitted(source, event_id)
        if enabled is True:
            s.call("Runtime.evaluate",
                   {"expression": f"document.querySelector({SEND_SEL!r}).click()"})
        else:
            # No identifiable send control: submit with Enter, the same path a keyboard user
            # takes. Success is still judged by the composer clearing, never by assumption.
            for t in ("keyDown", "char", "keyUp"):
                s.call("Input.dispatchKeyEvent",
                       {"type": t, "key": "Enter", "code": "Enter",
                        "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                        "text": "\r" if t == "char" else ""})

        # Two separate facts, in order. The composer emptying says the page took the
        # keystrokes; only a new turn — count risen, OR the newest turn's id changed under
        # a flat count (virtualized long chat) — says the conversation kept the message.
        import time
        cleared = False
        for _ in range(10):
            time.sleep(1)
            if not cleared:
                cleared = s.boolean(
                    f"(function(){{const c=document.querySelector({COMPOSER_SEL!r});"
                    f"return !c || c.textContent.length === 0;}})()") is True
            if not cleared:
                continue
            if s.count(USER_TURN_SEL) > turns_before:
                return {"ok": True, "reason": "submitted_and_user_turn_appeared"}
            last_id_now = s.last_attr(USER_TURN_SEL, "data-message-id")
            if (last_id_before is not None and last_id_now
                    and last_id_now != last_id_before):
                return {"ok": True, "reason": "submitted_and_user_turn_id_advanced"}
        if not cleared:
            return {"ok": False, "reason": "composer_did_not_clear_after_send"}
        # The composer emptied and nothing arrived. This is the exact shape of the silent
        # failure this check exists for: it must NOT be acknowledged, so the wake stays
        # pending and is retried after the cooldown.
        return {"ok": False, "reason": "user_turn_not_observed_after_send"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"cdp_error:{type(e).__name__}"}
    finally:
        if s:
            s.close()
