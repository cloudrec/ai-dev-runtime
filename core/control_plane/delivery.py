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

owner_push health is EVIDENCE-SCOPED, never configuration-derived (see `_owner_push_state`):
having a bot token is not delivery. After a cold start or a service restart the channel is
explicitly `unverified` (amber, NOT green) until a send is proven in THIS runtime; a proven
failure makes it `unhealthy`; a proven send makes it `healthy` with a timestamp + receipt.
"""
from __future__ import annotations

import os
from typing import Optional

from core.control_plane import api
from core.control_plane.store import now_iso, now_ts, runtime_epoch

# priority order (proactive first)
TIERS = ("same_chat_wake", "owner_push", "scheduled_chatgpt", "cto_inbox")

UNVERIFIED_DETAIL = ("unverified — credentials present but no delivery proven in this "
                     "runtime (not green until a real receipt)")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _owner_push_state(row: Optional[dict], *, enabled: bool) -> tuple:
    """Decide owner_push's state from EVIDENCE, never from configuration.

    Configuration presence answers "could this channel be tried?", not "does it work?".
    The live 2026-08-06 failure was exactly this conflation: creds present ⇒ healthy=1 ⇒
    green, while every send returned `Bad Request: chat not found`.

    Returns (state, last_error):
      * not configured                       → unhealthy (red, misconfigured)
      * proof recorded in THIS runtime epoch → keep it (healthy or unhealthy; a proven
        failure must survive every subsequent refresh tick, not be reset by the next probe)
      * anything else (cold start, restart, pre-v5 row) → unverified: NOT green, and the
        historical last_ok_at/last_proof stay on the row as evidence of what once worked.
    """
    if not enabled:
        return ("unhealthy", "TELEGRAM_BOT_TOKEN/CHAT_ID unset")
    if row and row.get("state") in ("healthy", "unhealthy") and row.get("proven_this_runtime"):
        return (row["state"], row.get("last_error") or "")
    return ("unverified", UNVERIFIED_DETAIL)


def _owner_push_detail(row: Optional[dict], state: str) -> str:
    """Human-readable posture for the UI — says WHY, not just red/green."""
    if not (row and row["enabled"]):
        return "disabled/misconfigured"
    if state == "healthy":
        return f"delivery proven at {row.get('last_ok_at')} ({row.get('last_proof') or 'receipt'})"
    return row.get("last_error") or UNVERIFIED_DETAIL


CDP_WAKE_WINDOW_SECS = int(os.getenv("CDP_WAKE_EVIDENCE_SECS", "3600"))


def _cdp_same_chat(conn=None) -> dict:
    """The proactive channel that actually works here, reported from EVIDENCE.

    `same_chat_wake` means "a proactive assistant turn in the SAME chat", and it is
    modelled as needing an inbound trigger URL — a webhook that makes ChatGPT speak.
    No such API exists: nothing outside a browser can push a turn into a user's
    conversation. So that tier is permanently unavailable in this environment, and it
    always will be.

    Meanwhile the CDP composer does exactly what the tier describes: it types into the
    bound conversation and the assistant answers. Measured 2026-09-04: 267 deliveries
    proven in 24 h across nine routes, every one recorded in `wake_delivery` with
    `delivered=1`. `notifications_status()` nonetheless reported red and
    `notifications_enabled=False`, because it enumerated only the URL tier and Telegram
    and was blind to the one channel carrying the traffic.

    That is not a cosmetic mislabel. It told the owner there was no proactive channel
    while 267 wakes were landing, which is precisely backwards for someone trying to
    decide what to fix.

    Evidence, never configuration: `available` requires a delivery PROVEN inside the
    window, so a broken browser or an empty log fails closed within the hour. `verified`
    means it has ever worked.
    """
    from core.control_plane.api import _c
    conn, own = _c(conn)
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, MAX(ts) last FROM wake_delivery "
            "WHERE delivered=1 AND ts > ?", (now_ts() - CDP_WAKE_WINDOW_SECS,)).fetchone()
        recent = int(row[0] or 0) if row else 0
        last_ts = (row[1] if row else None)
        ever = conn.execute(
            "SELECT COUNT(*) FROM wake_delivery WHERE delivered=1").fetchone()[0]
    except Exception:  # noqa: BLE001 — an unreadable log proves nothing; fail closed
        return {"tier": "cdp_same_chat", "available": False, "verified": False,
                "detail": "delivery log unreadable — cannot prove a proactive wake"}
    finally:
        if own:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return {
        "tier": "cdp_same_chat",
        "available": recent > 0,
        "verified": bool(ever),
        "proven_in_window": recent,
        "window_secs": CDP_WAKE_WINDOW_SECS,
        "last_delivered_ts": last_ts,
        "detail": ("%d delivery(s) proven in the last %ds" % (recent, CDP_WAKE_WINDOW_SECS)
                   if recent else
                   "no delivery proven in the window — browser wake not currently working"),
    }


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
    # 2) owner push (telegram) — evidence-scoped: `available` requires a PROVEN delivery in
    # this runtime, never merely present credentials. `state` distinguishes "not yet proven"
    # (unverified/amber) from "proven broken" (unhealthy/red) so the UI can tell them apart.
    tg = api.get_channel("owner_push", conn=conn)
    tg_state = (tg or {}).get("state") or "unverified"
    owner_push = {
        "tier": "owner_push",
        "available": bool(tg and tg["enabled"] and tg_state == "healthy"),
        "verified": bool(tg and tg.get("last_ok_at")),      # historical: ever proven
        "state": tg_state,
        "configured": bool(tg and tg["enabled"]),
        "proven_this_runtime": bool((tg or {}).get("proven_this_runtime")),
        "proven_at": (tg or {}).get("last_ok_at"),
        "evidence": (tg or {}).get("last_proof"),
        "detail": _owner_push_detail(tg, tg_state),
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
            "cdp_same_chat": _cdp_same_chat(conn),
            "scheduled_chatgpt": scheduled, "cto_inbox": cto_inbox}


def notifications_status(conn=None) -> dict:
    """RED unless at least one PROACTIVE channel (same-chat wake or healthy owner push)
    is enabled + healthy. The durable inbox alone is not "working delivery" — it is pull
    only. `notifications_enabled=false` is surfaced as red, never healthy."""
    caps = detect_capabilities(conn=conn)
    # NOTIFIER tiers only. `notifier._TIERS` is ("same_chat_wake", "owner_push") and
    # nothing in that module can reach the browser, so `cdp_same_chat` must NOT make this
    # green: it would claim owner alerts are being delivered while every one of them
    # dead-letters. Corrected 2026-09-04 within the hour of introducing it — status read
    # green with 19 active dead letters, which is the same lie as the red one it replaced,
    # pointing the other way.
    #
    # The browser capability is still REPORTED below, because it is true and it is what
    # the owner needs to know: wakes are landing even though alerts are not.
    proactive_ok = caps["same_chat_wake"]["available"] or caps["owner_push"]["available"]
    same_chat_complete = caps["same_chat_wake"]["available"] and caps["same_chat_wake"]["verified"]
    status = "green" if proactive_ok else "red"
    reasons = []
    if not caps["owner_push"]["available"]:
        op_state = caps["owner_push"]["state"]
        reasons.append("owner_push unverified — no delivery proven since start"
                       if op_state == "unverified" else f"owner_push disabled/{op_state}")
    if not caps["same_chat_wake"]["available"]:
        # Stated as a PLATFORM boundary, not a misconfiguration: there is no API by
        # which a server can make ChatGPT speak, so this tier cannot be configured here
        # and chasing it is wasted effort.
        reasons.append("same_chat_wake unavailable — no server->ChatGPT inbound trigger "
                       "exists on this platform; browser wake covers this instead")
    # Reported either way: its presence explains why agents keep working while this
    # posture is red, and its absence is the more serious of the two failures.
    reasons.append("cdp_same_chat (wake path, NOT a notifier tier): "
                   + caps["cdp_same_chat"]["detail"])
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
    # owner_push / telegram: CONFIGURATION says it can be tried; only EVIDENCE says it works.
    # A probe therefore never invents health — it re-derives state from the stored proof, so
    # a cold start is `unverified` and a proven failure is not reset by the next tick.
    tg_enabled = _env_flag("WATCHDOG_TELEGRAM_ENABLED") or bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    tg_state, tg_error = _owner_push_state(api.get_channel("owner_push", conn=conn),
                                           enabled=tg_enabled)
    api.upsert_channel("owner_push", enabled=tg_enabled, kind="telegram",
                       state=tg_state, last_error=tg_error, conn=conn)
    # scheduled ChatGPT hourly automation (fallback). Evidence: runs hourly but currently
    # notifications_enabled=false, so NOT accepted as the pinger.
    api.upsert_channel("scheduled_chatgpt", enabled=_env_flag("CHATGPT_HOURLY_ENABLED"),
                       kind="scheduled", healthy=False,
                       last_error="hourly only; notifications_enabled=false — not the pinger",
                       conn=conn)
    # same-chat wake: enabled only with a real inbound trigger URL.
    sc = bool(os.getenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "").strip())
    # configured ⇒ presumed reachable (a failed send flips it via the delivery attempt);
    # unconfigured ⇒ unhealthy. Either way `verified` still requires a real recorded receipt.
    api.upsert_channel("same_chat_wake", enabled=sc, kind="inbound_trigger", healthy=sc,
                       last_error="" if sc else "no inbound trigger configured", conn=conn)
    status = notifications_status(conn=conn)
    if status["status"] == "red":
        # never silent: a red delivery posture is a durable owner-visible blocker
        from core.control_plane.cto import emit
        emit("delivery", "notifications_red", severity="critical", owner_action_required=True,
             payload=status, dedup_key="notifications_red", dedup_window_secs=3600,
             push=False, conn=conn)          # inbox-only: pushing a down-channel alert would recurse
    return status


def _send_owner_push(message: str) -> tuple:
    """Real Telegram owner-push, HARD-GATED. With no TELEGRAM_BOT_TOKEN/CHAT_ID this returns
    (False, None, reason) and makes NO network call. When configured, POSTs sendMessage and
    returns a receipt only on a real 2xx `ok` response — never a fabricated success."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return (False, None, "owner_push credentials unset (TELEGRAM_BOT_TOKEN/CHAT_ID)")
    try:
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat, "text": message[:4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:   # noqa: S310 — fixed telegram host
            status = getattr(r, "status", 200)
            body = _json.loads(r.read().decode() or "{}")
        if status // 100 == 2 and body.get("ok"):
            mid = (body.get("result") or {}).get("message_id")
            return (True, f"telegram:{mid}", None)
        return (False, None, f"telegram rejected: {str(body)[:160]}")
    except urllib.error.HTTPError as e:
        # Telegram's actual reason lives in the error response BODY ("description"),
        # never in str(e) — that's just "HTTP Error 400: Bad Request" for every 4xx,
        # indistinguishable whether the cause is a bad chat_id, an unstarted bot, a
        # malformed request, or something else entirely. Read it so a red owner_push
        # is diagnosable instead of a generic code-and-nothing-else.
        try:
            detail = _json.loads(e.read().decode() or "{}").get("description") or str(e)
        except Exception:  # noqa: BLE001
            detail = str(e)
        return (False, None, f"telegram send failed: {detail}")
    except Exception as e:  # noqa: BLE001
        return (False, None, f"telegram send failed: {e}")


def _send_same_chat(message: str) -> tuple:
    """Real same-chat inbound-trigger POST, HARD-GATED. With no CONTROL_PLANE_SAMECHAT_WAKE_URL
    this returns (False, None, reason) and makes NO network call. When a relay URL is
    configured, POSTs the event JSON; returns a receipt only on a real 2xx response."""
    url = os.getenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "").strip()
    if not url:
        return (False, None, "no inbound trigger configured (CONTROL_PLANE_SAMECHAT_WAKE_URL)")
    try:
        import json as _json
        import urllib.request
        payload = _json.dumps({"text": message[:4000], "source": "owner_os_pinger"}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        token = os.getenv("CONTROL_PLANE_SAMECHAT_WAKE_TOKEN", "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as r:   # noqa: S310 — owner-supplied relay
            status = getattr(r, "status", 200)
            body = (r.read().decode() or "")[:160]
        if status // 100 == 2:
            return (True, f"same_chat_wake:{status}", None)
        return (False, None, f"relay rejected: {status} {body}")
    except Exception as e:  # noqa: BLE001
        return (False, None, f"same_chat relay failed: {e}")


_ADAPTERS = {"same_chat_wake": _send_same_chat, "owner_push": _send_owner_push}


def _message_for(notif_id: int, conn=None) -> str:
    """Factual message text for a notification — the correlated event's action_taken/summary."""
    n = api.get_notification(notif_id, conn=conn)
    if not n:
        return "(owner-os event)"
    conn2, own = api._c(conn)
    try:
        r = conn2.execute("SELECT action_taken,type,agent_id FROM event WHERE id=?",
                          (n.get("event_id") or 0,)).fetchone()
    finally:
        if own:
            conn2.close()
    if r and (r[0] or r[1]):
        return r[0] or f"{r[2] or ''}: {r[1]}"
    return "(owner-os event)"


def _mark_channel_failed(tier: str, error: str, conn=None) -> None:
    """Record a PROVEN delivery FAILURE. A channel whose sends are rejected is not healthy,
    whatever its configuration says.

    Live 2026-08-06: owner_push reported enabled=1, healthy=1, status=green while every send
    returned `Bad Request: chat not found`. Health was derived purely from credentials being
    PRESENT, so a channel that had never delivered anything looked identical to a working one
    — the same false-green that makes a monitoring system worse than none.
    """
    try:
        api.upsert_channel(tier, enabled=True, state="unhealthy",
                           last_error=str(error or "send failed")[:200], conn=conn)
    except Exception:  # noqa: BLE001 — never let bookkeeping break a delivery attempt
        pass


def _mark_channel_ok(tier: str, receipt: str = "", conn=None) -> None:
    """Record a PROVEN delivery on a channel — the ONLY thing that turns a channel green.
    Stamps last_ok_at + the receipt as evidence and the current runtime epoch, so `verified`
    (and, for same_chat_wake, `same_chat_wake_complete`) flips true only after a real
    receipt, and the green does not survive into the next process without a fresh proof."""
    api.upsert_channel(tier, enabled=True, state="healthy", proof=str(receipt or "")[:200],
                       last_error="", conn=conn)   # healthy → last_ok_at=now


def deliver(notif_id: int, *, severity: str = "info", adapters=None, conn=None) -> dict:
    """Attempt to deliver a queued notification across the tier matrix, best proactive first.
    Marks the notification `sent` with a REAL receipt only when an adapter returns a proven
    2xx receipt; otherwise FAILED (visible, retryable) and it stays in the durable CTO inbox.
    Never a silent or fabricated success. `adapters` is injectable for tests."""
    # build at call-time so the module-level adapter funcs can be monkeypatched in tests
    adapters = adapters or {"same_chat_wake": _send_same_chat, "owner_push": _send_owner_push}
    message = _message_for(notif_id, conn=conn)
    attempts = []
    # adapters self-gate on config (no creds/URL → they return unavailable with NO network
    # call), so a first send can actually be attempted and prove the channel.
    for tier in ("same_chat_wake", "owner_push"):
        ok, receipt, err = adapters[tier](message)
        if ok and receipt:
            api.mark_notification(notif_id, "sent", receipt=receipt, conn=conn)
            _mark_channel_ok(tier, receipt=receipt, conn=conn)
            attempts.append({"tier": tier, "result": "sent", "receipt": receipt})
            return {"delivered": True, "tier": tier, "receipt": receipt, "attempts": attempts}
        # A rejected send is EVIDENCE the channel does not work. Configuration presence is
        # not health; only a delivery proves a channel, and only a failure disproves it.
        if err and "credentials unset" not in str(err) and "no inbound trigger" not in str(err):
            _mark_channel_failed(tier, err, conn=conn)
        attempts.append({"tier": tier, "result": ("error" if err else "unavailable"),
                         "detail": err})
    # nothing proactive delivered → visible failure + remains in the durable inbox (pull)
    api.mark_notification(notif_id, "failed", conn=conn)
    attempts.append({"tier": "cto_inbox", "result": "queued_pull_only",
                     "detail": "no proactive channel delivered; event stays in durable CTO inbox"})
    return {"delivered": False, "tier": None, "attempts": attempts,
            "blocker": "no proactive notification channel delivered"}
