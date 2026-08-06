# OWNER OS — NIGHT SHIFT CTO: CANONICAL ARCHITECTURE + PHASED PLAN

Authoritative sources this builds on (not a new design):
`reports/OWNER_OS_CONTROL_PLANE_V2_ARCHITECTURE.md` (R2 CTO inbox, R3 delivery matrix),
`reports/SAME_CHAT_PINNER_STATUS.md`, `core/control_plane/cto.py`, `core/os_task_queue.py`,
`core/continuation_governor.py`, `core/commander_autopilot.py`.

Status as of 2026-08-06: **phases 2, 3 and 5 are BUILT, TESTED AND DEPLOYED.** Phases 1, 4,
6 and the 24h acceptance are each blocked on an owner decision, not on engineering.

| phase | state | commit |
|---|---|---|
| 2 — event bus + executive skeleton | deployed; draining signals on the live tick | `6b9b491`, `f1b7bb1` |
| 3 — observation breadth | deployed; services + sustained resource pressure | `b5c231c` |
| 5 — tier policy + budgets | deployed; tier 0 free, no silent escalation | `0e10cdc` |
| 1 — MCP CTO inbox exposure | blocked: `/opt/seo` container rebuild + `RUNTIME_TOKEN` | — |
| 4 — portfolio brain | blocked: permitted autonomous-work set | — |
| 6 — wake bridge | server half unblocked; browser leg needs one-time ChatGPT login | — |
| 7 — 24h acceptance | needs 1, 4, 6 | — |

Suite at the last deploy: **1545 passed, 0 failed**.

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
| model routing / dispatcher | **EXISTS — reuse, do not rebuild** | `/opt/seo/backend/services/dispatcher.py` + `routes_dispatcher.py` + migration `0011_ai_dispatcher` + tests. Cost-per-1k registry across premium/balanced/cheap/local tiers, `score_model` with risk fit, `estimate_cost`. My earlier "absent" claim was wrong — I had searched only `ai-dev-runtime/core/`. |
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

### Phase 5 — Model routing + budgets (MANDATORY; integrate the existing dispatcher)

Reuse `/opt/seo/backend/services/dispatcher.py` — it already carries a per-model cost
registry, tier metadata (premium/balanced/cheap/local), risk-fit scoring and `estimate_cost`.
Night Shift adds the routing POLICY on top, not a second router.

| tier | use | rule |
|---|---|---|
| 0 | polling, state reduction, dedupe, tests, routine checks | **deterministic code, no model at all** |
| 1 | classification, extraction, summaries, broad idea screening | local / free / lowest cost |
| 2 | ordinary research, specs, coding | inexpensive capable |
| 3 | complex implementation / review | Sonnet-class |
| 4 | ambiguous architecture, high-impact reasoning, final critical audit | Opus-class only |

Required behaviour: **automatic downgrade** whenever a lower tier can complete the task
safely; **escalation only with a recorded reason** on the task row; per-task token and cost
ceilings; caching, batching and context compaction; loop detection; a daily budget with a
kill switch. **Never spend tokens to prove liveness** — tier 0 already answers "is it alive".
The 24h report breaks cost down by project and model and reports useful artefacts per dollar.

### Phase 6 — Wake bridge (SERVER-HOSTED Linux companion)

Owner correction 2026-08-06: the bridge must not depend on the owner's PC being awake. It
runs on the server as a dedicated Linux browser companion with a persistent isolated profile
and a ChatGPT session the owner authenticates ONCE by hand.

* **Wake-only.** On a deduped urgent/actionable CTO event it opens the configured existing
  conversation and submits exactly one fixed phrase:
  `Проверь новые события Owner OS через MCP и продолжи разрешённую работу.`
* **Never**: scrape or parse assistant output, inject arbitrary event text, store plaintext
  credentials, or bypass login / 2FA / CAPTCHA / rate limits.
* **Never the control plane.** Owner OS + MCP stay the source of truth; the phrase carries no
  payload, so refreshing ChatGPT on any other device shows the same conversation history.
* One-time manual login / re-auth through noVNC or equivalent, with restricted profile
  permissions.
* Controls: explicit enable/disable, cooldown, event correlation, **acknowledgement stops
  further wakes**, health check, emergency kill switch, audit row per wake.
* **Experimental and reversible.** Consumer ChatGPT UI automation is unsupported, so this
  bridge must never be required for core autonomy — if it is disabled or broken, the CTO
  inbox and Telegram surfaces carry on unchanged.

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

## 4b. Delivery surfaces — live state 2026-08-06

### Same-chat wake bridge — LIVE and end-to-end tested

| | |
|---|---|
| server companion | Xvfb :99 + openbox + Google Chrome (.deb) + x11vnc + noVNC |
| access | `127.0.0.1` only, reached by SSH tunnel; nothing publicly bound |
| browser profile | `/home/owneros/companion-profile`, mode 700, dedicated `owneros` user |
| active chat | rotatable pointer, `owner-os-chat bind <url>`, audited, fail-closed |
| bridge | `WAKE_BRIDGE_ENABLED=1`, cooldown 900s, kill switch available |
| end-to-end test | event 3136 → decided 14:31:28Z → submitted 14:31:48Z → acknowledged; **exactly one** wake row, no duplicate over three further polls |

Two failures worth recording, because both were reported as working before they were:

* **Black screen.** Chrome was crash-looping (233 restarts, `Running as root without
  --no-sandbox is not supported`), and I called it "running" from a transient PID caught
  between restarts. A live PID is not evidence of a mapped window. Fixed with a dedicated
  unprivileged user rather than `--no-sandbox`, which would have run a browser holding an
  authenticated session as root with its sandbox off. Snap Chromium then failed a second way
  (`not a snap cgroup` — snaps need a logind session a system service lacks) and Ubuntu 24.04
  ships no native Chromium, hence the .deb. Proven by framebuffer: 4849 distinct colours.
* **"Enable the already-implemented bridge"** turned out to be only the decision half; nothing
  submitted anything. The companion daemon had to be written before "enabled" meant anything.

### Telegram owner_push — configured, BLOCKED on one owner action

Credentials installed in `configs/.env` (mode 600, git-ignored), service restarted, bot
identity verified (`ezzetasecurity_bot`, getMe OK). Sending fails with
`Bad Request: chat not found`: a bot cannot open a conversation with a user who has never
messaged it. **The owner must press Start on the bot once.** Until then this tier is dead and
the CTO inbox plus the same-chat wake carry everything.

`getUpdates` returns 409 Conflict — the bot is already long-polled by another service, i.e.
this is an existing product bot, not a dedicated Owner OS one. Worth separating.

A related defect was found and fixed while configuring it: `owner_push` reported
`healthy=1, status=green` while every send failed, because health was derived from
credentials being PRESENT rather than from delivery succeeding. A rejected send now marks the
channel unhealthy; abstention (no credentials) still does not.

## 5. Acceptance (as mandated)

**24 continuous hours** unattended (owner correction 2026-08-06), ≥2 managed project agents, no manual Enter or pings;
safe continuation works; one bounded promotion/revenue experiment researched and specified;
one new opportunity evaluated; no duplicate agents/tasks/notifications; budgets respected;
the companion wakes the configured chat exactly once for a canary event and stops after
acknowledgement; restart survives; full evidence report.

Also required in the 24h evidence: survives BOTH a service restart and a browser-companion
restart; the companion wakes the configured chat exactly once for a canary event and stops
after acknowledgement; cost broken down by project and model with useful artefacts per dollar.

**The one owner-side step that remains** is the initial ChatGPT authentication in the
companion profile (login + 2FA through noVNC). It is one-time and interactive by nature — I
will not store or handle ChatGPT credentials. Everything after that, including the 24h run,
is server-side and unattended.
