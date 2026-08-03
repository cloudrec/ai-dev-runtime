# SAME-CHAT PINNER — STATUS

**As of 2026-08-03. Read-only investigation + emit-only producer. No pane actuation, no agent
create/resume/stop, no external/destructive action, no flag/scope change. cp-canary-only
actuation scope preserved.**

Owner priority: significant agent events (payment + arbitrage2) must arrive **proactively in
this exact ChatGPT conversation** — not only by email, daily brief, or after the owner asks.
This report states what is **proven**, what is **still impossible/unconfigured**, and the
**exact blocker**.

---

## TL;DR

- **Producer built + proven (E2E, deterministic).** A single entry point
  `event_pipeline.publish_significant_event()` turns each significant transition
  (`completed` / `waiting_owner` / `failure` / `dead` / `blocker`) into the full contract:
  correlated CTO event id, agent, project, concise factual summary, delivery attempt with one
  retry, receipt evidence **only on a proven proactive send**, dedupe, and a persistent CTO
  inbox + legacy `commander_events` record. **False-idle invariant enforced:** a live
  shell/tool run is never reported idle/completed.
- **Durable floor works and is live.** The legacy `commander_events` → seo-backend
  `agent_notifier` path is draining and ack'ing (live: **499 events, 0 unacked**), and already
  carries real payment + arbitrage2 events (`agent_waiting_input`, `agent_completed`,
  `agent_unexpected_idle`). Ack = a real delivery receipt to an owner surface.
- **The proactive same-chat turn is NOT possible right now.** No platform inbound trigger is
  configured, so the server cannot create a new assistant turn in *this* conversation on its
  own. This is reported RED and is **not** claimed as working.

**Exact blocker (owner-gated, unchanged):**
- **G5** — `CONTROL_PLANE_SAMECHAT_WAKE_URL` is **unset**: no supported inbound trigger /
  relay/webhook that can post a new turn into this ChatGPT conversation → `same_chat_wake`
  **unavailable**.
- **G4** — `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` **unset**: `owner_push` **unavailable**
  (secret-bearing; cannot be configured without the owner providing credentials).

With neither proactive channel configured, `delivery.notifications_status()` is **RED**
(`notifications_enabled=false`) — surfaced honestly, never as "working delivery".

---

## What is PROVEN (tests + live evidence)

| Claim | Evidence |
|---|---|
| Each significant class carries the full contract | `tests/test_event_pipeline.py` — completed/waiting_owner/failure/dead/blocker, each asserts event_id, agent, project, factual summary, notification, inbox record, commander mirror |
| Correlated durable CTO inbox record | event appears in `cto_brief_since` with correlated_event_id; never lost across restart (cursor-based) |
| Delivery attempt + **retry once** | `test_retry_once_on_delivery_failure_then_success` — deliver fails then succeeds, `retried=True`, 2 attempts recorded |
| Receipt only on a **proven** send; else honest floor | `test_no_proactive_channel_is_honest_floor_never_fabricated` — `delivered=False, receipt=None, delivery_floor=cto_inbox, blocker set`. Success is NEVER inferred from queued/email |
| Dedupe (both stores) | `test_dedupe_collapses_repeat_within_window` — same CTO event id reused, commander log deduped, 1 row |
| **False-idle: live shell/tool run never idle** | `test_false_idle_*` — spinner marker, `working`, and `shell_running` all suppress `completed`; a `false_idle_corrected` event is recorded and NO completion is emitted or mirrored |
| Legacy owner-surface path is live + healthy | live `commander_delivery_report`: total 499, unacked 0, `drain_alive=True` (green) |
| payment + arbitrage2 events already flow to that surface | live `commander_events` rows for both agents in the last 24h, all ack'd |

Full suite: **987 passed, 0 failed.**

## What is STILL IMPOSSIBLE / UNCONFIGURED

| Capability | State | Why |
|---|---|---|
| `same_chat_wake` — new assistant turn in THIS chat | **unavailable / unverified** | no inbound trigger URL (**G5**). No `verified` E2E turn exists, so `same_chat_wake_complete=false` |
| `owner_push` — Telegram/out-of-band push | **unavailable** | no bot token / chat id (**G4**) — secret, owner must provide |
| `scheduled_chatgpt` — hourly automation | present but **not the pinger** | hourly latency; owner explicitly rejects "daily brief / after the owner asks" |
| `cto_inbox` — durable pull | **available** (floor only) | consumed on the next CTO invocation; not an unsolicited turn |

Live `notifications_status`: **RED**, `notifications_enabled=false`, `same_chat_wake_complete=false`.

---

## Safe staged cutover proposal (owner-gated — NOT executed)

Actuation scope stays `cp-canary:0.0` only until each gate below is explicitly approved. The
producer is emit-only, so stages 1–2 add no risk beyond writing internal event rows.

1. **Stage 0 (done, this change).** Producer + false-idle guard + full contract, proven by
   deterministic tests. No live wiring for payment/arbitrage2 (no synthetic events injected
   into the owner's real chat).
2. **Stage 1 — shadow, canary.** Wire `publish_significant_event` into the control loop for
   `cp-canary:0.0` only; observe CTO inbox + commander mirror for one bounded period. Reversible
   (single flag). No proactive channel needed — validates classification + dedupe live.
3. **Stage 2 — configure ONE proactive channel (unblocks G4 or G5).** Owner provides EITHER
   an inbound same-chat trigger URL (`CONTROL_PLANE_SAMECHAT_WAKE_URL`, unblocks G5 — the true
   pinger) OR Telegram credentials (unblocks G4 — out-of-band push). Then run a **live
   acceptance**: a real event must produce a **new turn with a captured receipt** before
   `same_chat_wake_complete` is allowed to flip true. Until a receipt exists, still RED.
4. **Stage 3 — enable for payment + arbitrage2.** Only after Stage 2 acceptance passes, turn on
   live emission for those two agents (still observe-only; no pane actuation). Verify ack via
   `commander_delivery_report` and receipts via the notification outbox.
5. **Stage 4 — broaden.** Only on explicit owner approval; never automatic.

**Do not proceed past Stage 1 without owner action on G4/G5.** No credentials, payments,
trading, mainnet, external network, destructive, or actuation-broadening steps are taken here.

---

## Files

- `core/control_plane/event_pipeline.py` — the producer (new).
- `core/control_plane/api.py::get_notification` — read helper for receipt evidence (new).
- `tests/test_event_pipeline.py` — 13 E2E/contract/false-idle/dedupe/retry tests (new).
- Delivery matrix + honesty: `core/control_plane/delivery.py` (existing, unchanged).
- Legacy live surface: `core/agent_control.py::record_commander_event` +
  `/opt/seo/backend/services/agent_notifier.py` (existing, unchanged).
