"""Tell the owner when the ChatGPT session on the wake browser is gone.

On 2026-11-06 that session expired at about 19:41 CET. Every wake then failed as
`composer_focus_trapped_by_dialog` for 11.5 hours, with no word to the owner. A wake cannot
report this, because the wake path is the thing that broke, so the alert goes to Telegram.

Two bad probes in a row alert, once, then one reminder per REMINDER_SECS, then one message
on recovery. An alert counts as sent only on Telegram's receipt, so a failed send is
retried at the next probe. `unknown` changes nothing: a page that could not answer says
nothing about the session. State is a small file, not `control_plane.db`.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

OK = "ok"
LOGGED_OUT = "logged_out"
BROWSER_DOWN = "browser_down"
UNKNOWN = "unknown"

FAIL_THRESHOLD = int(os.getenv("CHATGPT_SESSION_FAIL_THRESHOLD", "2"))
REMINDER_SECS = int(os.getenv("CHATGPT_SESSION_REMINDER_SECS", str(6 * 3600)))

_TEXT = {
    LOGGED_OUT: "ChatGPT: вход на живом браузере слетел. Сообщения в чаты не доходят, "
                "цикл стоит. Нужен новый вход.",
    BROWSER_DOWN: "ChatGPT: браузер не отвечает. Сообщения в чаты не доходят, цикл стоит.",
}
_RECOVERED = "ChatGPT: снова работает, сообщения идут."


def _state_path() -> str:
    explicit = os.getenv("CHATGPT_SESSION_STATE", "").strip()
    if explicit:
        return explicit
    db = os.getenv("CONTROL_PLANE_DB", "/root/ai-dev-runtime/control_plane.db")
    return db + ".chatgpt_session.json"


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def decide(state: dict, status: str, now: float) -> Optional[str]:
    """Update `state` in place. Returns the message to send, or None."""
    if status == UNKNOWN:
        return None
    if status == OK:
        state["streak"] = 0
        return _RECOVERED if state.get("alerted") else None
    state["streak"] = int(state.get("streak") or 0) + 1
    if state["streak"] < FAIL_THRESHOLD:
        return None
    last = float(state.get("last_sent") or 0)
    if state.get("alerted") and now - last < REMINDER_SECS:
        return None
    text = _TEXT.get(status, _TEXT[BROWSER_DOWN])
    link = login_url() if status == LOGGED_OUT else ""
    return f"{text}\nВойти: {link}" if link else text


def login_url() -> str:
    """The owner's view of the wake browser (noVNC, tailnet only), set by the operator.
    Only an http(s) URL without credentials is used; anything else is dropped, so a
    misconfiguration can never put a secret into a Telegram message."""
    from urllib.parse import urlsplit
    raw = os.getenv("CHATGPT_SESSION_LOGIN_URL", "").strip()
    try:
        u = urlsplit(raw)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return ""
    if "password" in (u.query or "").lower():
        return ""
    return raw


def check(probe: Callable[[], str], send: Optional[Callable[[str], tuple]] = None,
          *, now: Optional[float] = None, path: Optional[str] = None) -> dict:
    """One probe. Never raises on a probe failure: a broken probe reads as `unknown`."""
    if send is None:
        from core.control_plane.delivery import _send_owner_push as send
    now = time.time() if now is None else now
    path = path or _state_path()
    state = _load(path)
    try:
        status = probe()
    except Exception:  # noqa: BLE001 — a failed probe says nothing about the session
        status = UNKNOWN
    message = decide(state, status, now)
    result = {"status": status, "sent": False, "error": None}
    if message:
        ok, _, error = send(message)
        result.update(sent=bool(ok), error=error)
        if ok:
            state["alerted"] = status != OK
            state["last_sent"] = now
    _save(path, state)
    return result
