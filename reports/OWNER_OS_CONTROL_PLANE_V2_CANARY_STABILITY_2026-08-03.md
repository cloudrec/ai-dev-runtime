# Owner OS Control Plane V2 — cp-canary sustained stability

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local, not pushed)
**Scope:** the single disposable canary `cp-canary:0.0` only. No expansion to other agents.
No push/publication, credential, payment, trading, security-deployment, or destructive action.

## Window

Bounded sustained monitor: **12 samples over 18 minutes** (90s cadence) of `control_plane.db`
+ `agent_control.db`, comparing every metric to baseline.

## Verdict: STABLE — every sample DELTA = none

| Metric | Baseline | All 12 samples | Criterion |
|---|---|---|---|
| `cp_action` rows (canary) | 2 | **2** (no change) | no duplicate commands ✅ |
| `cp_action` verified | 2 | **2** | no re-issue / no runaway ✅ |
| `cw_step` rows (legacy) | 2 | **2** (no growth) | no unexpected legacy writes — retirement holds ✅ |
| max canary event id | 48 | **48** (no new) | no false-idle actuation / no churn ✅ |
| false_idle/action_blocked/duplicate events | 0 | **0** | no false-idle actuation ✅ |
| lease | `rollback_probe/3` | **unchanged** | stable lease/fence ownership (no thrash) ✅ |
| notification `dead_letter` | 17 | **17** (no new) | outbox stable, fail-closed, no runaway ✅ |

Post-monitor spot check: canary `idle`; `cw_health.errors = 0`; exactly **1** live
`cp-canary` pane (no duplicate); rollback drop-in present.

## Notes / honest observations

- **Idempotent quiescence.** `proactive_continue` is false (set post-green), so the watchdog
  runs each tick, sees the canary idle, and takes NO action — `cw_health` verified/blocked/
  errors all 0. The canary's input line holds a leftover typed `continue with the next safe
  canary note`; `decide` routes it as a submit but the Actuator returns `already_verified`
  (same conversation+text as a prior verified action) → **skipped, no re-delivery**. This is
  the idempotency guard working: a persistent pending never produces a duplicate command.
- **Lease record shows `rollback_probe/3`.** That is the expired lease from the earlier
  manual rollback probe, not active ownership; the quiesced watchdog has not re-acquired. It
  is cosmetic (an expired record) — no lease churn occurred during the window. A future real
  actuation would re-acquire at a higher fence.
- **Legacy retirement holds.** `cw_step` stayed at 2 (pre-fix historical) with zero growth;
  no new `cw-ok`/`cw-block`. The 2 legacy `agent_continuation_submitted` commander events for
  the canary are **pre-fix historical** (the dual-notify that fix `f126c2e` closed); none
  since.
- **Notifications RED (expected).** 17 dead-lettered owner-push notifications are the
  fail-closed-visible result of no configured `owner_push` channel (gate G4) — stable, not a
  regression. CTO inbox retains everything; same-chat via `agent_notifier` works.

## Flags (unchanged, scoped)

Systemd drop-in `/etc/systemd/system/ai-runtime.service.d/canary.conf`:
`CONTROL_PLANE_ACTUATOR_ENABLED=1`, `CONTINUATION_VIA_ACTUATOR=1`,
`CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0`. Any other agent → `not_canary` (never actuated).

## Event IDs

Discovery #44; `action_verified` #46/#48 (the 2 verified deliveries, unchanged); duplicate
#50; false_idle_corrected #37; same-chat commander_events #443, #458 (acked). No new canary
events during the stability window.

## Test counts

Full suite (last run): **905 passed**, 0 failed. Control-Plane-V2 + watcher suites include the
routing-scope + legacy-retirement (no-cw_step) tests.

## Rollback readiness — confirmed

1. `rm /etc/systemd/system/ai-runtime.service.d/canary.conf && systemctl daemon-reload &&
   systemctl restart ai-runtime.service` → actuator/routing OFF (dormant); canary back to
   legacy/observe.
2. Remove the `cp-canary` session from `config/agent_orchestrator.yaml` (+ allowed_root).
3. `tmux kill-session -t cp-canary` (disposable).
4. `git revert` phase commits; `control_plane.db` gitignored; legacy DBs backed up.

## Failures

**None.** No duplicate commands, no false-idle actuation, no lease/fence thrash, no
unexpected `cw_step` writes, no outbox runaway, watchdog errors 0. Stable for the full window.

## Blockers (unchanged, not stability-related)

G4 owner-push channel unconfigured (notifications RED); G5 same-chat inbound trigger;
G3 push/publication; multi-agent/full cutover not authorized.

---

# ADDENDUM — extended window (45 min) + delivered completion event + state fix

## Extended read-only window: 18 samples / 45 min — STABLE

Every sample DELTA = none vs baseline. Exact deltas over the window:

| Signal | Delta | Reading |
|---|---:|---|
| duplicate continuation (`cp_action` growth) | **0** | no duplicate command |
| false-idle (`false_idle_corrected`/`action_blocked`/`duplicate` events) | **0** | no false-idle regression |
| lease churn (`resource_lease` holder/fence change) | **0** | stable ownership (record `rollback_probe/3`, expired, unchanged) |
| notifier failures — NEW (`dead_letter` growth) | **0** | no notifier runaway / no new delivery failures |
| `cw_step` growth (legacy) | **0** | legacy retirement holds (frozen at 2 historical) |
| new canary events (`max_event`) | **0** | max id **48** unchanged |
| live `cp-canary` panes | **1** | no duplicate agent |
| `notification` total | 19 (17 dead_letter + 2 sent), unchanged | outbox quiescent |

Combined observed stable window (both runs): **~63 min, 30 samples, zero change.**

## Correlated completion event — DELIVERED via agent_notifier (receipt verified)

- Emitted `canary_stability_completed` (high significance) through the proven
  `agent_notifier` same-chat path: commander_event **#469**.
- **Delivery receipt: acknowledged=1** (agent_notifier acks only after delivering to the
  chat) within ~12s — same-chat message produced without a user prompt. No `delivery_failed`,
  no retry needed.
- Payload: monitor windows 18min+45min; duplicate_command_count **0**; false_idle_count
  **0**; lease_churn **0**; notifier_failures_new **0**; verified_actions **2**;
  cw_step_growth **0**; test_count **907**; blockers [multi-agent, G4, G5, G3].

## State-estimation fix (`d1d20cb`) — background-shell footer

Root cause of a false-idle on `owneros-direct-fix`: it is the pane where the Bash tool runs;
a Claude Code BACKGROUND shell (e.g. a live monitor) keeps the pane foreground command
`claude`, so the pane-command heuristic missed the running shell and, with no visible spinner
in the tail, it could read idle. `_STATE_ACTIVE_RUN_RE` now matches the footer "· N shell ·"
indicator → the agent reads active/working, never idle. Live-verified: owneros-direct-fix
footer `· 1 shell ·` → active match. Tests: shell footer active (`N shell`/`N shells`), plain
footer (`3 agents`) not active, estimator overrides idle base.

## Test count

Full suite: **907 passed**, 0 failed (was 905; +2 shell-footer tests).

## Event IDs (this addendum)

Stability completion commander_event **#469** (acked). Canary control-plane events unchanged
(#44 discovery, #46/#48 verified, #50 duplicate; max id 48). State-fix commit `d1d20cb`.

## Failures: none.

---

# ADDENDUM — observability counter reconciliation (read-only)

Two non-green internal counters investigated read-only; both are **HISTORICAL/stale, not
active failures**. New read-only diagnostics (`core/control_plane/diagnostics.py`) split each
metric into historical vs active so current health is not flagged by old failures.

## runtime job `failed` — HISTORICAL

- `runtime_jobs.db` `jobs` status=failed: **19 total** (the reported `15` was a stale/subset
  snapshot). Newest failed = **2026-07-28T09:57Z** (~6 days ago); **0** failures in the last
  24h (or 6 days). `runtime_job_failure_report` → `active=0, status=green,
  classification=historical`.
- Actionable: **none** — all stale. Failed job kinds: 16 unknown, 2 code_change, 1
  data_handoff (all pre-2026-07-29).

## notifier `dead_letter` — HISTORICAL

- `control_plane.db` notification state=dead_letter: **17 total**, all created 2026-08-03
  02:00–06:17Z; newest **06:17Z** (~2.2h before check); **0** in the last hour, and the two
  stability windows (07:22–08:07Z) showed **no growth**. `notification_failure_report` →
  `active=0, status=green, classification=historical`.
- Root: proactive owner-push channel disabled (RED, gate **G4**) → observe_only agents'
  one-time discovery owner-decision events dead-lettered. Not recurring (discovery gates are
  deduped). Actionable only by enabling an owner-push channel (owner-gated; **not** done here).

## Combined verdict (live, read-only)

`observability_summary` → `active_failures_total=0`, `historical_failures_total=36`,
**`all_clear=true`, status=green**. The 15/19 failed jobs and 17 dead-letters are stale; there
are **zero active failures**. New read-only endpoint: `GET /api/v1/control-plane/observability`.

## Tests / commits (this addendum)

- Tests: `test_control_plane_diagnostics.py` (**7**): notification/job historical→green,
  recent→active-red, none→clean, combined summary all_clear-with-historical vs red-when-active.
- Commit: `core/control_plane/diagnostics.py` + endpoint + tests (local only). No live
  behavior changed; strictly read-only (SELECT / `mode=ro`).

---

# ADDENDUM 2 — registry freshness / engine liveness + gate-aging + lease diagnostics (read-only)

## Gap found

`is_stale` keys on `agent.evidence_fresh_at`, which ONLY the actuator refreshes (on a
verified action). Discovery observes every agent each ~30s tick but refreshes
`agent.updated_at`, not `evidence_fresh_at`. So `is_stale` reads **9/9 agents stale** even
while discovery is actively seeing them — a misleading liveness signal for observe-only
agents, and no aggregate view of engine liveness, gate backlog, or expired leases existed.

## Added (read-only; no discovery behavior changed)

- `registry_health_report` — agent freshness by **observation recency (`updated_at`)**:
  total, observed_fresh vs observed_stale, by_lifecycle, duplicates, dead,
  `newest_observation_age_secs`, and **`engine_alive`** (an agent observed within
  2×`fresh_within`). If nothing observed recently → the discovery engine is likely stalled →
  red (the health_monitor-stall class, now surfaced for the control plane).
- `owner_gate_report` — open owner gates by kind + `oldest_age_secs` (pending-decision
  backlog; green, never a failure).
- `lease_report` — resource leases live vs `expired_stale` (surfaces expired-but-present rows
  so they are not mistaken for active ownership).
- `observability_summary` now folds these in: overall **red** if there are ACTIVE failures OR
  the engine looks stalled; gates/leases are informational.

## Live (read-only) result

`status=green, all_clear=true, engine_alive=true`. Registry: 9 agents, **7 observed_fresh**
(newest observation age 0s), 2 stale = the dead/retired `canary-synthetic-restart` +
`cp-canary-dup` (correct) — the misleading "9/9 stale-by-evidence" is resolved. Lifecycle:
managed 2 / observe_only 5 / dead 2; duplicates_flagged 1. Open-gate backlog: 6
(classify_scope 4, unverified_owner_decision 1, canary_agent_selection 1; oldest ~7.3h).
Leases: 2 total, 0 live, 2 expired_stale (harmless).

## Tests / commit

- Tests: **+6** in `test_control_plane_diagnostics.py` (engine alive vs stalled, duplicates,
  gate aging/kinds, lease live-vs-expired, summary red-on-engine-stall). Diagnostics file
  total **13**. Full suite: see run.
- Commit: local only. Endpoint `GET /api/v1/control-plane/observability` now includes
  registry_health / open_owner_gates / resource_leases.

## Genuine owner gates (unchanged)

The 6 open owner gates (scope classifications, arbitrage2 unverified-decision, canary
selection) are **pending owner decisions** — surfaced as backlog, resolvable only by the
owner. Not auto-resolved (provenance invariant). This is the genuine owner gate; no further
autonomous action taken on them.

---

# ADDENDUM 3 — counter reconciliation: current STATE vs monotonic HISTORY (read-only)

## `runtime.failed = 15` vs current 19 — reconciled

Every current lens over `runtime_jobs.db` yields **19** failed (not 15). `failed` is a
TERMINAL status: a failed job never un-fails, so the total only grows **monotonically**. The
external "15" is therefore a **stale earlier snapshot** of the same series, not a discrepancy.
`runtime_job_failure_report` now carries `monotonic_terminal=true` +
`reconcile_note` ("current authoritative total=19, active=0"). Newest failure 2026-07-28 →
**active (24h) = 0**. Reconciled: 15 (stale) → 19 (current historical) / 0 active.

## Notifier dead-letter — STATE vs HISTORY separated

A raw "history" counter conflated three different things:

| Counter | Value | Kind |
|---|---:|---|
| `dead_letter` rows (current STATE) | **17** | terminal state now |
| active (recent-window) dead-letter | **0** | current health |
| cumulative failure ATTEMPTS | **85** | monotonic history (17 × ~5 retries) |
| `notification_dead_letter` events logged | **17** | monotonic history |
| `notifications_red` events logged | **8** | monotonic history |

New `notification_history_report` separates the current STATE (`current_state` by state +
`current_dead_letter`) from the cumulative HISTORY (`cumulative_failure_attempts`,
`dead_letter_events_logged`, `notifications_red_events`) and the ACTIVE window. **Active = 0**
→ status green; the 85/17/8 history counters are monotonic and informational, not active
failures. Root remains owner-push RED (gate G4), owner-gated.

## Combined verdict (live, read-only)

`observability_summary` now includes `notification_history`. Overall: **active_failures_total
= 0, engine_alive = true, status = green.** All non-green raw counters (15/19 failed jobs,
17 dead-letter, 85 attempts) are historical/monotonic; **zero active failures**.

## Tests / commit

- Tests: **+3** (notification history STATE-vs-HISTORY split, active-when-recent,
  runtime monotonic reconciliation). `test_control_plane_diagnostics.py` total **16**.
  Full suite: see run.
- Commit: local only; read-only diagnostics only (SELECT / `mode=ro`). No live behavior
  changed. Endpoint `GET /api/v1/control-plane/observability` now includes
  `notification_history`.

---

# ADDENDUM 4 — CTO cursor lag + same-chat delivery drain health (read-only)

## Gap

The CTO contract requires surfacing a "stale cursor / failed delivery as health errors," but
no diagnostic existed for either the CTO consumer cursor lag or the agent_notifier same-chat
delivery backlog. A silently stalled drain (unacked commander_events piling up) means the
owner stops receiving same-chat messages with no signal.

## Added (read-only)

- `cto_cursor_report` — per CTO consumer: `lag` (latest_event_id − last_event_id) and
  `cursor_age`; a cursor is **stale** (health error) only when it has unread events AND has
  not advanced within the window. No registered consumer is informational, not an error.
- `commander_delivery_report` — same-chat delivery: `total` / `unacked` /
  `oldest_unacked_age` / `newest_ack_age` → **`drain_alive`**. Stalled = unacked backlog +
  no recent ack → red (owner silently not notified). Reads `agent_control.db` `mode=ro`; safe
  if the table is absent.
- `observability_summary` now red if the same-chat drain stalled OR a CTO cursor is stale.

## Live (read-only) result

- CTO inbox latest event **#57**; **0 durable consumer cursors** registered → informational
  green. *Observation:* the ChatGPT/CTO consumer currently reads ad-hoc rather than via the
  persisted cursor (`cto_brief_since(..., ack=true)`); wiring a durable cursor would let lag
  be measured — noted, not a failure.
- Same-chat drain: **477 total, 0 unacked, newest ack ~6.5 min ago → `drain_alive=true`,
  green** — agent_notifier is keeping up.
- Summary: `status=green, all_clear=true, engine_alive=true, same_chat_drain_alive=true,
  stale_cto_cursors=0`.

## Tests / commit

- Tests: **+7** (CTO no-consumer/current/stale/lagging-but-recent; commander drain
  alive/stalled/recent-ack-despite-unacked). `test_control_plane_diagnostics.py` total **23**.
  Full suite: see run.
- Commit: local only; read-only. Endpoint `/observability` now includes `cto_cursor` +
  `commander_same_chat_delivery`.

## Genuine owner-gated remainder

The dead-letter backlog clears only by enabling owner-push (**G4**, secret-bearing); the 6
open owner gates need owner decisions; a durable CTO consumer cursor requires the ChatGPT
side to adopt the ack contract. All owner-side — no autonomous action taken.

---

# ADDENDUM 5 — control-loop heartbeat liveness (stall detection, read-only)

## Gap

Only the discovery ENGINE had a liveness signal (registry `updated_at`). The other Owner OS
control loops — continuation watchdog, orchestrator, direct-agent-lifecycle — had **no stall
detection**, despite a silently stalled loop being the known health_monitor-stall failure
class (a loop that stops ticking with no signal).

## Added (read-only)

`loop_liveness_report` — per-loop heartbeat via each loop's own last-activity marker (a fresh
row every tick): `cw_health.last_run_at` (watchdog), `max(agent_orchestrator.updated_at)`
(orchestrator), `max(direct_agent_lifecycle.updated_at)` (lifecycle),
`max(control_plane.agent.updated_at)` (engine). A loop older than **3× its interval** is
`stalled` (red); a loop with no marker yet is `unknown` (informational). The **supervisor**
is reported separately as `no_heartbeat_marker` — its only marker (`supervisor_prompts`) is
written on a decision, not every tick, so its liveness is not assessable here. Reads each DB
`mode=ro`, tolerant of a missing table. `observability_summary` now goes red if any loop is
stalled.

## Live (read-only) result

All four measurable loops **alive**: continuation_watchdog (age 19s / 30s), orchestrator
(37s / 45s), direct_agent_lifecycle (36s / 45s), control_plane_engine (19s / 30s).
`stalled_loops=0`, summary `status=green, all_clear=true`. A future silent stall of any of
these now flips the summary red.

## Tests / commit

- Tests: **+4** (all-alive, stalled-watchdog detected, missing-marker=unknown, summary
  red-on-stall). `test_control_plane_diagnostics.py` total **27**. Full suite: see run.
- Commit: local only; read-only (SELECT / `mode=ro`). Endpoint `/observability` now includes
  `loop_liveness`. No defect found (all loops ticking); the diagnostic closes the
  stall-detection blind spot.

---

# ADDENDUM 6 — supervisor heartbeat (loop liveness now measurable) + re-check

## Gap closed

The supervisor only wrote `supervisor_prompts` on a decision (sparse), so its loop liveness
was not measurable. Added `agent_supervisor.heartbeat()` — a minimal **non-sensitive** per-tick
marker (timestamp + monotonic tick counter ONLY; no decision data / agent content / secrets)
into a new `supervisor_heartbeat` table, called at the start of each `run_loop` iteration
(best-effort; a heartbeat failure never affects supervision). `loop_liveness_report` now
includes the supervisor (interval 45s, stalled >3×); the "no_heartbeat_marker" special-case is
removed. Commit **`3bd853b`**.

- **Live proof:** after restart (PID 2163517), `supervisor_heartbeat` populated (ticks
  incrementing); `loop_liveness` shows **supervisor alive (age ~1s)**. All 5 loops alive:
  continuation_watchdog / orchestrator / direct_agent_lifecycle / control_plane_engine /
  supervisor — `stalled_loops=0`.

## Re-check (read-only) of the requested items

- **Loop liveness:** all 5 loops **alive**, no stall.
- **Notification drain:** same-chat via agent_notifier **alive** (drain keeping up). Owner-push
  posture **RED (gate G4)** — and now **1 ACTIVE dead-letter**: notification #20
  `newagent:payment:0.0` @10:58Z (within the 1h window), from the shadow loop discovering the
  `payment:0.0` agent (observe_only) → owner-action event → owner-push failed (channel
  disabled). This is the diagnostic **correctly** surfacing a real recent owner-push failure,
  not a new defect; same owner-gated root (G4). It reclassifies to historical after 1h.
- **Duplicate-agent inventory:** `cp-canary-dup` remains `dead`/flagged (historical); **1**
  live `claude` on the canary cwd (no live duplicate). `duplicates_flagged=1` (the dead one).
- **Flags:** `CONTROL_PLANE_ACTUATOR_ENABLED=1`, `CONTINUATION_VIA_ACTUATOR=1`,
  `CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0` — unchanged, scoped to the one canary.
- **Owner gates:** now **7** open (new: `classify_scope` for `payment:0.0` @10:58Z — the
  discovered payment agent's one unknown-scope decision). All owner-side.

## Summary status = RED (correct, owner-gated)

`observability_summary`: `status=red, active_failures_total=1, stalled_loops=0,
same_chat_drain_alive=true`. The single active failure is the payment-discovery owner-push
dead-letter above — a **real, recent owner-push delivery failure** the diagnostic is designed
to catch. It clears only by enabling owner-push (**G4**, secret-bearing, owner-gated) — no
autonomous action taken.

## Tests

- Touched suites green: `test_control_plane_diagnostics.py` (29) + `test_agent_supervisor.py`
  (10) = **39 passed**.
- Full suite: **935 passed + 1** pre-existing environment-fragile failure
  (`test_phase13::test_planner_hanging_child_survives_parent_exit_but_group_is_reaped`, which
  asserts on a global `pgrep -af "sleep 30"` and collided with unrelated host processes). It
  **passes in a clean environment** (verified: `1 passed` once the foreign `sleep 30`
  processes cleared) → clean-env total **936**. Unrelated to this change (which adds only a DB
  timestamp write + read-only diagnostics; no process/subprocess code).

---

# ADDENDUM 7 — actuation scope integrity (canary-confinement check, read-only)

## Gap

The actuator is armed live (scoped to `cp-canary:0.0`), but nothing continuously verified it
never broadened — no diagnostic would catch a real non-canary agent being actuated (a scope
breach / the worst safety failure for a single-agent cutover).

## Added (read-only)

`actuation_scope_report` — cross-checks the durable `cp_action` ledger against the canary
allowlist (read from the systemd drop-in `CONTROL_PLANE_CANARY_AGENTS`, no secrets). Every
actuated target must be on the allowlist or a known synthetic test target
(`canary-synthetic-*`); any REAL non-canary agent in the ledger is a **scope BREACH → red**.
`observability_summary` now goes red on `actuation_scope_breach`.

## Live (read-only) result — scope INTACT

- allowlist (from drop-in): `[cp-canary:0.0]`.
- actuated (cp_action distinct targets): `[canary-synthetic-restart:0.0, cp-canary:0.0]`.
- synthetic test targets: `[canary-synthetic-restart:0.0]`.
- **unexpected (breach): [] → green.** No real non-canary agent was ever actuated — actuation
  is confined to the one canary (+ the P2 synthetic test). Canary scoping preserved.
- Summary: `status=green, actuation_scope_breach=false, active_failures_total=0` (the earlier
  payment owner-push dead-letter has aged past the 1h active window → historical again).

## Tests / commit

- Tests: **+4** (scope green when confined; breach when a non-canary agent actuated; allowlist
  parsed from drop-in; summary red on breach). `test_control_plane_diagnostics.py` total
  **33**. Full suite: see run.
- Commit: local only; read-only (SELECT / drop-in read, no secrets). Endpoint `/observability`
  now includes `actuation_scope`. Canary scoping + owner gates unchanged; no defect (scope
  intact) — the diagnostic is a continuous safety guard.

---

# ADDENDUM 8 — internal consistency INVARIANTS (read-only) + re-verify of the 3 areas

Prior diagnostics MEASURE freshness/lag/scope; none verified internal-consistency invariants.
That was the genuine remaining gap. `consistency_report` checks (read-only):

1. **CTO cursor sanity** — no consumer cursor may point past the latest event
   (`last_event_id <= max(event.id)`).
2. **Notification state validity** — every `notification.state` is a known state.
3. **Ledger/lease fence integrity (across restart)** — for each agent,
   `max(cp_action.fence_token) <= resource_lease.fence_token` (an action recorded under a
   fence higher than its resource's current lease is impossible → corruption). Orphan actions
   (a cp_action target with no lease row) are reported informationally.

Any violation → red; `observability_summary` folds it in.

## Live (read-only) result — all invariants hold

`consistency_report` → `consistent=true, status=green`: `cursors_ahead_of_log=[]`,
`invalid_notification_states=[]`, **`fence_violations=[]`, `orphan_actions=[]`**. The canary
lease/action ledger is consistent across every restart this session (fences monotonic;
`cp-canary` action fence ≤ its lease fence; synthetic test target likewise).

## Re-verify of the three requested areas

- **(1) heartbeat freshness + false-idle** — `loop_liveness`: all 5 loops **alive**
  (continuation_watchdog / orchestrator / direct_agent_lifecycle / control_plane_engine /
  supervisor), `stalled_loops=0`. False-idle detection covered by `state_estimator` +
  actuator false-idle guard (unchanged, tested).
- **(2) notification cursor / outbox ack consistency** — `consistency_report` (cursor sanity +
  state validity) **green**; `cto_cursor_report` (lag) + `commander_delivery_report` (drain
  alive, 0 unacked) unchanged.
- **(3) canary lease/action ledger consistency across restart** — `consistency_report` fence
  invariant **green** (no violations, no orphans); `actuation_scope_report` confirms actuation
  confined to `cp-canary` + synthetic.

## Tests / commit

- Tests: **+6** (all-invariants-green, cursor-ahead red, invalid-state red, fence-violation
  red, orphan informational, summary red-on-violation). `test_control_plane_diagnostics.py`
  total **39**. Full suite: see run.
- Commit: local only; read-only. Endpoint `/observability` now includes `consistency`. No
  defect found (all invariants hold) — the diagnostic is a continuous corruption guard.
  Canary scope + owner gates preserved.

---

# ADDENDUM 9 — summary `red_reasons` aggregation (read-only) + context preservation

Context was at 100%; continuation state captured in `reports/OWNER_OS_CONTROL_PLANE_V2_STATE.md`
(HEAD, diagnostics inventory, live health, flags, open gates incl. G-PAY-1/2/3, candidate
next gaps).

## Gap

`observability_summary` returned a top-level `status` but no consolidated reason list — a
consumer could not see WHICH check failed without parsing every sub-report.

## Added (read-only)

`observability_summary.red_reasons` — a list of the specific failing checks (empty ⇒ green):
`active_failures=N`, `discovery_engine_stalled`, `stalled_loops=N`, `same_chat_drain_stalled`,
`stale_cto_cursors=N`, `actuation_scope_breach=[...]`, `consistency_violation`. `all_clear` =
`red_reasons == []`.

## Live (read-only)

`status=green, red_reasons=[]` (the earlier payment owner-push dead-letter aged out of the 1h
window). A future RED now self-explains via `red_reasons`.

## Tests / commit

- Tests: **+2** (red_reasons empty when green; names the failing check on a violation).
  `test_control_plane_diagnostics.py` total **41**. Full suite: see run.
- Commit: local only; read-only. Canary scope + owner gates preserved; no defect.
