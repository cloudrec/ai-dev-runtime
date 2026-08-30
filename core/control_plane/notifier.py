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


def drain(*, max_attempts: int = MAX_ATTEMPTS, conn=None) -> dict:
    sent = failed = dead = 0
    for n in api.pending_notifications(conn=conn):
        if n["attempts"] >= max_attempts:
            api.mark_notification(n["id"], "dead_letter", conn=conn)
            emit("notifier", "notification_dead_letter", severity="critical",
                 owner_action_required=True,
                 payload={"notification_id": n["id"], "channel": n["channel"],
                          "attempts": n["attempts"], "dedup_key": n["dedup_key"]},
                 action_taken="dead-lettered after max attempts — delivery channel unhealthy",
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
