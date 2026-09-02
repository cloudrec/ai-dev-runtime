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
    dead_reason TEXT DEFAULT '',
    dead_at_ts REAL
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
                    sets += ["active=1", "dead_reason=''", "dead_at_ts=NULL"]
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


_DEAD_AT_MIGRATION = ("dead_at_ts", "REAL")


def _ensure_dead_at(conn) -> None:
    """Additive: when the chat was marked dead. Needed so a dead mark can EXPIRE
    instead of silencing a project route forever (see wake_routes.resolve)."""
    try:
        conn.execute(f"ALTER TABLE chat_registry ADD COLUMN {_DEAD_AT_MIGRATION[0]} "
                     f"{_DEAD_AT_MIGRATION[1]}")
    except Exception:  # noqa: BLE001 — already present
        pass


def mark_dead(conversation: str, *, reason: str, conn=None,
              now: Optional[float] = None) -> dict:
    """A send fired and the page refused it, or an equivalent proof. Recorded, never
    inferred from silence — a timeout is not death, only a refusal is."""
    now = now if now is not None else now_ts()
    url = (conversation or "").strip().rstrip("/")
    conn, own = _conn(conn)
    try:
        _ensure_dead_at(conn)
        cur = conn.execute(
            "UPDATE chat_registry SET active=0, writable=0, dead_reason=?, "
            "writable_checked_at=?, last_seen=?, last_seen_ts=?, dead_at_ts=? "
            "WHERE conversation=?",
            (reason[:160], now_iso(), now_iso(), now, now, url))
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO chat_registry (conversation,first_seen,first_seen_ts,last_seen,"
                "last_seen_ts,writable,writable_checked_at,active,dead_reason,source,"
                "dead_at_ts) VALUES (?,?,?,?,?,0,?,0,?,?,?)",
                (url, now_iso(), now, now_iso(), now, now_iso(), reason[:160],
                 "delivery-failure", now))
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


def discover_from_links(links: list, *, source: str = "sidebar_discovery", conn=None,
                        now: Optional[float] = None) -> dict:
    """Inventory conversation links from ChatGPT's own sidebar / conversation list.

    `links` are {url, title} dicts read from same-origin `/c/` anchors in the logged-in
    page — the account-visible surface, so a chat created on another device appears here
    once the web app syncs it, without any server tab ever opening it. Only valid
    conversation URLs are recorded; nothing is opened and nothing is sent."""
    seen = []
    for it in links or []:
        url = (it.get("url") or "").split("?")[0].split("#")[0].rstrip("/")
        if not wake_routes.valid_conversation(url):
            continue
        title = (it.get("title") or "").strip()
        if title.lower() in ("chatgpt", ""):
            title = ""
        r = upsert_chat(url, title=title, source=source, conn=conn, now=now)
        if r.get("ok"):
            seen.append({"conversation": url, "action": r["action"], "title": title})
    return {"discovered": seen}


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


# Explicit owner-curated project markers, beyond the bare route key. Each entry is a
# small closed list — an alias earns its place here from OBSERVED owner usage (the chat
# titles the owner actually writes), never from similarity. Matching is still
# token-bounded, so `видео` does not fire inside another word.
PROJECT_ALIASES = {
    "mess": ("mess", "chemmy", "мессенджер", "хемми"),
    "gaika-video": ("gaika-video", "видео"),
    "payment-orchestrator": ("payment-orchestrator", "платёжка", "платежка",
                             "payment.clients.help", "sbp", "сбп"),
    "jobhunter-ai": ("jobhunter-ai", "jobhunter"),
    "gaika-drop": ("gaika-drop", "гайка-корзина", "gaika basket"),
}

_WORD = r"[a-z0-9а-яё.\-]"

# Combination rules: EVERY token in a tuple must appear (token-bounded) for the combo to
# count as a marker. This exists for projects whose vocabulary is made of generic words —
# `корзина` alone names nothing, but `корзина` together with `gaika`/`гайка`/`атб`+`варус`
# is the GAIKA Basket chat and nothing else. A generic word must never appear here alone.
PROJECT_ALIAS_COMBOS = {
    "gaika-drop": (("корзин", "gaika"), ("корзин", "гайка"), ("корзин", "атб", "варус"),
                   ("basket", "gaika")),
}


def _token_present(text: str, token: str) -> bool:
    """Token-bounded on the left; the right boundary tolerates Cyrillic inflection for
    Cyrillic stems (`корзин` matches `корзины`), while Latin tokens stay exact."""
    t = re.escape(token.lower())
    tail = "" if re.search(r"[а-яё]$", token.lower()) else rf"(?!{_WORD})"
    return bool(re.search(rf"(?<!{_WORD}){t}{tail}", (text or "").lower()))


def _alias_matches(title: str, key: str) -> bool:
    t = (title or "").lower()
    for alias in PROJECT_ALIASES.get(key, (key,)):
        if re.search(rf"(?<!{_WORD}){re.escape(alias.lower())}(?!{_WORD})", t):
            return True
    for combo in PROJECT_ALIAS_COMBOS.get(key, ()):
        if all(_token_present(t, tok) for tok in combo):
            return True
    return False


def match_projects(title: str, known_keys: set) -> list:
    """Every project the title names, via exact key token OR curated alias. The caller
    binds only when this list has exactly one member — two markers is ambiguity."""
    keys = set(known_keys) | set(PROJECT_ALIASES)
    keys.discard(wake_routes.FALLBACK_ROUTE)
    return sorted(k for k in keys
                  if _title_matches(title or "", k) or _alias_matches(title or "", k))


def consider_auto_bind(conversation: str, *, title: str = "", conn=None,
                       now: Optional[float] = None) -> dict:
    """Bind a project route to a discovered chat ONLY on deterministic evidence.

    Requirements, all of them: the chat is a valid conversation and not known dead; its
    title names exactly ONE project, by exact key token or curated alias
    (`PROJECT_ALIASES`); and the route is either UNBOUND, or bound to a chat that is
    RECORDED DEAD — the continuation case: the old doorbell provably refuses sends and
    the owner has titled a new chat with that project's marker. A healthy existing
    binding is never replaced, however new the chat; zero matches, two matches and the
    owner-os key are refusals with the reason recorded.
    """
    now = now if now is not None else now_ts()
    url = (conversation or "").strip().rstrip("/")
    if not wake_routes.valid_conversation(url):
        return {"bound": False, "reason": "not_a_conversation_url"}
    conn, own = _conn(conn)
    try:
        if is_dead(url, conn=conn):
            return {"bound": False, "reason": "chat_known_dead"}
        matches = match_projects(title or "", _known_project_keys(conn))
        if not matches:
            return {"bound": False, "reason": "no_project_marker_in_title"}
        if len(matches) > 1:
            return {"bound": False, "reason": "ambiguous_project_markers",
                    "candidates": matches}
        key = matches[0]
        # The conversation side of the same guarantee. Everything below protects the
        # ROUTE — a healthy binding is never replaced — and nothing protected the
        # CONVERSATION, so auto-discovery could pile a second and third project onto a
        # chat that already belonged to another one. It did: `owner-os`,
        # `payment-orchestrator` and `seo` all ended up on the one chat now titled
        # ПЛАТЁЖКА, which is 3 route keys sharing 1 doorbell out of 12 keys over 10
        # chats. Wakes for all three landed in one conversation and the other two
        # projects' chats were never rung, which is what an owner sees as "the loop
        # stopped, I have to poke it myself".
        #
        # Refuse, rather than rebind: moving a pointer is an owner decision about where
        # their messages land, and this function's whole license is deterministic
        # evidence. A title is mutable — the `seo` binding was made when this chat's
        # title still named seo, and the title changed afterwards — so a claim can be
        # true at bind time and false later. Refusing is the fail-closed half of that;
        # unwinding what a past rename left behind is not auto-discovery's call.
        for other in wake_routes.list_routes(conn=conn):
            if (other.get("conversation") or "").strip().rstrip("/") == url \
                    and other.get("route_key") != key:
                return {"bound": False, "reason": "conversation_already_bound_to_other_route",
                        "route_key": key, "held_by": other.get("route_key")}
        existing = wake_routes.get_route(key, conn=conn)
        continuation = False
        if existing:
            if not is_dead(existing["conversation"], conn=conn):
                return {"bound": False, "reason": "route_already_bound", "route_key": key}
            # Continuation: the bound chat is proven dead (a fired send the page refused)
            # and the new chat carries this project's marker unambiguously. Audited as a
            # rebind with the previous URL preserved, like every other pointer move.
            continuation = True
        note = (f"auto-rebound: previous chat recorded dead "
                f"({existing['conversation']}); new chat titled for '{key}'"
                if continuation else
                f"auto-bound: title names project '{key}' unambiguously")
        res = wake_routes.bind_route(key, url, by="auto-discovery", note=note, conn=conn,
                                     now=now)
        if not res.get("ok"):
            return {"bound": False, "reason": res.get("reason"), "route_key": key}
        upsert_chat(url, title=title, inferred_route=key, confidence="strong",
                    evidence=("continuation of dead route" if continuation
                              else f"title marker match: {key}"),
                    source="auto-bind", conn=conn, now=now)
        return {"bound": True, "route_key": key, "conversation": url,
                "continuation": continuation,
                "previous": (existing["conversation"] if existing else None)}
    finally:
        if own:
            conn.close()
