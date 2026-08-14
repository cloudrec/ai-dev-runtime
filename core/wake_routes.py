"""Wake route registry — WHICH ChatGPT chat an event's wake belongs to.

One global `wake_target` was the original design: a single doorbell, rotated by the owner.
That was correct while every event was owner-os traffic, and wrong the moment project agents
got their own work chats: a MESS event ringing the payments chat is a wrong-chat delivery
even when every URL involved is one the owner supplied.

This module is the canonical answer. One table, keyed by a stable ROUTE KEY — the event's
`project_id`, normalized — mapping to exactly one conversation URL. Resolution is
deterministic and fail-closed:

    explicit route for the event's project      -> that conversation   (explicit_route)
    no route for the project                    -> the owner-os route  (unmapped_route:<key>)
    no owner-os route either                    -> the legacy single wake_target row,
                                                   kept ONLY as a migration bridge
    nothing bound anywhere                      -> not bound; nothing is sent

Never a guess: a route exists only because someone bound it through `bind_route`, which
validates the URL with the same fail-closed predicate the bridge uses and audits every
change. An event whose project has no binding does not go hunting — it goes to the owner-os
fallback, labelled as unmapped, or nowhere.

This module deliberately imports nothing from `core.wake_bridge` (the bridge imports us),
so the URL validator lives here and the bridge re-exports it.
"""
from __future__ import annotations

import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# The fallback route key. Owner OS / runtime / infrastructure traffic is this route
# EXPLICITLY; everything unmapped lands here as a labelled fallback, never silently.
FALLBACK_ROUTE = "owner-os"

# Exactly a conversation: one of the two real ChatGPT hosts, the /c/ path, one id segment.
_CHAT_RE = re.compile(r"^https://(chatgpt\.com|chat\.openai\.com)/c/[A-Za-z0-9\-]+/?$")

# A route key is an identifier, not free text: it is used as a grouping key in SQL and
# printed in audit rows. Same shape as the project ids that actually occur in the event log.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wake_route (
    route_key TEXT PRIMARY KEY,
    conversation TEXT, bound_at TEXT, bound_ts REAL, bound_by TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS wake_route_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, action TEXT, route_key TEXT, conversation TEXT, previous TEXT,
    by TEXT, note TEXT
)
"""


def valid_conversation(url: str) -> bool:
    """A conversation URL, not an arbitrary page. Fail closed on anything else."""
    return bool(_CHAT_RE.match((url or "").strip()))


def normalize_key(key: str) -> str:
    """The canonical form of a route key; "" when the input cannot be one."""
    k = (key or "").strip().lower()
    return k if _KEY_RE.match(k) else ""


def route_key_for_event(project_id: str = "", source: str = "", agent_id: str = "") -> str:
    """The stable routing identity of an event. Project first; everything projectless —
    runtime, infrastructure, delivery meta-traffic — is owner-os traffic by definition."""
    return normalize_key(project_id) or FALLBACK_ROUTE


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def get_route(route_key: str, conn=None) -> Optional[dict]:
    key = normalize_key(route_key)
    if not key:
        return None
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT conversation,bound_at,bound_by,note FROM wake_route "
                         "WHERE route_key=?", (key,)).fetchone()
        if not r:
            return None
        return {"route_key": key, "conversation": r[0], "bound_at": r[1],
                "bound_by": r[2], "note": r[3]}
    finally:
        if own:
            conn.close()


def list_routes(conn=None) -> list:
    conn, own = _conn(conn)
    try:
        return [{"route_key": r[0], "conversation": r[1], "bound_at": r[2],
                 "bound_by": r[3], "note": r[4]}
                for r in conn.execute("SELECT route_key,conversation,bound_at,bound_by,note "
                                      "FROM wake_route ORDER BY route_key")]
    finally:
        if own:
            conn.close()


def bind_route(route_key: str, conversation: str, *, by: str = "owner", note: str = "",
               conn=None, now: Optional[float] = None) -> dict:
    """Bind one route to one conversation. Idempotent, audited, fail-closed.

    Validation happens BEFORE any write: a malformed key or URL leaves both tables
    untouched. Rebinding to the URL already held is a no-op PASS — repeated application
    converges instead of stacking audit noise.
    """
    now = now if now is not None else now_ts()
    key = normalize_key(route_key)
    if not key:
        return {"ok": False, "reason": "invalid_route_key", "route_key": (route_key or "")[:64]}
    url = (conversation or "").strip()
    if not valid_conversation(url):
        return {"ok": False, "reason": "not_a_conversation_url", "route_key": key,
                "conversation": url[:120]}
    conn, own = _conn(conn)
    try:
        prev = conn.execute("SELECT conversation FROM wake_route WHERE route_key=?",
                            (key,)).fetchone()
        previous = (prev[0] if prev else "") or ""
        if previous.rstrip("/") == url.rstrip("/"):
            return {"ok": True, "action": "unchanged", "route_key": key, "conversation": url}
        conn.execute(
            "INSERT INTO wake_route (route_key,conversation,bound_at,bound_ts,bound_by,note) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(route_key) DO UPDATE SET "
            "conversation=excluded.conversation, bound_at=excluded.bound_at, "
            "bound_ts=excluded.bound_ts, bound_by=excluded.bound_by, note=excluded.note",
            (key, url, now_iso(), now, by, note[:200]))
        conn.execute(
            "INSERT INTO wake_route_audit (ts,at,action,route_key,conversation,previous,by,note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, now_iso(), "rebind" if previous else "bind", key, url, previous, by,
             note[:200]))
        conn.commit()
        return {"ok": True, "action": "rebind" if previous else "bind", "route_key": key,
                "conversation": url, "previous": previous or None}
    finally:
        if own:
            conn.close()


def unbind_route(route_key: str, *, by: str = "owner", note: str = "", conn=None,
                 now: Optional[float] = None) -> dict:
    """Remove a route. Audited; events for the key fall back to owner-os from then on."""
    now = now if now is not None else now_ts()
    key = normalize_key(route_key)
    if not key:
        return {"ok": False, "reason": "invalid_route_key"}
    conn, own = _conn(conn)
    try:
        prev = conn.execute("SELECT conversation FROM wake_route WHERE route_key=?",
                            (key,)).fetchone()
        if not prev:
            return {"ok": True, "action": "absent", "route_key": key}
        conn.execute("DELETE FROM wake_route WHERE route_key=?", (key,))
        conn.execute(
            "INSERT INTO wake_route_audit (ts,at,action,route_key,conversation,previous,by,note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, now_iso(), "unbind", key, "", prev[0] or "", by, note[:200]))
        conn.commit()
        return {"ok": True, "action": "unbind", "route_key": key, "previous": prev[0]}
    finally:
        if own:
            conn.close()


def route_history(limit: int = 50, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        return [{"at": r[0], "action": r[1], "route_key": r[2], "conversation": r[3],
                 "previous": r[4], "by": r[5], "note": r[6]}
                for r in conn.execute(
                    "SELECT at,action,route_key,conversation,previous,by,note "
                    "FROM wake_route_audit ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        if own:
            conn.close()


def _legacy_target(conn) -> str:
    """The pre-registry single wake_target row. Read-only migration bridge — this module
    never writes it, and resolution consults it only when the registry has no owner-os
    route at all."""
    try:
        r = conn.execute("SELECT conversation FROM wake_target WHERE id=1").fetchone()
        return ((r[0] if r else "") or "").strip()
    except Exception:  # noqa: BLE001 — table may not exist in a fresh DB
        return ""


def resolve(*, project_id: str = "", source: str = "", agent_id: str = "",
            conn=None) -> dict:
    """Event -> target, deterministically, resolved FRESH at call time.

    Returns {"bound": bool, "conversation", "route_key", "route_reason"}. The reason names
    which rule spoke: `explicit_route`, `owner_os_route`, `unmapped_route:<key>` (fallback
    used because the key has no binding), each with `:legacy_single_target` appended when
    the fallback came from the unmigrated single-target row. Unbound is a refusal to send,
    never a guess.
    """
    key = route_key_for_event(project_id, source, agent_id)
    conn, own = _conn(conn)
    try:
        r = get_route(key, conn=conn)
        if r and valid_conversation(r["conversation"]):
            reason = "explicit_route" if key != FALLBACK_ROUTE else "owner_os_route"
            return {"bound": True, "conversation": r["conversation"], "route_key": key,
                    "route_reason": reason}
        unmapped = f"unmapped_route:{key}" if key != FALLBACK_ROUTE else "owner_os_route"
        fb = get_route(FALLBACK_ROUTE, conn=conn)
        if fb and valid_conversation(fb["conversation"]):
            return {"bound": True, "conversation": fb["conversation"],
                    "route_key": FALLBACK_ROUTE, "route_reason": unmapped}
        legacy = _legacy_target(conn)
        if valid_conversation(legacy):
            return {"bound": True, "conversation": legacy, "route_key": FALLBACK_ROUTE,
                    "route_reason": f"{unmapped}:legacy_single_target"}
        return {"bound": False, "reason": "no_route_bound", "route_key": key}
    finally:
        if own:
            conn.close()


def migrate_legacy_target(*, by: str = "migration", conn=None) -> dict:
    """Copy the single wake_target row into the registry as the owner-os route, once.

    Idempotent: an existing owner-os route is never overwritten (the registry is already
    canonical), and a missing/invalid legacy row migrates nothing. The legacy row itself is
    left in place — `wake_bridge.bind_chat` keeps it in lockstep for old readers.
    """
    conn, own = _conn(conn)
    try:
        if get_route(FALLBACK_ROUTE, conn=conn):
            return {"ok": True, "action": "already_migrated"}
        legacy = _legacy_target(conn)
        if not valid_conversation(legacy):
            return {"ok": False, "reason": "no_valid_legacy_target"}
        return bind_route(FALLBACK_ROUTE, legacy, by=by,
                          note="migrated from single wake_target row", conn=conn)
    finally:
        if own:
            conn.close()
