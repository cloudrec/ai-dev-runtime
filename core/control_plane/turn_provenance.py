"""Who wrote this turn — and the one answer this module will never give.

Text appearing in a pane or a chat is not evidence of who put it there. Owner OS
writes into panes itself: the companion submits wake phrases over CDP, the API
delivers continuations, the native supervisor continues agents. All of it lands in
the same composer a human types into, and once it is on screen the two are
indistinguishable by inspection.

That is not hypothetical. On 2026-09-13 an instruction reached this session's own pane
reading like an owner decision; the durable record says
`actor=api:bearer  source=172.20.0.4 ua=python-httpx/0.27.0`. An automation had written
it. Nothing on screen said so.

So this module answers a deliberately narrow question:

    "Is there durable proof that OWNER OS delivered this exact turn?"

`automated` when a delivery record matches. `unknown` otherwise. There is no third
value, and in particular there is no `owner` and no `human`: the absence of an
automation record is not proof that a person typed something. It means only that this
system has no record — an unfingerprinted delivery, an older build, a pruned row, a
path that never went through the API all look identical from here.

`is_owner_authority()` therefore returns False for every turn, always, and exists to
be called rather than reasoned around. Owner authority comes from an authenticated
channel that recorded an owner acting, never from prose in a pane.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

ORIGIN_AUTOMATED = "automated"
ORIGIN_UNKNOWN = "unknown"

# How long a delivery record may claim a turn. A fingerprint is not a nonce: the same
# phrase delivered days ago must not vouch for the same text appearing today, or an
# automated instruction becomes permanently replayable by anyone who can retype it.
MATCH_WINDOW_SECS = 900


def _now_ts() -> float:
    import time
    return time.time()


def _conn(conn=None):
    if conn is not None:
        return conn, False
    from core import agent_control as ac
    return ac._db(), True


def _unknown(reason: str) -> dict:
    return {"origin": ORIGIN_UNKNOWN, "actor": "", "source": "",
            "idempotency_key": "", "delivered_at": "", "age_secs": None,
            "is_owner_authority": False, "reason": reason}


def classify_turn(target: str, text: str, *, now: Optional[float] = None,
                  window_secs: int = MATCH_WINDOW_SECS, conn=None) -> dict:
    """Classify one pane turn as `automated` (proven) or `unknown` (everything else).

    A match requires all of: a delivery record for THIS target, whose fingerprint is
    the sha256 of exactly this text, recorded no longer than `window_secs` ago. Any
    missing piece is `unknown`, never a guess in the permissive direction.
    """
    now = now if now is not None else _now_ts()
    target = (target or "").strip()
    if not target:
        return _unknown("no_target")
    if not (text or "").strip():
        # An empty turn cannot be attributed to anything, and hashing "" would match
        # any other empty delivery. Refuse rather than produce a fingerprint collision.
        return _unknown("empty_text")
    from core import agent_control as ac
    digest = ac.text_fingerprint(text)
    conn, own = _conn(conn)
    try:
        try:
            row = conn.execute(
                "SELECT p.idempotency_key, p.recorded_ts, a.actor, a.source "
                "FROM delivery_provenance p "
                "LEFT JOIN delivery_attribution a ON a.idempotency_key = p.idempotency_key "
                "WHERE p.target=? AND p.text_sha256=? "
                "ORDER BY p.recorded_ts DESC LIMIT 1", (target, digest)).fetchone()
        except sqlite3.Error:
            # An unreadable store proves nothing was automated — and proves nothing was
            # human either. Fail closed to unknown rather than raise into a caller that
            # is probably deciding whether to trust an instruction.
            return _unknown("provenance_store_unreadable")
        if not row:
            return _unknown("no_matching_delivery")
        key, recorded_ts, actor, source = row
        age = now - float(recorded_ts or 0)
        if age > window_secs:
            return _unknown(f"match_older_than_window:{int(age)}s")
        if age < 0:
            # A record from the future is a clock fault, not evidence.
            return _unknown("match_timestamp_in_future")
        return {"origin": ORIGIN_AUTOMATED,
                "actor": actor or "",
                "source": source or "",
                "idempotency_key": key,
                "delivered_at": "",
                "age_secs": age,
                "is_owner_authority": False,
                "reason": "owner_os_delivered_this_exact_text"}
    finally:
        if own:
            conn.close()


def is_owner_authority(classification: Optional[dict] = None) -> bool:
    """Always False. Kept as a function so the rule is called, not remembered.

    No classification of pane text can establish that the owner authorised anything.
    `automated` proves the opposite; `unknown` proves nothing at all. An owner decision
    has to arrive through a channel that authenticated them and recorded it — see
    `core.control_plane.access_recovery`, which writes `actor="owner", authenticated=True`.
    """
    return False


def describe(classification: dict) -> str:
    """One line for a human or a log. Says what is known and refuses to imply more."""
    if not classification:
        return "turn provenance UNKNOWN — no classification"
    if classification.get("origin") == ORIGIN_AUTOMATED:
        who = classification.get("actor") or "an automation"
        src = classification.get("source") or "unrecorded source"
        return (f"turn was DELIVERED BY OWNER OS — {who} via {src}; "
                f"not owner sign-off")
    return (f"turn provenance UNKNOWN ({classification.get('reason', 'no reason')}) — "
            f"no record that Owner OS delivered it, which is NOT evidence a human did")
