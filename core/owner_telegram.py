"""Telegram as an owner-identity channel for delegation grants (no paid API).

Disabled unless BOTH `OWNER_GRANT_TELEGRAM_BOT_TOKEN` and `OWNER_GRANT_TELEGRAM_USER_ID`
are set (the owner's numeric Telegram user id). Telegram authenticates the sender; this
module only trusts a message when
  * it is a private-chat message whose `from.id` AND `chat.id` equal the owner user id,
  * it says `/approve <grant id> <code>` (or `/revoke <grant id>`), and
  * the code is the one-time code this server sent to that chat with the SERVER-SIDE
    summary of the pending grant (so what the owner read is what gets activated).
Edited messages, forwards, other chats, other users and replays (update offset) are ignored.
Text relayed by ChatGPT, an API call or an agent never reaches this path.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Callable, Optional

_CMD = re.compile(r"^/(approve|revoke)\s+(g[0-9a-f]{12})(?:\s+(\d{6}))?\s*$")


def _cfg():
    token = os.getenv("OWNER_GRANT_TELEGRAM_BOT_TOKEN", "").strip()
    uid = os.getenv("OWNER_GRANT_TELEGRAM_USER_ID", "").strip()
    return (token, int(uid)) if token and uid.isdigit() else (None, None)


def enabled() -> bool:
    return _cfg()[0] is not None


def _call(method: str, payload: dict, opener: Optional[Callable] = None) -> dict:
    token, _ = _cfg()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 — never echo the URL (it contains the token)
        return {"ok": False, "error": type(e).__name__}


def send_approval_request(gid: str, summary: str, spec_hash: str, code: str,
                          opener: Optional[Callable] = None) -> bool:
    if not enabled():
        return False
    _, uid = _cfg()
    text = (f"Owner OS delegation request {gid}\n{summary}\n"
            f"content {spec_hash[:12]}\n\nApprove: /approve {gid} {code}\nIgnore to decline.")
    return bool(_call("sendMessage", {"chat_id": uid, "text": text}, opener).get("ok"))


def _offset_path() -> str:
    return os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db") + \
        ".telegram_offset"


def handle_update(upd: dict) -> Optional[dict]:
    """One Telegram update -> action result, or None when it is not an owner command."""
    from core import automation_grants as ag
    _, uid = _cfg()
    msg = upd.get("message")              # edited_message / channel_post deliberately ignored
    if not isinstance(msg, dict) or uid is None:
        return None
    frm, chat = msg.get("from") or {}, msg.get("chat") or {}
    if frm.get("id") != uid or chat.get("id") != uid or chat.get("type") != "private" \
            or frm.get("is_bot") or msg.get("forward_origin") or msg.get("forward_from") \
            or msg.get("forward_date"):
        return {"ignored": "not_the_owner_or_not_direct"}
    m = _CMD.match((msg.get("text") or "").strip())
    if not m:
        return None
    verb, gid, code = m.group(1), m.group(2), m.group(3)
    try:
        if verb == "revoke":
            return {"revoked": ag.revoke(gid, by="owner:telegram")}
        g = ag.get(gid)
        if g is None or not code:
            return {"approved": False, "reason": "unknown grant or missing code"}
        ag.approve(gid, via="telegram_verified", code=code, expected_hash=g["spec_hash"])
        return {"approved": True, "grant": gid}
    except ag.GrantError as e:
        return {"approved": False, "reason": str(e)}


def poll_once(opener: Optional[Callable] = None) -> list:
    """Fetch new updates once (no webhook, no inbound port) and handle owner commands."""
    if not enabled():
        return []
    try:
        offset = int(open(_offset_path()).read().strip() or 0)
    except (OSError, ValueError):
        offset = 0
    res = _call("getUpdates", {"offset": offset, "timeout": 0,
                               "allowed_updates": ["message"]}, opener)
    out = []
    for upd in res.get("result") or []:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        r = handle_update(upd)
        if r is not None:
            out.append(r)
    with open(_offset_path(), "w") as fh:
        fh.write(str(offset))
    return out
