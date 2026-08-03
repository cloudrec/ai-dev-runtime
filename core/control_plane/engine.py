"""Control Plane V2 engine loop — P1 SHADOW (observe-only).

Runs the continuous discovery + channel-health pass inside the always-on daemon. This
phase is READ-ONLY with respect to panes: it discovers/classifies/monitors agents and
records the durable source of truth + CTO inbox, but issues NO commands. Actuation
(controllers under lease) is P2+/P4 and gated by owner cutover (G1).

Gated by CONTROL_PLANE_ENABLED (default on — visibility must not depend on config).
"""
from __future__ import annotations

import os

ENABLED = os.getenv("CONTROL_PLANE_ENABLED", "1") not in ("0", "false", "no", "")
INTERVAL = int(os.getenv("CONTROL_PLANE_INTERVAL_SECS", "30"))


def tick_once() -> dict:
    from core.control_plane import discovery, delivery, notifier
    disc = discovery.discover()                    # observe-only registry reconcile
    health = delivery.refresh_channel_health()     # fail-closed delivery posture
    notif = notifier.drain()                        # durable outbox: attempt/retry/dead-letter
    return {"discovery": disc, "notifications": health["status"], "outbox": notif}


async def run_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger("control_plane")
    if not ENABLED:
        log.info("control plane disabled")
        return
    log.info(f"control plane engine started (SHADOW/observe-only, interval {INTERVAL}s)")
    while True:
        try:
            res = await asyncio.to_thread(tick_once)
            d = res["discovery"]
            if d.get("events") or d.get("dead") or d.get("recovered") or d.get("duplicates"):
                log.info(f"control plane: discovered={d['discovered']} managed={d['managed']} "
                         f"observe={d['observe_only']} dup={d['duplicates']} dead={d['dead']} "
                         f"recovered={d['recovered']} notif={res['notifications']}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"control plane tick error: {e}")
        await asyncio.sleep(INTERVAL)
