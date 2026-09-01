"""Notifier — durable outbox drain with retry + dead-letter (P3).

Attempts each pending/failed notification through the fail-closed delivery matrix
(`delivery.deliver`), recording receipts. Bounded retry: after `max_attempts` failures a
notification is dead-lettered and a critical event is raised — a stuck/disabled channel is
never silent. Retry cadence is the engine loop interval (default 30s).

Sends notifications only; touches NO agent pane.
"""
from __future__ import annotations

import os

from core.control_plane import api, delivery
from core.control_plane.cto import emit

MAX_ATTEMPTS = 5

# How long one dead channel speaks for itself. The meaning of a dead letter is "this
# CHANNEL is not delivering", which is one standing fact, not one fact per message.
DEAD_LETTER_DEDUP_SECS = int(os.getenv("NOTIFIER_DEAD_LETTER_DEDUP_SECS", "900"))


# The proactive tiers `delivery.deliver` walks, in its order. Named here because the
# dead-letter alarm has to be able to say WHY, and the reason is per-tier.
_TIERS = ("same_chat_wake", "owner_push")


def _failure_reasons(conn=None) -> dict:
    """Why each proactive tier is not delivering, from the stored channel evidence.

    `deliver()` computes a real reason per tier on every attempt and returns it in
    `attempts[].detail`, but the dead-letter branch fires on a LATER drain, when that
    return value is long gone — so the alarm carried none of it. Measured: 311 critical
    dead letters in 24 h whose entire payload was an id, a channel name, an attempt
    count and a dedup key, while the cause sat one table away the whole time
    ("Bad Request: chat not found" — a chat id the bot cannot post to).

    Read from `channel.last_error`, which delivery already maintains as the durable
    record of the last real rejection. Best-effort by construction: an alarm that
    cannot be raised because its explanation failed to load is strictly worse than an
    unexplained alarm.
    """
    out = {}
    for tier in _TIERS:
        try:
            row = api.get_channel(tier, conn=conn) or {}
        except Exception:  # noqa: BLE001 — never block the alarm on its own annotation
            continue
        err = (row.get("last_error") or "").strip()
        if err:
            out[tier] = err[:300]
    return out


def drain(*, max_attempts: int = MAX_ATTEMPTS, conn=None) -> dict:
    sent = failed = dead = 0
    # Read once per drain, not once per dead letter: the channel state cannot change
    # inside a pass, and a backlog is exactly when this runs over many rows at once.
    reasons = None
    for n in api.pending_notifications(conn=conn):
        if n["attempts"] >= max_attempts:
            api.mark_notification(n["id"], "dead_letter", conn=conn)
            if reasons is None:
                reasons = _failure_reasons(conn=conn)
            # The one line an owner actually reads. "delivery channel unhealthy" names
            # the symptom they already knew about from the severity; the rejection text
            # is the part that says what to change.
            why = "; ".join(f"{t}: {r}" for t, r in reasons.items())
            emit("notifier", "notification_dead_letter", severity="critical",
                 owner_action_required=True,
                 payload={"notification_id": n["id"], "channel": n["channel"],
                          "attempts": n["attempts"], "dedup_key": n["dedup_key"],
                          "reasons": reasons},
                 action_taken=("dead-lettered after max attempts — "
                               + (why or "delivery channel unhealthy"))[:400],
                 # Keyed by CHANNEL, not by notification id. An id is unique by
                 # construction, so the old key deduped nothing: a channel that has been
                 # down for weeks minted a fresh critical owner_action_required event for
                 # every single message — 937 events under 937 distinct keys in 24h,
                 # the largest event type on this host, all for one unchanging cause.
                 # Nothing is lost: every dead-lettered message is still recorded
                 # individually in the `notification` table, which is the per-message
                 # ledger. This event is the per-channel alarm.
                 dedup_key=f"deadletter:{n['channel']}",
                 dedup_window_secs=DEAD_LETTER_DEDUP_SECS,
                 push=False, conn=conn)  # inbox-only, no recursion
            dead += 1
            continue
        out = delivery.deliver(n["id"], conn=conn)
        if out.get("delivered"):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "dead_letter": dead}
