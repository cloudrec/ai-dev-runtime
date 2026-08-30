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
import re
import os
import time
import urllib.parse
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
# A user turn element appearing is CLIENT-SIDE evidence: the page rendered our text.
# It does not prove the backend accepted the message, that it persisted, or that the
# assistant ever ran. Three deliveries were reported successful into a conversation
# that ended up holding two user turns, and the owner had to type manually because no
# assistant turn had started. The wake exists to make the reviewer RUN, so the proof
# has to be the assistant running.
ASSISTANT_TURN_SEL = "[data-message-author-role='assistant']"
# ChatGPT shows a stop control only while a response is generating; its presence is
# the earliest positive evidence that generation actually began.
STREAMING_SEL = ("button[data-testid='stop-button'], button[aria-label*='Stop'], "
                 "button[data-testid='composer-speech-button-container'] ~ "
                 "button[data-testid='stop-button']")
# How long the assistant gets to START after our message lands. Generous: a slow
# start is not a failure, only silence is.
ASSISTANT_START_SECS = int(os.getenv("WAKE_ASSISTANT_START_SECS", "45"))


def _http(path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://{CDP_HOST}:{CDP_PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}


# BROWSER-level health, as distinct from one tab's renderer. The difference decides
# whether replacing a tab helps or hurts.
#
# 2026-08-30: the host ran out of memory and swap (100% of 20 GB, load 29, continuous
# paging). `page_responsive()` was false for every tab, because the whole browser was
# starved — so `recover_wedged_tab()` fired on every delivery attempt, opened a
# replacement it could not verify inside its window, and left the old tab open. Chrome
# went from 1 owner-os tab and 61 processes to 41 pages (25 of them bare chatgpt.com
# roots) and 68 processes in eight minutes. Each failed delivery was adding renderers to
# the exhaustion that caused the failure.
#
# Replacing a tab is right when ONE renderer is wedged and the browser is healthy — the
# 4214 incident this recovery was written for. It is destructive when the browser itself
# is degraded, so that case must refuse instead.
BROWSER_MAX_PAGES = int(os.getenv("CDP_MAX_PAGES", "12"))
BROWSER_SLOW_SECS = float(os.getenv("CDP_SLOW_SECS", "2.0"))


def browser_degraded(list_fn=None, clock=None) -> dict:
    """Is the BROWSER in trouble, rather than a single page?

    Three signals, any one sufficient: the browser-level endpoint does not answer at all;
    it answers but slowly (a healthy Chrome lists tabs in milliseconds); or it is already
    holding more pages than this host should ever need, which is itself the signature of
    replacement tabs accumulating.
    """
    clock = clock or time.monotonic
    t0 = clock()
    try:
        pages = (list_fn or (lambda: _http("/json/list")))()
    except Exception as e:  # noqa: BLE001
        return {"degraded": True, "reason": f"endpoint_unreachable:{type(e).__name__}"}
    elapsed = clock() - t0
    n = sum(1 for p in pages if isinstance(p, dict) and p.get("type") == "page") \
        if isinstance(pages, list) else -1
    if elapsed > BROWSER_SLOW_SECS:
        return {"degraded": True, "reason": f"endpoint_slow:{elapsed:.1f}s", "pages": n}
    if n > BROWSER_MAX_PAGES:
        return {"degraded": True, "reason": f"too_many_pages:{n}", "pages": n}
    return {"degraded": False, "reason": "ok", "pages": n, "elapsed": elapsed}


def page_responsive(target: dict, timeout: float = 8.0) -> bool:
    """Can this page's renderer answer a trivial evaluate at all?

    The 4214 incident: the owner-os tab's main thread wedged for hours, every delivery
    attempt died in WebSocketTimeout — 113 in a row — and no per-call timeout could help,
    because the renderer itself had stopped answering. This probe is the cheap question
    asked BEFORE spending a claimed send on a dead page."""
    s = None
    try:
        import websocket
        ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                         timeout=timeout, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": "1+1", "returnByValue": True}}))
            msg = json.loads(ws.recv())
            return ((msg.get("result") or {}).get("result") or {}).get("value") == 2
        finally:
            ws.close()
    except Exception:  # noqa: BLE001
        return False


def recover_wedged_tab(old_target: dict, conversation_url: str) -> Optional[dict]:
    """Replace a wedged renderer with a fresh tab on the SAME bound conversation.

    Browser-level HTTP endpoints (/json/new, /json/close) are handled by the browser
    process and keep working when a renderer hangs — which is exactly when the CDP
    session channel does not. The new tab opens directly on the bound URL, so the
    exact-route guarantee is untouched; the wedged tab is closed only AFTER the
    replacement exists, so a failed recovery cannot leave us with no ChatGPT tab at all.

    REFUSES when the BROWSER is degraded rather than this one tab: opening a replacement
    then is not recovery, it is another renderer added to whatever is already starving the
    browser. This is the single choke point for creating tabs, so the guard lives here and
    no caller can bypass it."""
    import time
    d = browser_degraded()
    if d.get("degraded"):
        return None
    try:
        try:
            fresh = _http(f"/json/new?{urllib.parse.urlencode({'': conversation_url})[1:]}",
                          method="PUT")
        except Exception:  # noqa: BLE001 — pre-111 Chrome used GET here
            fresh = _http(f"/json/new?{urllib.parse.urlencode({'': conversation_url})[1:]}")
        if not fresh.get("id"):
            return None
        for _ in range(15):
            time.sleep(2)
            t = find_target(conversation_url)
            if t and t.get("id") == fresh.get("id") and page_responsive(t):
                break
        else:
            return None
        if old_target.get("id") and old_target["id"] != fresh["id"]:
            try:
                _http(f"/json/close/{old_target['id']}")
            except Exception:  # noqa: BLE001 — a zombie tab is worse to fight than to leave
                pass
        return find_target(conversation_url)
    except Exception:  # noqa: BLE001
        return None


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


def open_chatgpt_page(conversation_url: str) -> Optional[dict]:
    """Open a FRESH tab directly on the bound conversation when NO ChatGPT tab exists at
    all — the browser-level `/json/new` endpoint, same one `recover_wedged_tab` already
    uses for a wedged renderer, so this adds no new transport, only the missing case
    where there was no renderer to recover in the first place. The owner's existing
    logged-in browser profile carries the session; this only asks the browser to point
    a tab at the URL, never touches credentials."""
    import time
    try:
        try:
            fresh = _http(f"/json/new?{urllib.parse.urlencode({'': conversation_url})[1:]}",
                          method="PUT")
        except Exception:  # noqa: BLE001 — pre-111 Chrome used GET here
            fresh = _http(f"/json/new?{urllib.parse.urlencode({'': conversation_url})[1:]}")
        if not fresh.get("id"):
            return None
        for _ in range(15):
            time.sleep(2)
            t = find_target(conversation_url)
            if t and t.get("id") == fresh.get("id") and page_responsive(t):
                return t
        return find_target(conversation_url)
    except Exception:  # noqa: BLE001
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


# A dialog that CONTAINS activeElement is a focus trap: focus() on the composer is
# reverted the moment it is called, so the phrase can never be typed. Radix popovers —
# ChatGPT's own menus and pickers — are exactly this, and one left open on a route tab
# silently blocks every wake to that chat until someone notices in a browser.
_DIALOG_TRAPS_FOCUS_JS = (
    "(function(){var d=document.querySelectorAll('[role=dialog]');"
    "for(var i=0;i<d.length;i++){if(d[i].contains(document.activeElement))return true;}"
    "return false;})()")


def dialog_traps_focus(s) -> Optional[bool]:
    return s.boolean(_DIALOG_TRAPS_FOCUS_JS)


# The ONLY labels this code will click, and only on a dialog whose entire button set is
# that one button. An acknowledgement modal has one job: be acknowledged. Anything with a
# choice in it ("Upgrade"/"Not now", "Delete"/"Cancel") is a DECISION, and a decision is
# not this module's to make — it refuses and reports instead.
_ACK_BUTTON_LABELS = frozenset({"got it", "ok", "okay", "dismiss", "close", "understood"})

# The page is asked only for DATA (the buttons' accessible names) and, separately, for a
# click on a dialog that has exactly one button. The decision of whether to click at all
# lives here, in Python, where it is tested — an earlier cut put it inside the injected
# JS, and the test fake reimplemented the same rules in Python, so removing either guard
# left every test passing. Policy the tests cannot reach is not policy.
_DIALOG_BUTTONS_JS = """
(function(){
  var d = document.querySelector('[role=dialog]');
  if (!d) return '[]';
  return JSON.stringify([].slice.call(d.querySelectorAll('button')).map(function(b){
    return ((b.getAttribute('aria-label') || b.textContent || '').trim()).slice(0, 60);
  }));
})()
"""

_CLICK_ONLY_BUTTON_JS = """
(function(){
  var d = document.querySelector('[role=dialog]');
  if (!d) return 'no_dialog';
  var btns = d.querySelectorAll('button');
  if (btns.length !== 1) return 'not_single_button';
  btns[0].click();
  return 'clicked';
})()
"""


def dialog_buttons(s) -> list:
    """The accessible NAMES of the open dialog's buttons. Chrome-level UI metadata."""
    r = s.call("Runtime.evaluate", {"expression": _DIALOG_BUTTONS_JS,
                                    "returnByValue": True})
    v = ((r.get("result") or {}).get("value"))
    try:
        names = json.loads(v) if isinstance(v, str) else []
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)]


def ack_click_decision(names: list) -> str:
    """May this dialog be acknowledged with a click? FAIL CLOSED.

    Two conditions together make a click an acknowledgement rather than a decision: the
    dialog has exactly ONE button, and that button's name is a known acknowledgement.
    A dialog offering a CHOICE ("Upgrade"/"Not now", "Delete"/"Cancel") is a decision,
    and a decision is not this module's to make.
    """
    if not names:
        return "no_dialog_buttons"
    if len(names) != 1:
        return "not_single_button"
    if names[0].strip().lower() not in _ACK_BUTTON_LABELS:
        return "label_not_allowlisted"
    return "allowed"


# While a turn is generating, ChatGPT REPLACES the send control with a stop control. The
# composer still accepts text, so a wake typed now sits in the box, the Enter fallback is
# ignored, and the attempt is reported as `composer_did_not_clear_after_send` — a reason
# that says the page refused a send when in truth no send was ever possible.
#
# 2026-08-30: six consecutive wake deliveries failed exactly this way while the assistant
# was still answering the previous wake. Claims were fast and correct; the delivery verdict
# was simply describing the wrong thing, and an operator reading the log would look for a
# broken composer instead of ordinary back-pressure.
_STOP_BUTTON_SEL = "button[data-testid='stop-button'], button[aria-label*='Stop']"
# Tokens actually arriving. Distinct from STREAMING_SEL / _STOP_BUTTON_SEL, which are the
# stop CONTROL: the control being up says a turn was started, this says one is still
# producing. The difference between them is exactly what distinguishes back-pressure from
# a wedged conversation.
STREAMING_CONTENT_SEL = ".result-streaming, [data-message-streaming='true']"


def assistant_is_generating(s) -> Optional[bool]:
    """Is a turn being generated right now? None when the page cannot answer."""
    return s.boolean(f"!!document.querySelector({_STOP_BUTTON_SEL!r})")


def generating_is_wedged(s, samples: int = 3, interval: float = 2.0, sleep=None) -> bool:
    """A stop control that is present while NOTHING is happening.

    ChatGPT shows the stop control for the whole of a turn, and the composer offers no
    send control while it does — so a conversation whose turn never finishes blocks every
    future delivery to that chat, permanently and silently. 2026-08-30: the owner-os tab
    sat with `data-testid=stop-button` visible, `.result-streaming` absent and the newest
    assistant turn frozen, for over half an hour; `page_responsive()` was true throughout
    (the RENDERER was fine — the CONVERSATION was stuck), so the existing wedged-tab
    recovery never triggered and not one wake could be delivered.

    Bounded and conservative: every sample must agree, across `samples * interval`
    seconds, that the stop control is up, nothing is streaming and the newest assistant
    turn id has not moved. Any sign of life — streaming, a new turn, the stop control
    clearing — answers False immediately, so a genuinely long answer is never cut short.
    """
    import time as _t
    sleep = sleep or _t.sleep
    last_id = None
    for i in range(max(1, samples)):
        if i:
            sleep(interval)
        if s.boolean(f"!!document.querySelector({_STOP_BUTTON_SEL!r})") is not True:
            return False                      # the turn ended: not wedged
        if s.boolean(f"!!document.querySelector({STREAMING_CONTENT_SEL!r})") is True:
            return False                      # tokens are arriving: genuinely working
        now_id = s.last_attr(ASSISTANT_TURN_SEL, "data-message-id")
        if last_id is not None and now_id != last_id:
            return False                      # a new turn landed: genuinely working
        last_id = now_id
    return True


def dialog_title(s) -> str:
    """The dialog's accessible TITLE — UI chrome, the same class of metadata this module
    already reads from the sidebar. Never conversation content."""
    r = s.call("Runtime.evaluate", {"returnByValue": True, "expression":
               "(function(){var d=document.querySelector('[role=dialog]');if(!d)return '';"
               "var lb=d.getAttribute('aria-labelledby');"
               "var t=lb?document.getElementById(lb):null;"
               "return ((t?t.textContent:'')||'').trim().slice(0,60);})()"})
    v = ((r.get("result") or {}).get("value"))
    return v if isinstance(v, str) else ""


def dismiss_focus_trap(s) -> str:
    """Close a focus-trapping dialog: Escape first, then — and only then — a click on a
    single allowlisted acknowledgement button.

    Escape alone is not enough, and pretending otherwise is how a TRANSIENT condition
    became a permanent one. The dialog that blocked every wake on 2026-08-30 was
    ChatGPT's own "Too many requests" notice: an alert dialog with one button ("Got it")
    that deliberately ignores Escape, because an alert is meant to be acknowledged. With
    nothing to acknowledge it, a rate limit that had long since expired kept the composer
    unreachable for hours.

    A click here is bounded by two conditions that together make it an acknowledgement
    and not a decision: the dialog must contain exactly ONE button, and that button's
    accessible name must be in `_ACK_BUTTON_LABELS`. Anything else is left alone.
    """
    for ev in ("rawKeyDown", "keyUp"):
        s.call("Input.dispatchKeyEvent", {"type": ev, "key": "Escape", "code": "Escape",
                                          "windowsVirtualKeyCode": 27,
                                          "nativeVirtualKeyCode": 27})
    if dialog_traps_focus(s) is not True:
        return "escape"
    decision = ack_click_decision(dialog_buttons(s))
    if decision != "allowed":
        return decision
    r = s.call("Runtime.evaluate", {"expression": _CLICK_ONLY_BUTTON_JS,
                                    "returnByValue": True})
    v = ((r.get("result") or {}).get("value"))
    return v if isinstance(v, str) else "unknown"


def focus_failure_reason(s) -> str:
    """Name WHY the composer could not be focused, and name the dialog when there is one.

    The 3.5-hour blackout was logged 98 times as `composer_not_focused` and it took a
    live CDP session to discover that the cause was ChatGPT's rate-limit notice. The
    dialog's own title is the whole diagnosis, so it belongs in the reason."""
    if dialog_traps_focus(s) is not True:
        return "composer_not_focused"
    slug = re.sub(r"[^a-z0-9]+", "-", (dialog_title(s) or "").lower()).strip("-")[:40]
    return f"composer_focus_trapped_by_dialog:{slug}" if slug         else "composer_focus_trapped_by_dialog"


def focus_composer(s, attempts: int = 3, sleep=None) -> Optional[bool]:
    """Focus the composer, dismissing a focus-trapping dialog if one is in the way.

    Bounded: a trap that survives `attempts` Escapes is reported, not fought. The caller
    turns that into a named failure reason rather than a retry loop against a page that
    is not going to yield.
    """
    import time as _t
    sleep = sleep or _t.sleep
    for i in range(max(1, attempts)):
        s.call("Runtime.evaluate",
               {"expression": f"document.querySelector({COMPOSER_SEL!r}).focus()"})
        focused = s.boolean(
            f"document.activeElement === document.querySelector({COMPOSER_SEL!r})")
        if focused is True:
            return True
        if dialog_traps_focus(s) is not True:
            # Not a trap — retrying the same call would only repeat the same answer.
            return focused
        dismiss_focus_trap(s)
        if i + 1 < max(1, attempts):
            sleep(0.3)
    return s.boolean(
        f"document.activeElement === document.querySelector({COMPOSER_SEL!r})")


def _record_delivery(source: str, event_id: Optional[int], res: dict,
                     conversation: str = "", route_key: str = "") -> dict:
    """Persist the outcome of an attempt — the failures above all, since those are the ones
    that must stay unacknowledged and be retried. The conversation and route the attempt
    resolved to are recorded alongside, so "which chat did this send go to, and why" is
    answerable from state."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/root/ai-dev-runtime")
        from core import wake_bridge as _wb
        _wb.record_delivery(source, event_id=event_id, delivered=bool(res.get("ok")),
                            reason=str(res.get("reason", "")), conversation=conversation,
                            route_key=route_key)
    except Exception:  # noqa: BLE001 — a missing recorder must never turn into a false success
        pass
    return res


def list_sidebar_conversations(limit: int = 100) -> dict:
    """Read-only: the conversation links ChatGPT's own sidebar currently shows.

    This is the account-visible surface — a chat created on the owner's phone appears
    here once the web app syncs it, even though no server tab was ever open on it. Reads
    ONLY same-origin anchors under /c/ (href + link text, which is the chat's title);
    never navigates, never types, never touches history or other domains. A page without
    a mounted sidebar simply yields nothing — fail safe, not fail loud.
    """
    t = find_chatgpt_page()
    if not t:
        return {"ok": False, "reason": "no_chatgpt_page_open", "conversations": []}
    s = None
    try:
        s = _Session(t["webSocketDebuggerUrl"])
        s.call("Runtime.enable")
        expr = ("(function(){const seen={};const out=[];"
                "for(const a of document.querySelectorAll('a[href^=\"/c/\"]')){"
                f"if(out.length>={int(limit)})break;"
                "const h=(a.getAttribute('href')||'').split('?')[0].split('#')[0];"
                "if(!/^\\/c\\/[A-Za-z0-9-]+\\/?$/.test(h)||seen[h])continue;seen[h]=1;"
                "out.push({href:h,title:(a.textContent||'').trim().slice(0,200)});}"
                "return JSON.stringify(out);})()")
        r = s.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        raw = ((r.get("result") or {}).get("value"))
        if not isinstance(raw, str):
            return {"ok": False, "reason": "sidebar_unreadable", "conversations": []}
        items = json.loads(raw)
        convs = [{"url": "https://chatgpt.com" + i["href"].rstrip("/"),
                  "title": i.get("title") or ""} for i in items]
        return {"ok": True, "conversations": convs}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"cdp_error:{type(e).__name__}", "conversations": []}
    finally:
        if s:
            s.close()


def submit_phrase(conversation_url: str, phrase: str, *, source: str = "unknown",
                  event_id: Optional[int] = None, claim: bool = True,
                  actionable: bool = False, route_key: str = "") -> dict:
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
            # The claim is for a slot in THIS conversation, so it carries the route.
            c = _wb.claim_send(source, event_id=event_id, actionable=actionable,
                               route_key=route_key)
            if not c.get("allowed"):
                return {"ok": False, "reason": f"not_claimed:{c.get('reason')}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"claim_unavailable:{type(e).__name__}"}

    # The claim is spent from here on, so every outcome past this line is a delivery attempt
    # and is recorded as one — success and failure alike.
    res = _attempt(conversation_url, phrase, source=source, event_id=event_id)
    # A WEDGED conversation never clears on its own: the stop control stays up, no send
    # control is offered, and every future wake to this chat fails identically. That is a
    # permanent route outage, so it earns the one recovery this module already has —
    # replacing the tab on the SAME bound conversation, which keeps the exact-route
    # guarantee — and exactly one retry. Ordinary back-pressure is NOT recovered: a turn
    # genuinely in flight resolves itself and interrupting it would be destructive.
    if res.get("reason") == "assistant_generating_wedged":
        target = find_target(conversation_url)
        if target and recover_wedged_tab(target, conversation_url):
            res = _attempt(conversation_url, phrase, source=source, event_id=event_id)
            res["after_wedge_recovery"] = True
    return _record_delivery(source, event_id, res,
                            conversation=conversation_url, route_key=route_key)


def _latch_submitted(source: str, event_id: Optional[int]) -> None:
    """Record that the phrase was FIRED, before we know whether it landed."""
    try:
        import sys as _sys
        _sys.path.insert(0, "/root/ai-dev-runtime")
        from core import wake_bridge as _wb
        _wb.mark_submitted(event_id, source=source)
    except Exception:  # noqa: BLE001
        pass


def _await_assistant(s, asst_before: int, asst_id_before) -> dict:
    """Did the assistant actually start after our message landed?

    Three signals, any of which is positive proof that the backend accepted the
    turn and generation began:
      * a new assistant turn element (count risen), or
      * the newest assistant turn's id changed (virtualization-proof, same trick
        the user-turn check already uses), or
      * the streaming/stop control is present, which ChatGPT renders only while a
        response is being produced.

    Silence for ASSISTANT_START_SECS is reported as a FAILURE, deliberately. The
    message is already in the chat and the submission is latched, so it must never
    be resent as a duplicate - the honest outcome is delivered=false with a reason
    that names what did not happen, leaving the closed-loop watchdog to re-wake and
    ultimately escalate.
    """
    deadline = time.time() + ASSISTANT_START_SECS
    while time.time() < deadline:
        if s.count(STREAMING_SEL) > 0:
            return {"ok": True, "reason": "submitted_and_assistant_started_generating"}
        if s.count(ASSISTANT_TURN_SEL) > asst_before:
            return {"ok": True, "reason": "submitted_and_assistant_responded"}
        asst_id_now = s.last_attr(ASSISTANT_TURN_SEL, "data-message-id")
        if (asst_id_before is not None and asst_id_now
                and asst_id_now != asst_id_before):
            return {"ok": True, "reason": "submitted_and_assistant_turn_advanced"}
        time.sleep(2)
    return {"ok": False,
            "reason": f"user_turn_landed_but_assistant_never_started_in_{ASSISTANT_START_SECS}s"}


def _attempt(conversation_url: str, phrase: str, *, source: str = "unknown",
             event_id: Optional[int] = None) -> dict:
    """The submission itself, once the slot is claimed. Split out so that every exit path
    below is recorded by exactly one caller instead of each remembering to do it."""
    # FAIL FAST on a starving browser, before any session is opened. Under the 2026-08-30
    # host exhaustion the renderer probe passed and the CDP session then hung, so each
    # attempt burned tens of seconds of a machine that was already thrashing and recorded
    # `cdp_error:WebSocketTimeoutException` — true, but it blames the socket for a
    # shortage of memory. Checking first costs one cheap browser-level call and makes the
    # attempt honest. Fail-open: only a browser that is measurably degraded is refused.
    _bd = browser_degraded()
    if _bd.get("degraded"):
        return {"ok": False, "reason": f"browser_degraded:{_bd.get('reason')}"}
    target = find_target(conversation_url)
    if not target:
        # The page may simply be on another ChatGPT URL (a restart lands on the root). Take
        # the ChatGPT tab and navigate it to the BOUND conversation. The URL is the owner's
        # pointer, not content, and navigating is what guarantees the phrase cannot land in
        # some other chat.
        target = find_chatgpt_page()
        if not target:
            # No ChatGPT tab exists anywhere in this browser — nothing to navigate.
            # Open one directly on the bound conversation at the browser level, exactly
            # as the wedged-renderer recovery path already does.
            target = open_chatgpt_page(conversation_url)
            if not target:
                return {"ok": False, "reason": "no_chatgpt_page_open"}
        else:
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
    # A wedged renderer answers nothing, ever; detect it BEFORE burning the attempt on a
    # guaranteed timeout, and replace the tab at browser level (the 4214 incident: 113
    # identical WebSocketTimeouts against one hung page).
    if not page_responsive(target):
        # Name WHICH thing is unwell. A starving browser and a single wedged renderer look
        # identical from one tab, and only the second is worth replacing a tab over.
        d = browser_degraded()
        if d.get("degraded"):
            return {"ok": False, "reason": f"browser_degraded:{d.get('reason')}"}
        target = recover_wedged_tab(target, conversation_url)
        if not target:
            return {"ok": False, "reason": "renderer_unresponsive"}
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
        # The same two baselines for the ASSISTANT side, taken before anything is typed.
        asst_before = s.count(ASSISTANT_TURN_SEL)
        asst_id_before = s.last_attr(ASSISTANT_TURN_SEL, "data-message-id")
        # Focus by element identity, then CONFIRM focus. A focus call that silently fails is
        # exactly how the phrase went nowhere before.
        focused = focus_composer(s)
        if focused is not True:
            # Name the cause instead of collapsing it. 2026-08-30, 12:19 -> 16:0x: every
            # wake delivery failed as the generic `composer_not_focused` while the real
            # state was a stray Radix popover (`[role=dialog][data-state=open]`) holding
            # activeElement in its focus trap on three route tabs at once. Three and a
            # half hours of undeliverable wakes read as one undifferentiated failure.
            return {"ok": False, "reason": focus_failure_reason(s)}

        # BACK-PRESSURE, checked before anything is typed. A turn in flight means there is
        # no send control to click and Enter is ignored, so typing now would only leave the
        # phrase sitting in the composer and produce a misleading verdict. Nothing is
        # latched and nothing is typed: the event stays pending and the ordinary backoff
        # brings it around again once the assistant is free. Fail-open — if the page cannot
        # answer the question (None), the previous path runs unchanged.
        if assistant_is_generating(s) is True:
            # Two different worlds behind one appearance. A turn genuinely in flight is
            # back-pressure and resolves itself; a stop control that is up while nothing
            # streams never resolves, and blocks the route forever.
            if generating_is_wedged(s):
                return {"ok": False, "reason": "assistant_generating_wedged"}
            return {"ok": False, "reason": "assistant_still_generating"}

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
                if cleared:
                    # THE LATCH BOUNDARY. A cleared composer means the page took the
                    # phrase — from here on it may be in the chat, so this event must
                    # never be submitted again, whatever verification says below.
                    _latch_submitted(source, event_id)
            if not cleared:
                continue
            user_turn_seen = s.count(USER_TURN_SEL) > turns_before
            if not user_turn_seen:
                last_id_now = s.last_attr(USER_TURN_SEL, "data-message-id")
                user_turn_seen = bool(last_id_before is not None and last_id_now
                                      and last_id_now != last_id_before)
            if user_turn_seen:
                # The message is in the page. Now the question that actually matters:
                # did the assistant START? Until it does, the wake has delivered
                # nothing the reviewer will act on, and saying otherwise is the false
                # positive that let two agents sit stopped.
                return _await_assistant(s, asst_before, asst_id_before)
        if not cleared:
            # The phrase is still SITTING IN THE COMPOSER — provably not sent (event
            # 4214: a settling page refused the click and the old pre-fire latch then
            # consumed the event forever). No latch; clear the draft so the retry
            # starts clean, and let the backoff bring the event around again.
            s.call("Runtime.evaluate",
                   {"expression":
                    f"(function(){{const c=document.querySelector({COMPOSER_SEL!r});"
                    f"if(c){{c.innerHTML='';"
                    f"c.dispatchEvent(new Event('input',{{bubbles:true}}));}}}})()"})
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
