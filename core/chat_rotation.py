"""A ChatGPT chat that reached its length cap is replaced, not left dead.

2026-10-28 the owner-os and payment-orchestrator chats hit the cap; wakes kept being typed
into them and the routes went silent. The work never lived in the chat (Owner OS keeps
specs, tasks, reviews, and the tech lead's notes: core.fullcycle.brief), so a new chat can
carry on. When a delivery shows the cap, `rotate` opens a new chat with a short start
message that tells it to read `techlead_brief`, moves every route bound to the full chat
onto it, and tells the owner in one line. The pending wake is not sent here: it stays
pending, and the next delivery resolves the route to the new chat.

One attempt per full chat per cooldown, so a misread page cannot open a chat per tick.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from core.control_plane.api import _c

COOLDOWN_SECS = 1200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_rotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, old TEXT, new TEXT, ok INTEGER,
    reason TEXT, routes TEXT
)"""


def start_message(old_url: str) -> str:
    return ("Owner OS: продолжение работы техлида. Предыдущий чат достиг предела длины "
            f"({old_url}). Состояние хранится в Owner OS, не в чате. Сначала вызови "
            "techlead_brief из коннектора Owner OS, восстанови проекты, задачи, ожидающие "
            "ревью и свои заметки, коротко подтверди, что понял, и продолжай как техлид. "
            "Дальше сюда будут приходить обычные уведомления Owner OS. Важные решения "
            "записывай через techlead_note_add.")


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def _norm(u: str) -> str:
    return (u or "").strip().rstrip("/")


def replaced_by(old_url: str, conn=None) -> Optional[str]:
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT new FROM chat_rotation WHERE old=? AND ok=1 ORDER BY id DESC "
                         "LIMIT 1", (_norm(old_url),)).fetchone()
        return r[0] if r else None
    finally:
        if own:
            conn.close()


def rotate(old_url: str, *, new_chat: Callable[[str], dict],
           notify: Optional[Callable[[str], object]] = None, conn=None,
           now: Optional[float] = None) -> dict:
    """Replace a full chat. `new_chat(start_message)` opens and returns {"ok", "url"}."""
    from core import wake_routes as wr
    now = now if now is not None else time.time()
    old = _norm(old_url)
    conn, own = _conn(conn)
    try:
        if not wr.valid_conversation(old):
            return {"ok": False, "reason": "not_a_conversation_url"}
        bound = [r for r in wr.list_routes(conn=conn) if _norm(r.get("conversation")) == old]
        if not bound:
            # nothing routes here: a new chat would have no one to talk to
            return {"ok": False, "reason": "no_route_on_this_chat"}
        new = replaced_by(old, conn=conn)
        if not new:
            last = conn.execute("SELECT ts FROM chat_rotation WHERE old=? ORDER BY id DESC "
                                "LIMIT 1", (old,)).fetchone()
            if last and now - (last[0] or 0) < COOLDOWN_SECS:
                return {"ok": False, "reason": "rotation_cooldown"}
            try:
                r = new_chat(start_message(old)) or {}
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "reason": f"{type(e).__name__}: {e}"[:200]}
            url = _norm(r.get("url") or "")
            if not r.get("ok") or not wr.valid_conversation(url) or url == old:
                conn.execute("INSERT INTO chat_rotation (ts, old, new, ok, reason, routes) "
                             "VALUES (?,?,?,?,?,?)", (now, old, url or None, 0,
                                                      str(r.get("reason"))[:200], ""))
                conn.commit()
                return {"ok": False, "reason": f"new_chat:{r.get('reason')}"}
            new = url
        moved = []
        for route in bound:
            b = wr.bind_route(route["route_key"], new, by="chat-rotation",
                              note=f"replaces full chat {old}"[:200], conn=conn)
            if b.get("ok"):
                moved.append(route["route_key"])
        conn.execute("INSERT INTO chat_rotation (ts, old, new, ok, reason, routes) "
                     "VALUES (?,?,?,?,?,?)", (now, old, new, 1, "rotated", ",".join(moved)))
        conn.commit()
    finally:
        if own:
            conn.close()
    if notify and moved:
        try:
            notify(f"💬 Чат ChatGPT переполнился, Owner OS открыл новый и продолжил в нём: {new}\n"
                   f"Переехали: {', '.join(moved)}. Старый: {old}")
        except Exception:  # noqa: BLE001 — a missed notice never undoes the move
            pass
    return {"ok": True, "new": new, "moved": moved}
