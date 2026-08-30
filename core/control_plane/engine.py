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
# Work evidence is filesystem + git work, so it runs on its own slower cadence rather than
# on every 30s tick. Process-local: a restart costs one extra scan, which is deduped anyway.
WORK_EVIDENCE_INTERVAL = int(os.getenv("WORK_EVIDENCE_INTERVAL_SECS", "300"))
_last_work_scan = 0.0


def tick_once() -> dict:
    import time
    global _last_work_scan
    from core.control_plane import discovery, delivery, notifier, pinger_shadow
    disc = discovery.discover()                    # observe-only registry reconcile
    health = delivery.refresh_channel_health()     # fail-closed delivery posture
    notif = notifier.drain()                        # durable outbox: attempt/retry/dead-letter
    # cp-canary SHADOW pinger — scope-confined significant-event emission (observe-only).
    # Best-effort: a pinger failure must never break discovery/health/outbox.
    try:
        ping = pinger_shadow.shadow_tick()
    except Exception as e:  # noqa: BLE001
        ping = {"error": str(e)}
    # WORK EVIDENCE — reports, commits and partial completions. The state pinger above only
    # sees an agent's observable state, so work that finished (or stopped half-finished)
    # while the stage pointer stood still was invisible until this ran.
    work = {"skipped": "interval"}
    if (time.time() - _last_work_scan) >= WORK_EVIDENCE_INTERVAL:
        _last_work_scan = time.time()
        try:
            from core import work_evidence
            work = work_evidence.scan()
        except Exception as e:  # noqa: BLE001 — never break the tick for an observer
            work = {"error": str(e)}
    return {"discovery": disc, "notifications": health["status"], "outbox": notif,
            "pinger": ping, "work_evidence": work}


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
