# OWNER OS — PRODUCTION ACCEPTANCE (Stage 1 pinger deploy)

**2026-08-03. Owner-authorized deploy of the completed Stage 1 pinger.** Scope strictly
`cp-canary:0.0`. Only `ai-runtime.service` restarted. All prior safety gates preserved.

Deployed code: `941b76f` (delivery adapters) on `3346444` (shadow wiring) on `1db15a4`
(producer). Branch `owner-os/control-plane-v2` (local, not pushed).

---

## Rollback

**Backup (pre-deploy):** `/root/owner-os-backups/20260803T153940Z/` — git HEAD, unit + drop-in,
pre-restart service status, `control_plane.db.bak`, `agent_control.db.bak`, baselines.

**Primary rollback (disable the pinger, keep everything else) — one command:**
```
git -C /root/ai-dev-runtime checkout 1db15a4 -- core/control_plane/engine.py && systemctl restart ai-runtime.service
```
(`1db15a4`'s `engine.py` has no pinger wiring — verified. Observe-only, so no state cleanup needed.)

**Full restore (code + durable DBs to the pre-deploy snapshot) — only if required:**
```
git -C /root/ai-dev-runtime checkout 3346444 -- core/control_plane/ api/ core/agent_control.py
cp /root/owner-os-backups/20260803T153940Z/control_plane.db.bak /root/ai-dev-runtime/control_plane.db
cp /root/owner-os-backups/20260803T153940Z/agent_control.db.bak /root/ai-dev-runtime/agent_control.db
systemctl restart ai-runtime.service
```
(DB restore discards events recorded after the snapshot — use only for a genuine data problem.)

---

## Acceptance — PASS / FAIL per requirement

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Deploy Stage 1 pinger code | **PASS** | service restarted (PID 2163517→2879516→2976504), new code loaded |
| 2 | Timestamped rollback backup + record unit/drop-in state | **PASS** | `/root/owner-os-backups/20260803T153940Z/` (git HEAD, unit+dropin, DB snapshots, baselines) |
| 3 | Restart only the required service | **PASS** | only `ai-runtime.service` restarted; no other project service touched |
| 4 | Scope strictly cp-canary | **PASS** | `CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0`; scope-confinement test live — out-of-scope agents counted, never emitted; only cp-canary produced events |
| 5 | Preserve all safety gates | **PASS** | actuator still cp-canary-only; owner_push + same_chat_wake OFF; no cutover/broadening; `notifications_status` honestly RED |
| 6a | Service healthy | **PASS** | `is-active=active`; 5/5 control loops alive; `restart_safe=True`, `consistent=True` |
| 6b | Discovery/continuation unaffected | **PASS** | continuation_watchdog/orchestrator/direct_agent_lifecycle/engine/supervisor all `alive`, 0 stalled |
| 6c | No duplicate agents / actions / events | **PASS** | CP agents 10→10, `cp_action` 3→3 (pinger creates none), exactly 1 pinger event; the single duplicate flag (`cp-canary-dup`, dead) predates the deploy (06:14) |
| 6d | False-idle suppressed during REAL shell/tool activity | **PASS** | live: drove cp-canary into a real shell run → `state=working`, `has_active_marker=True`; a `completed` publish against that live evidence was **suppressed** (`reason=false_idle_suppressed`) |
| 6e | One controlled canary transition → exactly ONE durable event + CTO inbox record | **PASS** | drove cp-canary→`waiting_owner`; live shadow loop emitted exactly CP event `id=67` (`waiting_owner`, high, owner_action, dedup `sig:cp-canary:0.0:waiting_owner`) + 1 CTO inbox record + 1 commander mirror (`id=505`, `corr_event_id=67`) |
| 6f | No repeat after restart | **PASS** | 2nd `ai-runtime` restart + loop tick: cp-canary events stayed 6, mirror stayed 1 — `pinger_shadow_state` persisted last_kind across restart |
| 6g | Retry / dedupe evidence intact | **PASS** | notification `id=21` shows `attempts=5` then `dead_letter`, `receipt=None` (retry + honest floor); repeat ticks did not re-emit; same event id reused on dedupe |
| 7 | No fabricated same-chat success | **PASS** (honest negative — see below) |

### 7 — Did an actual proactive message appear in THIS ChatGPT conversation? **NO.**

- `notifications_status` = **RED**, `notifications_enabled=false`, `same_chat_wake_complete=false`.
- The controlled `waiting_owner` event's push attempt **dead-lettered** (`telegram`, 5 attempts,
  `receipt=None`) because owner_push is unconfigured (G4).
- The durable **CTO inbox** record (event 67) and the legacy **commander mirror** (505) hold the
  event; the seo-backend `agent_notifier` drained + **ack'd** 505 — i.e. it was delivered to the
  *OwnerEvent surface*, **not** proven to be a new turn in *this exact conversation*.
- **No receipt for `same_chat_wake` exists**, so no same-chat proactive turn is claimed. This is
  the correct fail-closed behavior, reported honestly.

---

## Blocker fixes (priority order) — done to the external gate

| Priority | Blocker | Server-side status | External/platform GATE (stop point) |
|---|---|---|---|
| 1 | Automatic connector/control availability for new chats | **READY** — `/control-plane/cto/brief` auto-serves any new consumer from cursor 0 (zero manual registration; verified live: fresh consumer sees pending backlog). `/registry`, `/observability`, `/notifications/status` live. | ChatGPT **platform connector / GPT-action must be attached to the chat** — cannot be configured from the server. |
| 2 | G5 same-chat inbound trigger | **READY** — `delivery._send_same_chat` POSTs the event to `CONTROL_PLANE_SAMECHAT_WAKE_URL` (+ optional bearer); records a receipt only on a real 2xx; flips `same_chat_wake_complete` true only after a proven turn. Gated: no URL ⇒ no network call. | A **relay endpoint that can inject a message into this exact ChatGPT conversation**. ChatGPT exposes **no inbound API** to create an unsolicited turn — this is a genuine **platform endpoint gate**. Needs an owner-run relay/webhook URL. |
| 3 | G4 fallback delivery (owner push) | **READY** — `delivery._send_owner_push` sends via Telegram `sendMessage`, receipt only on real 2xx. Gated: no token/chat-id ⇒ no network call. | **`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` credentials** — owner-supplied secret. Not enabled here per instruction. |

**Honesty fix shipped:** `deliver()` previously marked a notification `sent` on mere channel
*availability* (a fabricated receipt). It now marks `sent` **only on a real adapter 2xx
receipt**; unconfigured channels make **no network call** and fail closed to the durable inbox.
So `delivered`/`receipt`/`same_chat_wake_complete` are now trustworthy.

---

## What is needed to make the pinger actually reach this conversation

Exactly one of (owner action — external):
1. **Same-chat relay (best):** stand up an endpoint that can post into this conversation, set
   `CONTROL_PLANE_SAMECHAT_WAKE_URL` (+ `CONTROL_PLANE_SAMECHAT_WAKE_TOKEN`). Then a live
   acceptance must capture a real receipt before `same_chat_wake_complete` is allowed true.
2. **Telegram fallback:** provide `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` for out-of-band push.
3. **Connector attach:** attach the Owner-OS connector/GPT-action to the chat so it auto-pulls
   the CTO brief each turn (near-real-time, pull-based).

Until one is provided, the system correctly reports RED and delivers to the durable CTO inbox +
commander surface only — never to this exact conversation, and never claims otherwise.

**STOP POINT:** all three remaining blockers now terminate at a genuine external credential /
endpoint / platform-config gate. No further server-side progress is possible without owner input.

---

## Test + live status

- Full suite: **1000 passed**, 0 failed.
- Live post-deploy: 5/5 loops alive, `restart_safe=True`, `consistent=True`,
  `notifications_status=RED` (honest), adapters gated (no network when unconfigured).
- Actuation scope unchanged (`cp-canary:0.0`). No agent created/resumed/stopped. No push/publish.
