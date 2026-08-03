"""Notifier — durable outbox drain with retry + dead-letter (P3).

Attempts each pending/failed notification through the fail-closed delivery matrix
(`delivery.deliver`), recording receipts. Bounded retry: after `max_attempts` failures a
notification is dead-lettered and a critical event is raised — a stuck/disabled channel is
never silent. Retry cadence is the engine loop interval (default 30s).

Sends notifications only; touches NO agent pane.
"""
from __future__ import annotations

from core.control_plane import api, delivery
from core.control_plane.cto import emit

MAX_ATTEMPTS = 5


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
                 dedup_key=f"deadletter:{n['id']}", push=False, conn=conn)  # inbox-only, no recursion
            dead += 1
            continue
        out = delivery.deliver(n["id"], conn=conn)
        if out.get("delivered"):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "dead_letter": dead}
