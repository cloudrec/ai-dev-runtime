# Task 211 closed-loop ChatGPT wake (P0, gate GREEN) + task 209 router wiring

Date: 2026-08-15 (night). Branch `ai-runtime/182-retry-fix-wake-continuation-star`,
head `965fbbf`. Final full suite: **2121 passed**. All commits local; no push,
no prod deploy, no DNS/payments/secrets/external actions. Dirty owner files
byte-identical throughout.

Per the owner's token policy, both tasks were implemented by Sonnet background
agents from compact context packs; Fable did design, gate verdicts, and
compact senior review only (two review findings, both fixed).

## Task 211 — closed-loop wake, acceptance gate GREEN

Commits `e6c79a9`, `21e788a`, `7aad2b1`, `13bff6c` (Sonnet-implemented).

What changed:
- **Semantic wake text**: the companion now submits
  `[Owner OS wake] event=<id> trigger=<class> type=<type> project=<key>
  agent=<ref>. <original fixed instruction>` — every field an int, a closed
  lookup (five trigger classes: completion/blocker/owner_decision/failure/
  loop_watchdog), or strictly sanitized. Nothing pane- or ChatGPT-derived is
  ever interpolated; the injection defense of the old fixed phrase is intact.
- **OWNER_DECISION_WAIT**: an internal wait naming an owner power with no
  dialog on screen used to be silently dropped (OWNER_WAIT / not-doctor-
  domain); it now escalates as `owner_decision_required` after its SLO.
- **agent_crash_loop**: repeated crash/recover cycles emit the distinct
  repeated-failure trigger.
- **SLO watchdog + owner_intervention** (`core/closed_loop_wake.py`, in the
  companion loop): a delivered wake with no progress inside the SLO re-wakes
  once then escalates; a manual owner prod while a condition was pending is
  counted as an `owner_intervention` incident (conservative heuristic; one
  live false positive disclosed, event 5552). Counters surfaced in
  observability_summary.
- **Stale-queue fix** (the biggest live find): pre-guard pytest debris and a
  chronic backlog were being delivered HOURS late into real chats (event 4881
  reached the owner ~10h after emission). `wake_bridge.expire_stale` now
  retires decided-but-undelivered wakes past `WAKE_BRIDGE_MAX_AGE_SECS` (3h)
  or carrying the audited invalid overlay, before delivery — audited in
  `wake_expire_audit`, events stay readable in the inbox. Review note: the
  age ceiling also expires genuine decision wakes if delivery is down >3h;
  accepted because expiry is audited and the inbox persists.
- cdp_composer can open a ChatGPT tab when none exists instead of failing the
  send.

Live proof (production DB, companion restarted, nobody typing):
- Autonomous wakes with the new template delivered and verified
  (`submitted_and_user_turn_appeared/_id_advanced`): events 5518, 5521, 5522,
  5529, 5534, 5537, 5544, 5545, 5548, 5551 — 5548 end-to-end through the new
  agent_watch pipeline.
- **Distinct-route proof**: genuine permission dialog on
  payorch-sonnet-fixes:0.0 → event 5566 (`agent_prompt_needs_response`,
  project payment-orchestrator) → journal
  `delivered wake for event 5566 [route payment-orchestrator] to
  https://chatgpt.com/c/6a7f1005-… : submitted_and_user_turn_appeared` —
  the bound project chat, not the owner-os fallback. Disclosed caveat: the
  bridge-consultation step for 5566 was replayed manually with the event's
  own recorded fields after a diagnostic script ran without
  WAKE_BRIDGE_ENABLED; classification, emission, routing, delivery,
  verification and dedupe all went through the real pipeline, and the
  owner-os batch above was fully autonomous end-to-end.
- Dedupe: spot-checked events each have exactly one delivery row; the
  `wake_submitted` latch prevents re-offers.
- Event 5538 trace: skipped during the agent's ~16-min emergency
  WAKE_BRIDGE_KILL_SWITCH window (its own stale-bleed mitigation, since
  removed); the pane condition self-resolved — legitimate skip, not a bug.
- Synthetic events adjudicated during the run: 4881 + 9 siblings (pre-guard
  pytest debris) and 5576/5584 (router smoke jobs) — all retired via audited
  mark_invalid + acknowledge; none counted as proof.

## Task 209 — runtime dispatch wired through the router

Commits `1afcf40` (Sonnet-implemented), `965fbbf` (review fix).

- Every planner call (plan + each repair attempt) asks `model_router.route()`:
  kind→class mapping (unknown kinds fail toward sonnet), risk floor from
  risk_level + conservative money/security goal scan, lineage of prior failed
  jobs feeding the escalation ladder; a sonnet plan whose tests fail escalates
  its repair toward opus. Decisions land as `model_selection` job artifacts +
  log lines + `router_decision` rows; outcomes (with planner token accounting
  where observable) land as `router_outcome` rows. Router failure can never
  block a job (falls back to the configured default); `RUNTIME_MODEL_ROUTER=0`
  disables.
- Live smoke: two real jobs through the API. Job b34772f4 proved decision
  (sonnet/routine_implementation) + failure outcome with token accounting
  (2 in / 82 out) — and exposed a lost-update bug: the fallback-planning
  metadata append used a stale job dict and clobbered the model_selection
  artifact. Fixed (`965fbbf`) with a live-shaped regression; job 58ce9d16
  then proved both artifacts surviving together.
- Observation for later: both trivial smoke goals made the planner return
  prose without a `files` list → deterministic fallback (truthful
  `fallback_plan_only`). Pre-existing planner behavior, not a router defect.

## State

Services active: ai-runtime, owner-os-wake-companion, owner-os-chromium.
Session commits tonight: `41c9788, cf0a7e2, 820cb55, e698604, d0b46d3,
e6c79a9, 21e788a, 7aad2b1, 13bff6c, 1afcf40, 965fbbf`. Owner OS 2.0 roadmap
may continue (193/202 research iterations, 200/203) per the standing model
partition.

## Post-gate follow-up: SLO watchdog resolution blindness + self-feed (b4b0433)

Live operation surfaced two watchdog defects within an hour of the gate:
watches never checked whether their condition had RESOLVED (runtimejob targets
are terminal-blind and pane-less, so every runtime-job wake became a
guaranteed false positive), and loop-watchdog/escalation events registered
themselves as new watches (second-generation rewakes observed live:
5597→5612, 5599→5614). Six artifact events total (5597, 5599, 5611, 5612,
5613, 5614), each retired at sight with audited notes — no retries, no
duplicate dispatch, jobs verified terminal every time.

Fix (Sonnet, senior-reviewed): register_delivery refuses loop_watchdog-class
events; deregister_resolved() silently retires watches whose original event
carries the invalid overlay, whose runtimejob is terminal, or whose pane is
observed working — run at the top of every scan and used for the one-time
cleanup (12 stale rows deregistered via the code path on first tick, no raw
SQL). 8 new regressions; full suite 2129 green; companion restarted
23:24 CEST; three-plus ticks journal-quiet on all retired chains, verified
independently. Head `b4b0433`.

## Follow-up 2: event-age ceiling (52f47bf)

Event 4619 (emitted Aug 14, skipped for cooldown at emission) was re-decided
to `wake` ~24h later by `_redecide_cooldown_skips` — whose recency window
keyed off the SKIP's timestamp — and the fresh decision made a day-old event
invisible to expire_stale's decision-age clock: delivered ~24h late. Fix:
staleness now checks two independent clocks (decision age AND the event's own
ts_epoch from the durable log — a replayed decision cannot make the event
younger), the redecider is bounded by event age at the source, and a second
expire pass inside pending_wake backstops any future re-decision path.
5 regressions incl. the exact 4619 shape + fresh positive controls; full
suite 2133 green; companion restarted 00:00Z, journal quiet. Telegram
dead-letters (786) confirmed as the pre-existing known-red push channel —
owner decision item, not a delivery defect.

## Reconciliations 2026-08-16 (read-only)

- 5607 (notifications_red, critical): hourly re-announcement of the standing
  push-channel red (series 5485/5540/5607) — the known Telegram owner-decision
  item; no new condition. 5543/5557 (notification_dead_letter): both
  channel=telegram after 5 attempts, for payorch conditions whose ChatGPT
  wakes DID deliver — Telegram leg only, not a wake defect. No delivery
  config or external channel touched.
- Diagnostic staleness noted, deliberately unfixed: red_reason
  `actuation_scope_breach` flags two soak-era panes (arbitrage2-opus,
  mess-qa-automation, last cp_action Aug 5/7, panes gone) — the all-time
  ledger check vs the current canary allowlist can never self-clear; a
  time-window or allowlist-era fix is a future deliberate change to a safety
  check, not a bug fix to rush.
