# OWNER OS — NIGHT SHIFT CTO: CANONICAL ARCHITECTURE + PHASED PLAN

Authoritative sources this builds on (not a new design):
`reports/OWNER_OS_CONTROL_PLANE_V2_ARCHITECTURE.md` (R2 CTO inbox, R3 delivery matrix),
`reports/SAME_CHAT_PINNER_STATUS.md`, `core/control_plane/cto.py`, `core/os_task_queue.py`,
`core/continuation_governor.py`, `core/commander_autopilot.py`.

Status: **PLAN — nothing in phases 1-7 implemented yet.** Written 2026-08-06.

---

## 1. What already exists (verified, deployed)

Worth stating precisely, because the mandate's cadence requirement is already met and the
real gaps are elsewhere.

| capability | state | evidence |
|---|---|---|
| bounded fallback tick | **already 30-120s, never hourly** | autopilot 60s, watchdog 30s, supervisor 45s, context budget 120s |
| deterministic task ledger | done | `os_task` states, transcript ack, retry-once, restart recovery — VERIFIED PASS `acb3257` |
| grounded queue advancement | done | governor advances real project stages, exactly once |
| durable CTO inbox + cursor | done, **unexposed to the assistant** | `cto_brief_since` / `ack_through` / `cto_cursor`, monotonic, restart-safe |
| project-role isolation | done | clause-local, preservation-aware |
| owner gates split from diagnostics | done | genuine decisions separated from classification gaps |
| Telegram push | **RED — credentials unset** | `owner_push enabled=0`, 3 notifications `dead_letter` |
| same-chat wake | **unavailable** | no inbound trigger; `same_chat_wake_complete=false` |
| model routing / dispatcher | **absent** | no dispatcher or OpenRouter module in `core/` |
| portfolio / opportunity ledger | **absent** | — |

So the loop exists and is fast. What is missing is the **executive layer** above it
(diagnose → prioritize → act → verify → record), the **portfolio brain**, **model routing**,
and the **wake bridge**.

---

## 2. Target architecture

```
          events (immediate)                    bounded tick 60s (fallback, exists)
                 │                                        │
                 ▼                                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  NIGHT SHIFT EXECUTIVE  (new: core/night_shift.py)       │
        │  observe → diagnose → prioritize → act → verify → record │
        └───────┬───────────────────────┬──────────────────┬───────┘
                │                       │                  │
     existing actuation           portfolio brain     owner surfaces
     os_task ledger  ────────────  goals / backlog     CTO inbox (MCP)
     continuation governor          experiments        Telegram (urgent)
     session recovery               opportunities      morning brief
                                                       wake bridge (companion)
```

**Invariant carried forward from the ledger work:** every action the executive takes is a
ROW first (`os_task` or a queue stage), never text inferred from a pane. The executive
proposes; the ledger delivers; the transcript acknowledges. Screen reading stays diagnostic.

**Event-driven immediacy** is added by a signal table + condition variable: emitters
(`cto.emit`, task state changes, health probes) poke the executive, which wakes at once
instead of waiting for its next tick. The tick remains as the fallback floor so a missed
signal can never cause a stall — the same fail-closed shape as the ledger's ack timeout.

---

## 3. Phases (each reversible, backed up, tested, independently deployable)

### Phase 1 — CTO inbox exposed to the assistant *(prerequisite; blocked, see §4)*
Add read-only MCP tools proxying the control plane: `cto_brief_since` (never auto-acks),
`cto_ack` (explicit, monotonic), `cto_delivery_health` (posture + scheduled-wake freshness +
last consumer cursor/ack), `cto_open_gates`, `cto_event_evidence`. `notifications` stays a
summary. Connector tests: appears once, unread until ack, survives restart, resolves, never
reappears after ack.

### Phase 2 — Event bus + executive skeleton
`core/night_shift.py`: signal table, immediate wake, 60s fallback floor, one executive pass
(observe → diagnose → prioritize → act → verify → record). Actions restricted to what the
ledger already permits. Concurrency cap, and a make-work brake: no new task may be created
for a target that already has an active one, and a repeated identical proposal within a
window is suppressed.

### Phase 3 — Observation breadth
Extend observation to Git, tests, services/containers, and sustained resource pressure
(thresholds over a rolling window, never a single spike). Each observation becomes a
deduplicated event with severity, evidence and a recommendation. Paused and policy-excluded
projects are reported in their own section, never as blockers.

### Phase 4 — Portfolio brain (durable, no long chat context)
Tables: `project_goal`, `promotion_backlog`, `experiment_backlog`, `opportunity_ledger`.
Scoring: expected revenue × confidence ÷ (effort × risk), with a reuse bonus for existing
assets. Idle capacity only — never pre-empts managed project work.

### Phase 5 — Model routing + budgets
Route by task class (cheap classification vs deep design) with hard token/cost ceilings and
a durable spend ledger. A budget breach stops dispatch and raises a genuine owner decision.

### Phase 6 — Wake bridge (owner-side companion)
Server: authenticated wake endpoint (SSE/WebSocket/short-poll), event dedupe, minimum
cooldown, enable/disable + kill switch, and an audit row per wake. Companion (owner's
Windows/Chrome): opens the exact configured conversation and submits ONE fixed phrase —
`Check the Owner OS CTO inbox and continue approved work.` It never reads or extracts
ChatGPT output, stores no ChatGPT credentials or cookies on the server, and bypasses no
CAPTCHA, rate limit or safety control. Interim bridge because Scheduled Tasks have a 1h floor.

### Phase 7 — Morning brief + 8h overnight canary
Brief: work completed, decisions, cost, blockers, new opportunities. Then the acceptance run
in §5.

---

## 4. Genuine owner decisions (blocking; nothing else needs you)

1. **Phase 1 deploy touches a product service.** The installed MCP server is
   `/opt/seo/backend/services/mcp_server.py`; the SEO backend image is baked, so shipping
   read-only tools means rebuilding the image and restarting the `seo-backend-1` production
   container. The mandate says not to touch product projects. The code change is strictly
   additive and read-only, but the deploy is a product action — I will not do it unasked.
2. **`RUNTIME_TOKEN` inside that container.** The proxy calls need it; it is not present.
   Moving a credential between services needs your approval.
3. **Telegram credentials.** `owner_push` is RED and today's three notifications
   dead-lettered. Phase 5's urgent surface cannot work until `TELEGRAM_BOT_TOKEN` /
   `TELEGRAM_CHAT_ID` exist.
4. **Autonomous-work policy boundary.** Phases 2 and 4 let the executive create and dispatch
   work. The mandate's prohibitions (no publish, outreach, production deploy, spend,
   payments/credentials, live trading) will be encoded as a deny-by-default policy, but the
   *allowed* set for a real project should be confirmed once rather than inferred per task.

---

## 5. Acceptance (as mandated)

8-hour unattended overnight canary, ≥2 managed project agents, no manual Enter or pings;
safe continuation works; one bounded promotion/revenue experiment researched and specified;
one new opportunity evaluated; no duplicate agents/tasks/notifications; budgets respected;
the companion wakes the configured chat exactly once for a canary event and stops after
acknowledgement; restart survives; full evidence report.

**Honest limit to state now:** the companion runs on the owner's Windows machine. I can build
and test the server half (wake endpoint, dedupe, cooldown, kill switch, audit) and ship the
companion with its own tests, but I cannot install or run it on your machine — that leg of
acceptance needs you. Everything else is demonstrable server-side.
