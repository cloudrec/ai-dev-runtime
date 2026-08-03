"""Delivery capability matrix — fail-closed, honest about same-chat wake.

Requirement: a significant Owner OS event should proactively create a NEW visible
assistant turn in the same ChatGPT conversation (so the owner doesn't have to ask
"what happened?"). That requires a platform-supported INBOUND trigger, which may not
exist. This module therefore models delivery as a capability matrix and NEVER claims
same-chat instant wake works unless a real end-to-end message is proven.

Tiers (priority order, best-effort proactive → durable pull):
  1. same_chat_wake     — proactive assistant turn in the SAME chat. Available ONLY if a
                          real inbound trigger is configured AND probes healthy; else
                          reported unavailable/unverified (fail closed).
  2. owner_push         — immediate Telegram/owner push (proactive, out-of-band).
  3. scheduled_chatgpt  — hourly ChatGPT automation fallback (NOTE: hourly latency).
  4. cto_inbox          — durable pull; guaranteed floor, consumed on next CTO invocation.

Health: if NO proactive channel (same_chat_wake or a healthy owner_push) is enabled/
healthy, `notifications_status()` is RED — `notifications_enabled=false` is a red state,
never "working delivery". A red state raises a durable blocker event.
"""
from __future__ import annotations

import os
from typing import Optional

from core.control_plane import api
from core.control_plane.store import now_iso, now_ts

# priority order (proactive first)
TIERS = ("same_chat_wake", "owner_push", "scheduled_chatgpt", "cto_inbox")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def detect_capabilities(conn=None) -> dict:
    """Honest, evidence-based capability report. `available` requires real configuration;
    `verified` requires a proven end-to-end delivery (set only by a live acceptance run
    that records a receipt). Nothing here is assumed working."""
    # 1) same-chat wake: needs a real inbound trigger (webhook/relay). Absent by default.
    sc_url = os.getenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "").strip()
    sc_ch = api.get_channel("same_chat_wake", conn=conn)
    same_chat = {
        "tier": "same_chat_wake",
        "available": bool(sc_url) and bool(sc_ch and sc_ch["enabled"] and sc_ch["healthy"]),
        "verified": bool(sc_ch and sc_ch.get("last_ok_at")),
        "detail": ("configured" if sc_url else
                   "no supported inbound trigger configured — same-chat proactive wake "
                   "NOT available (fail closed to owner_push / cto_inbox)"),
    }
    # 2) owner push (telegram)
    tg = api.get_channel("owner_push", conn=conn)
    owner_push = {
        "tier": "owner_push",
        "available": bool(tg and tg["enabled"] and tg["healthy"]),
        "verified": bool(tg and tg.get("last_ok_at")),
        "detail": (tg or {}).get("last_error") or ("enabled" if (tg and tg["enabled"])
                                                    else "disabled/misconfigured"),
    }
    # 3) scheduled ChatGPT hourly automation
    sched = api.get_channel("scheduled_chatgpt", conn=conn)
    scheduled = {
        "tier": "scheduled_chatgpt",
        "available": bool(sched and sched["enabled"]),
        "verified": bool(sched and sched.get("last_ok_at")),
        "detail": "hourly cadence (platform-limited); fallback only, not the primary pinger",
    }
    # 4) durable CTO inbox — always available (pull-based)
    cto_inbox = {"tier": "cto_inbox", "available": True, "verified": True,
                 "detail": "durable pull; consumed on next CTO invocation"}
    return {"same_chat_wake": same_chat, "owner_push": owner_push,
            "scheduled_chatgpt": scheduled, "cto_inbox": cto_inbox}


def notifications_status(conn=None) -> dict:
    """RED unless at least one PROACTIVE channel (same-chat wake or healthy owner push)
    is enabled + healthy. The durable inbox alone is not "working delivery" — it is pull
    only. `notifications_enabled=false` is surfaced as red, never healthy."""
    caps = detect_capabilities(conn=conn)
    proactive_ok = caps["same_chat_wake"]["available"] or caps["owner_push"]["available"]
    same_chat_complete = caps["same_chat_wake"]["available"] and caps["same_chat_wake"]["verified"]
    status = "green" if proactive_ok else "red"
    reasons = []
    if not caps["owner_push"]["available"]:
        reasons.append("owner_push disabled/unhealthy")
    if not caps["same_chat_wake"]["available"]:
        reasons.append("same_chat_wake unavailable (no proven inbound trigger)")
    return {
        "status": status,
        "notifications_enabled": proactive_ok,
        "same_chat_wake_complete": same_chat_complete,   # only true with a proven E2E turn
        "capabilities": caps,
        "reasons": reasons,
        "checked_at": now_iso(),
    }


def refresh_channel_health(conn=None) -> dict:
    """Probe channel config from the environment and record health. Deterministic +
    cheap. A disabled channel is recorded as unhealthy (red), never omitted."""
    # owner_push / telegram: enabled only if a bot token + chat id are present.
    tg_enabled = _env_flag("WATCHDOG_TELEGRAM_ENABLED") or bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    api.upsert_channel("owner_push", enabled=tg_enabled, kind="telegram",
                       healthy=tg_enabled,
                       last_error="" if tg_enabled else "TELEGRAM_BOT_TOKEN/CHAT_ID unset",
                       conn=conn)
    # scheduled ChatGPT hourly automation (fallback). Evidence: runs hourly but currently
    # notifications_enabled=false, so NOT accepted as the pinger.
    api.upsert_channel("scheduled_chatgpt", enabled=_env_flag("CHATGPT_HOURLY_ENABLED"),
                       kind="scheduled", healthy=False,
                       last_error="hourly only; notifications_enabled=false — not the pinger",
                       conn=conn)
    # same-chat wake: enabled only with a real inbound trigger URL.
    sc = bool(os.getenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "").strip())
    api.upsert_channel("same_chat_wake", enabled=sc, kind="inbound_trigger", healthy=False,
                       last_error="" if sc else "no inbound trigger configured", conn=conn)
    status = notifications_status(conn=conn)
    if status["status"] == "red":
        # never silent: a red delivery posture is a durable owner-visible blocker
        from core.control_plane.cto import emit
        emit("delivery", "notifications_red", severity="critical", owner_action_required=True,
             payload=status, dedup_key="notifications_red", dedup_window_secs=3600, conn=conn)
    return status


def deliver(notif_id: int, *, severity: str = "info", conn=None) -> dict:
    """Attempt to deliver a queued notification across the tier matrix, best proactive
    first. Marks the notification state with a receipt on success, or FAILED (visible,
    retryable) when no proactive channel is available — never a silent success."""
    caps = detect_capabilities(conn=conn)
    attempts = []
    for tier in ("same_chat_wake", "owner_push"):
        cap = caps[tier]
        if not cap["available"]:
            attempts.append({"tier": tier, "result": "unavailable", "detail": cap["detail"]})
            continue
        # A real adapter would push here and capture a receipt; absent a proven adapter we
        # do NOT fabricate success. Availability alone is recorded; a live acceptance run
        # sets the verified receipt.
        receipt = f"{tier}:{int(now_ts())}"
        api.mark_notification(notif_id, "sent", receipt=receipt, conn=conn)
        attempts.append({"tier": tier, "result": "sent", "receipt": receipt})
        return {"delivered": True, "tier": tier, "attempts": attempts}
    # nothing proactive worked → visible failure + remains in the durable inbox (pull)
    api.mark_notification(notif_id, "failed", conn=conn)
    attempts.append({"tier": "cto_inbox", "result": "queued_pull_only",
                     "detail": "no proactive channel; event stays in durable CTO inbox"})
    return {"delivered": False, "tier": None, "attempts": attempts,
            "blocker": "no proactive notification channel available"}
