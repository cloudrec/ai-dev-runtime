"""Chat registry — every ChatGPT conversation the system has actually observed.

The wake route registry (`core.wake_routes`) answers "which chat does project X wake"; it
is deliberately small and owner-authoritative. This module is the layer beneath it: an
INVENTORY of conversations the logged-in companion browser has genuinely seen — open tabs,
delivery targets — so the owner does not have to paste every new URL, and so a dead chat
is a recorded fact instead of a surprise at delivery time.

Hard rules, each closing a specific failure mode:

  * Inventory is not routing. A discovered chat is a row here, never a wake target, until
    something with authority binds it: the owner (CLI/MCP), or `consider_auto_bind` with
    DETERMINISTIC evidence.
  * Auto-bind fails closed. It binds only a route that is currently UNBOUND, only when
    exactly ONE known project key matches the chat's title as an explicit token, and never
    the owner-os route. It never rebinds an existing route — "newer chat" is not evidence,
    and silently moving a project's doorbell is the wrong-chat bug with extra steps.
  * Deadness is evidence-based: a send that fired and was refused by the page
    (`composer_did_not_clear_after_send`) marks the chat dead here, and `wake_routes`
    then refuses to route into it (`dead_route:` fallback) instead of losing events.
  * Discovery reads only what the companion already touches: ChatGPT tab URLs and titles
    from the CDP page list. No browser history, nothing outside chatgpt.com.
"""
from __future__ import annotations

import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts
from core import wake_routes

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_registry (
    conversation TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    first_seen TEXT, first_seen_ts REAL,
    last_seen TEXT, last_seen_ts REAL,
    writable INTEGER,                -- NULL unknown, 1 proven, 0 refused a send
    writable_checked_at TEXT,
    inferred_route TEXT DEFAULT '',
    confidence TEXT DEFAULT '',
    evidence TEXT DEFAULT '',
    source TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    dead_reason TEXT DEFAULT ''
)
"""

_CONV_ID_RE = re.compile(r"/c/([A-Za-z0-9\-]+)")


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def upsert_chat(conversation: str, *, title: str = "", source: str = "",
                writable: Optional[bool] = None, inferred_route: str = "",
                confidence: str = "", evidence: str = "", conn=None,
                now: Optional[float] = None) -> dict:
    """Record a sighting. First-seen is preserved forever; last-seen always advances.
    A row is only created for a real conversation URL — fail closed on anything else."""
    now = now if now is not None else now_ts()
    url = (conversation or "").strip().rstrip("/")
    if not wake_routes.valid_conversation(url):
        return {"ok": False, "reason": "not_a_conversation_url"}
    conn, own = _conn(conn)
    try:
        prior = conn.execute("SELECT conversation FROM chat_registry WHERE conversation=?",
                             (url,)).fetchone()
        if prior:
            sets, args = ["last_seen=?", "last_seen_ts=?"], [now_iso(), now]
            if title.strip():
                sets.append("title=?"); args.append(title.strip()[:200])
            if source:
                sets.append("source=?"); args.append(source[:80])
            if writable is not None:
                sets += ["writable=?", "writable_checked_at=?"]
                args += [1 if writable else 0, now_iso()]
                if writable:
                    # A proven write supersedes an old death verdict.
                    sets += ["active=1", "dead_reason=''"]
            if inferred_route:
                sets.append("inferred_route=?"); args.append(inferred_route[:64])
            if confidence:
                sets.append("confidence=?"); args.append(confidence[:32])
            if evidence:
                sets.append("evidence=?"); args.append(evidence[:300])
            args.append(url)
            conn.execute(f"UPDATE chat_registry SET {', '.join(sets)} WHERE conversation=?",
                         args)
            conn.commit()
            return {"ok": True, "action": "updated", "conversation": url}
        conn.execute(
            "INSERT INTO chat_registry (conversation,title,first_seen,first_seen_ts,"
            "last_seen,last_seen_ts,writable,writable_checked_at,inferred_route,confidence,"
            "evidence,source,active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (url, title.strip()[:200], now_iso(), now, now_iso(), now,
             (None if writable is None else (1 if writable else 0)),
             (now_iso() if writable is not None else None),
             inferred_route[:64], confidence[:32], evidence[:300], source[:80]))
        conn.commit()
        return {"ok": True, "action": "discovered", "conversation": url}
    finally:
        if own:
            conn.close()


def mark_dead(conversation: str, *, reason: str, conn=None,
              now: Optional[float] = None) -> dict:
    """A send fired and the page refused it, or an equivalent proof. Recorded, never
    inferred from silence — a timeout is not death, only a refusal is."""
    now = now if now is not None else now_ts()
    url = (conversation or "").strip().rstrip("/")
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "UPDATE chat_registry SET active=0, writable=0, dead_reason=?, "
            "writable_checked_at=?, last_seen=?, last_seen_ts=? WHERE conversation=?",
            (reason[:160], now_iso(), now_iso(), now, url))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO chat_registry (conversation,first_seen,first_seen_ts,last_seen,"
                "last_seen_ts,writable,writable_checked_at,active,dead_reason,source) "
                "VALUES (?,?,?,?,?,0,?,0,?,?)",
                (url, now_iso(), now, now_iso(), now, now_iso(), reason[:160],
                 "delivery-failure"))
        conn.commit()
        return {"ok": True, "conversation": url, "dead_reason": reason[:160]}
    finally:
        if own:
            conn.close()


def is_dead(conversation: str, conn=None) -> bool:
    url = (conversation or "").strip().rstrip("/")
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT active FROM chat_registry WHERE conversation=?",
                         (url,)).fetchone()
        return bool(r) and not r[0]
    finally:
        if own:
            conn.close()


def list_chats(active_only: bool = False, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        q = ("SELECT conversation,title,first_seen,last_seen,writable,inferred_route,"
             "confidence,evidence,source,active,dead_reason FROM chat_registry ")
        if active_only:
            q += "WHERE active=1 "
        q += "ORDER BY last_seen_ts DESC"
        return [{"conversation": r[0], "title": r[1], "first_seen": r[2], "last_seen": r[3],
                 "writable": (None if r[4] is None else bool(r[4])), "inferred_route": r[5],
                 "confidence": r[6], "evidence": r[7], "source": r[8],
                 "active": bool(r[9]), "dead_reason": r[10]} for r in conn.execute(q)]
    finally:
        if own:
            conn.close()


def discover_from_tabs(tabs: list, conn=None, now: Optional[float] = None) -> dict:
    """Inventory the ChatGPT conversations visible in a CDP page list.

    `tabs` is the parsed /json/list — dicts with `type`, `url`, `title`. Only chatgpt.com
    conversation pages are recorded; every other page, host or history entry is ignored,
    which is the entire privacy boundary of discovery.
    """
    seen = []
    for t in tabs or []:
        if (t.get("type") or "") != "page":
            continue
        url = (t.get("url") or "").split("?")[0].split("#")[0].rstrip("/")
        if not wake_routes.valid_conversation(url):
            continue
        title = (t.get("title") or "").strip()
        if title.lower() in ("chatgpt", ""):
            title = ""                      # generic SPA title carries no identity
        r = upsert_chat(url, title=title, source="tab-discovery", conn=conn, now=now)
        if r.get("ok"):
            seen.append({"conversation": url, "action": r["action"]})
    return {"discovered": seen}


# ── deterministic auto-bind ─────────────────────────────────────────────────
def _known_project_keys(conn) -> set:
    """Project identities the system already knows: distinct event project_ids and agent
    project_ids. Deterministic state, not a guess."""
    keys = set()
    for table, col in (("event", "project_id"), ("agent", "project_id")):
        try:
            for (p,) in conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} != ''"):
                k = wake_routes.normalize_key(p or "")
                if k:
                    keys.add(k)
        except Exception:  # noqa: BLE001 — a missing table is an empty contribution
            pass
    keys.discard(wake_routes.FALLBACK_ROUTE)
    return keys


def _title_matches(title: str, key: str) -> bool:
    """The project key must appear in the title as an explicit token — `mess` does not
    match `messenger`, because substring matching is how a guess sneaks in."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", title.lower()))


def consider_auto_bind(conversation: str, *, title: str = "", conn=None,
                       now: Optional[float] = None) -> dict:
    """Bind a project route to a discovered chat ONLY on deterministic evidence.

    Requirements, all of them: the chat is a valid conversation and not known dead; its
    title names exactly ONE known project key as an explicit token; that route is
    currently UNBOUND. Anything else — zero matches, two matches, an existing binding,
    the owner-os key — is a refusal with the reason recorded. Rebinding an existing route
    stays a purely explicit act (owner CLI/MCP), never an inference.
    """
    now = now if now is not None else now_ts()
    url = (conversation or "").strip().rstrip("/")
    if not wake_routes.valid_conversation(url):
        return {"bound": False, "reason": "not_a_conversation_url"}
    conn, own = _conn(conn)
    try:
        if is_dead(url, conn=conn):
            return {"bound": False, "reason": "chat_known_dead"}
        matches = sorted(k for k in _known_project_keys(conn)
                         if _title_matches(title or "", k))
        if not matches:
            return {"bound": False, "reason": "no_project_marker_in_title"}
        if len(matches) > 1:
            return {"bound": False, "reason": "ambiguous_project_markers",
                    "candidates": matches}
        key = matches[0]
        if wake_routes.get_route(key, conn=conn):
            return {"bound": False, "reason": "route_already_bound", "route_key": key}
        res = wake_routes.bind_route(
            key, url, by="auto-discovery",
            note=f"auto-bound: title names project '{key}' unambiguously", conn=conn,
            now=now)
        if not res.get("ok"):
            return {"bound": False, "reason": res.get("reason"), "route_key": key}
        upsert_chat(url, title=title, inferred_route=key, confidence="strong",
                    evidence=f"title token match: {key}", source="auto-bind", conn=conn,
                    now=now)
        return {"bound": True, "route_key": key, "conversation": url}
    finally:
        if own:
            conn.close()
