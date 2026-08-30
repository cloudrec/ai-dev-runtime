# Owner OS — Control Plane V2 architecture

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` · **Status:** design + phased build
**Author:** Commander (autonomous) · **Scope:** `/root/ai-dev-runtime` control plane only.
Trading services, exchange credentials and secrets are out of scope and never touched.

> Mandate: replace the fragmented supervisor/orchestrator/commander/watcher/automation
> mesh with ONE event-driven control plane, a single durable source of truth, explicit
> state machines, verified actuation, a real notification pipeline, and behavioral
> acceptance — no more patchwork. Continue autonomously through safe phases; stop only at
> a genuine owner gate.

---

## 1. Current-system inventory (grounded)

### 1.1 Control loops (all in-process in `ai-runtime.service`)

| Loop | File | Interval | Responsibility | Commands a pane? |
|---|---|---|---|---|
| Supervisor | `core/agent_supervisor.py` | 20s | auto-approve provably-safe permission prompts | **yes** (`approve_prompt` → `1`) |
| Orchestrator | `core/agent_orchestrator.py` `refresh_and_resolve` | 45s | derive per-agent state; watcher/stuchalka resume; transition events; context-budget dispatch; phase-advance; reaper; plan orchestration; calls `direct_agent_lifecycle.sweep` | **yes** (`ensure_auto_mode`, `agent_send` resume, plan `agent_send`) |
| Continuation watchdog | `core/agent_continuation_watchdog.py` | 30s | verified continuation submit + retry | **yes** (`agent_send`, Enter, `robust_submit`) |
| Context budget | `core/agent_context_budget.py` (via orchestrator) | 45s | `/clear` rotation + resume handoff | **yes** (`agent_send "/clear"` + resume) |
| Phase advance | `core/agent_phase_advance.py` (via orchestrator) | 45s | dispatch approved next-phase text | **yes** (`agent_send` task text) |
| Direct lifecycle | `core/direct_agent_lifecycle.py` (via orchestrator) | 45s | completion/interruption events (observe-only) | no |
| AI planner | `core/ai_planner.py` | loop | job planning | no (job pipeline) |

**Root defect #1 — no arbitration:** FIVE code paths (`agent_supervisor`,
`agent_orchestrator` watcher + `ensure_auto_mode`, `agent_continuation_watchdog`,
`agent_context_budget`, `agent_phase_advance`) can send keystrokes/text to the **same
pane** with no lease or single owner. This is the source of duplicate/contradictory
commands and races.

### 1.2 State stores (`agent_control.db`, 15 tables — fragmented)

`agent_orchestrator` (per-agent record) · `orchestrator_goal` / `orchestrator_task` /
`orchestrator_state` (plan) · `commander_events` (event log) · `cw_step` / `cw_target` /
`cw_health` (watchdog) · `direct_agent_lifecycle` / `direct_agent_lifecycle_metrics` ·
`supervisor_prompts` · `agent_context_rotation` · `agent_phase_text` · `deliveries`
(send idempotency) · `pane_tail_cache`. Plus `runtime_jobs.db` (`jobs`),
`runtime_releases.db` (`releases`).

**Root defect #2 — no single source of truth:** an agent's "state" is spread across
`agent_orchestrator.state`, `cw_target.last_state`, `direct_agent_lifecycle.state` and
recomputed ad-hoc each loop. Controllers disagree; there is no one authoritative
per-agent record with desired/actual/next-action/evidence/owner-gate/owner-controller.

### 1.3 Notification path

`commander_events` rows are appended by every controller (`record_commander_event`,
deduped by `(agent,event_type,dedup_key)`) and **drained externally** via
`GET /api/v1/agents/commander/events` + `.../ack`. `core/notify_format.py` renders text.
There is **no durable outbox with delivery attempts/receipts/retry inside ai-runtime**;
actual Telegram send + the "hourly" cadence live in external ChatGPT automations.

**Root defect #3 — notification is fire-and-forget:** "recorded an event" ≠ "owner was
notified". No delivery proof, no retry, no channel-health, no resolution events tied to
the originating gate. A disabled channel is invisible.

### 1.4 External automations (evidence of manual kicking)

Local cron/timers exist for other projects (partners, sealed-factory, seo-backup,
email backup, ezetta-agent-hb) but **not** for agent continuation. The agent-continuation
cadence was an **external ChatGPT hourly automation**, plus manual `agent_send` pushes —
delivery keys observed in `deliveries`: `arb2-force-continue-after-watcher-missed-enter`,
`arb2-permanent-autocontinue-rule`, `arb2-autowatch-resume`, etc. This is exactly the
"manual kicking / ChatGPT-as-primary-controller" the mandate forbids.

## 2. Root-cause analysis

| Symptom (observed) | Root cause |
|---|---|
| False health (`agents_checked=0` looked "ok"; health from absence of evidence) | no explicit unknown/stale state; health derived from missing data |
| Silent idle (arbitrage2-opus idle, typed prompt unsubmitted) | fire-and-forget delivery; `submitted=true` from keystroke rc, not consumption |
| Duplicate / contradictory commands | 5 controllers, no lease/arbitration, overlapping idempotency namespaces |
| Missed owner notifications | no durable outbox/receipts; external drain only |
| Manual kicking / ChatGPT primary | no continuous internal controller owning the cadence |
| Contradictory state | state spread across ≥4 tables, recomputed per loop |
| Patchwork growth | each incident added a new watcher instead of a shared plane |

## 3. Target architecture

One **Control Plane** = a single event-driven engine with a single durable store and
explicit state machines. Layers:

```
 sources (tmux panes, Runtime jobs, budget, owner replies, clock)
        │  observe (deterministic collectors, event-driven + bounded poll fallback)
        ▼
 EVENT BUS  ──►  EVENT LOG (append-only, durable, single table)
        │
        ▼
 REDUCERS ──► SINGLE SOURCE OF TRUTH (entities: Project, Goal, WorkItem, Agent,
        │      AgentTurn, Decision, OwnerGate, Evidence, Notification, Budget,
        │      ResourceLease, Policy)  — desired vs actual, next action, evidence
        ▼      freshness, owner-gate, responsible controller, all EXPLICIT
 CONTROL LOOP (per Agent/WorkItem):
   Observe → Normalize → Diagnose → Plan → Policy-check → Act(verified) → Verify
   → Record(event) → Notify(outbox)
        │        (only the ONE controller holding the ResourceLease may Act)
        ▼
 ACTUATOR (single verified-delivery service; ACK/consume/transition required)
 NOTIFIER (durable outbox → channels → receipts → retry → resolution)
 CTO API (read model: goals, actual state, deltas, blockers, decisions, evidence,
          last action, notification delivery proof)
```

Model tiering (budget §8): deterministic code for continuous fast checks; a cheap model
for classification/summarization; an Opus-class model only for ambiguous planning/
diagnosis. No endless context — durable structured memory + rolling summaries + exact
evidence references + restart-safe continuation cursors.

## 4. Data model (single source of truth)

New database `control_plane.db` (additive; legacy DBs preserved, read during migration).
Core tables (SQLite, migration-managed):

- **project**(id, name, root, priority, status, definition_of_done, updated_at)
- **goal**(id, project_id, text, priority, status, dod, stop_conditions)
- **work_item**(id, goal_id, project_id, title, kind, desired_state, actual_state,
  next_safe_action, status, depends_on, artifact_refs, updated_at)
- **agent**(id, target, session, project_id, conversation_id, desired_state,
  actual_state, evidence_fresh_at, responsible_controller, lease_id, last_action,
  updated_at) — one row = authoritative per-agent truth
- **agent_turn**(id, agent_id, conversation_id, started_at, ended_at, summary_ref,
  tokens, outcome)
- **event**(id, ts, source, type, entity_type, entity_id, payload, evidence_ref,
  correlation_id) — append-only bus/log, the ONE event table
- **decision**(id, entity_id, policy_class, action, rationale, model_tier, ts)
- **owner_gate**(id, work_item_id, agent_id, reason, kind, state[open/answered/expired],
  correlation_id, opened_at, answered_at, answer)
- **evidence**(id, kind[report/test/pane/conversation/command], ref, hash, observed_at,
  freshness) — nothing is "true" without an evidence row; staleness explicit
- **notification**(id, event_id, channel, dedup_key, state[pending/sent/failed/acked/
  resolved], attempts, last_attempt_at, receipt, correlation_id)
- **budget**(id, scope, model, tokens, cost_usd, cpu, ram, disk, window, updated_at)
- **resource_lease**(id, resource[agent:target], holder_controller, acquired_at,
  expires_at, fence_token) — the arbitration primitive
- **policy**(id, action_pattern, policy_class[autonomous_safe/owner_approval/prohibited],
  scope, rationale)

**Explicit unknown/stale:** `actual_state` may be `unknown`; `evidence_fresh_at` drives a
derived `stale` flag. Health NEVER inferred from absence — a missing collector yields
`unknown` + a controller-health event, not "ok".

## 5. State machines

**Agent.actual_state:** `unknown → registered → working → thinking → idle → {waiting_owner,
blocked, context_limit, repeated_loop, unrelated_work, dead}`; recovery edges back to
working; `dead → recovered(same conversation_id)`. Transitions require an evidence row.

**WorkItem.status:** `planned → ready(deps met) → in_progress → {blocked, needs_owner} →
verifying → done | abandoned`. A `done` work item MUST either spawn the next `ready`
work item (documented safe next action) or open an `owner_gate` — never dead-ends.

**OwnerGate.state:** `open → notified → answered → resumed → closed`; `open → expired`
(with re-notify policy). Answer is correlated to the exact gate/work item and triggers the
responsible controller to resume the exact agent (same conversation_id).

**Notification.state:** `pending → sending → sent → acked | failed → retry(backoff) →
dead_letter`; a cleared blocker emits a `resolution` notification tied to the origin.

**ResourceLease:** `free → held(holder, fence_token, ttl) → expired/released`. Only the
lease holder may Act on that agent; fence token rejects stale actuations after restart.

## 6. Policies & safety (machine-readable)

`policy` rows classify every action:
- **autonomous_safe** — inside approved project scope: continue safe next work item,
  run tests/reports/backups, submit a documented safe continuation, summarize.
- **owner_approval_required** — anything consequential/ambiguous: unknown prompts,
  cross-project actions, large refactors, resource contention resolution.
- **prohibited (owner-gated, never auto)** — live trading, payments, credentials/secret
  rotation, destructive DB/filesystem ops, external publication, Git history rewrite,
  `push`/`deploy` unless a narrow pre-approved rule exists.

Every `Act` records a `decision` row (policy_class + rationale + model_tier). The actuator
refuses `prohibited`/unclassified text (deny-by-default), reusing the proven
`_FORBIDDEN_RE` + `permission_resolver.classify_command`.

## 7. Verified actuation (the emergency-repair lesson, generalized)

Single **Actuator** service. Every command requires evidence of effect — ALL of:
`submitted` (keystroke ok), `pane_changed`, `prompt_consumed`, `conversation_modified`,
`state_transitioned` — within a timeout; else one robust retry (clear+paste+Enter), else a
durable blocker + owner gate. Typed text is NEVER success. Idempotency by
`(agent, conversation_id, action_hash)` with a fence token; restart-safe; blocked actions
self-heal after cooldown. (Generalizes the accepted `agent_continuation_watchdog` fix into
the one path every controller must use.)

## 8. Budget / resource control

`budget` + `resource_lease` tracked continuously. Router: deterministic checks free;
classification/summaries on the cheap model; Opus-class only for ambiguous planning. On
budget/resource pressure: **pause/reschedule** work items by priority and release leases —
never blind-kill. Heavy-job concurrency capped via leases.

## 9. Migration map (canonical / migrate / deprecate) — no parallel patches

| Legacy component | Fate | V2 home |
|---|---|---|
| `agent_control.py` (tmux primitives, classify, `_deliver`) | **keep** as low-level driver | Actuator + Collector call it |
| `agent_orchestrator.refresh_and_resolve` | **migrate** → decomposed into Collector + Reducer + per-work-item Controller | control_plane engine |
| `agent_supervisor` (approve safe prompts) | **migrate** → a Policy+Actuator action under lease | Controller action |
| `agent_continuation_watchdog` | **migrate** → the canonical Actuator verify/retry logic | Actuator |
| `direct_agent_lifecycle` | **migrate** → Collector emitting completion/interruption events | Collector/Reducer |
| `agent_watcher.transition_event` | **migrate** → Reducer state-transition rules | Reducer |
| `agent_context_budget` | **migrate** → Budget controller action (lease-gated) | Budget controller |
| `agent_phase_advance` / `orchestrator_plan` | **migrate** → WorkItem/Goal engine | SoT + Controller |
| `commander_events` + external drain | **migrate** → `event` log + `notification` outbox with receipts | Notifier |
| external ChatGPT hourly automation | **demote** → secondary notification fallback only | Notifier fallback |
| multiple per-controller state tables | **deprecate** after backfill into SoT | `control_plane.db` |

**Cutover rule:** exactly ONE controller holds an agent's lease at a time; legacy
actuation paths are disabled as each is migrated (feature-flagged), so two controllers can
never command one agent. Legacy DBs preserved read-only for rollback + backfill.

## 10. Real acceptance matrix (behavioral — deterministic test + live canary)

| # | Scenario | Proof required |
|---|---|---|
| 1 | agent completes & idles → controller advances safe next work item, no owner | new event + verified actuation + WorkItem→next |
| 2 | real owner gate → controller stops + correlated notification | owner_gate open + notification sent+receipt |
| 3 | owner reply → exact agent resumes | gate answered → same conversation resumed (verified) |
| 4 | typed-but-not-submitted → detected + recovered | actuator verify fail → robust retry → verified |
| 5 | service restart mid-action → no duplicate command | fence token rejects stale; idempotency holds |
| 6 | agent crash → recovery same conversation_id | dead→recovered, conversation_id preserved, no new agent |
| 7 | stale/false working → corrected | evidence-freshness flips to stale; state corrected |
| 8 | duplicate agents → refused | lease + registry refuse second agent on same cwd |
| 9 | notification disabled/fails → visible blocker + retry | channel-health blocker + outbox retry/dead-letter |
| 10 | context growth / repeated loop → diagnosed + bounded | reducer flags context_limit/repeated_loop; bounded action |
| 11 | two projects contend for resources → priority/lease respected | lease arbitration by priority; loser paused |
| 12 | unsafe pending action → blocked, never executed | policy=prohibited → deny + gate, 0 actuations |
| 13 | CTO status query → current verified truth, not cached prose | read model from SoT + evidence + delivery proof |

Completion is NOT declared from unit tests; a **sustained live canary** (approved agents,
e.g. arbitrage2-opus + a second) must run without manual kicks, false health, silent idle,
duplicate commands, or missed notifications.

## 11. Phased rollout (small, reversible; each phase = its own commit + tests + backup)

- **P0 — Foundations (this branch):** `control_plane.db` + migrations + backups of legacy
  DBs; `event` log + append API; entity schema; read-only — no actuation. *Reversible: drop
  new DB.*
- **P1 — Collectors + Reducers (shadow):** observe panes/jobs/budget → events → SoT, in
  parallel with legacy, **read-only** (no commands). Compare SoT vs legacy for a canary
  window (detect false-health/stale). *Reversible: stop collectors.*
- **P2 — Actuator + Leases:** single verified-delivery service + `resource_lease`; migrate
  continuation_watchdog logic into it; still gated behind a flag. Prove idempotency +
  fence-token restart safety.
- **P3 — Notifier outbox:** durable `notification` outbox + delivery receipts + retry +
  channel-health blocker + resolution events; commander_events becomes a shim writing to
  it; external drain kept as fallback.
- **P4 — Controllers under lease:** move supervisor-approve, continuation, context-budget,
  phase-advance into lease-gated Controller actions; **disable** each legacy actuation path
  as its replacement goes live (one-owner guarantee).
- **P5 — WorkItem/Goal engine:** desired-vs-actual planning, done→next-or-gate, contention
  detection, strategic review.
- **P6 — CTO API v2:** read model surface with delivery proof; demote ChatGPT hourly to
  fallback.
- **P7 — Live canary + cutover:** sustained multi-agent canary; deprecate legacy tables
  after backfill; final acceptance-matrix run.

Each phase: exact changed files, migration, tests, live evidence, enabled/disabled legacy
components, rollback. No Git push/PR/publication without owner approval. Trading services,
exchange credentials and secrets untouched.

## Genuine owner gates (recorded, not "say the word")

These require owner decision and will be surfaced as `owner_gate` rows when reached — work
does not block on them for the safe phases P0–P6:
- **G1** cutover authorization for P4 (disabling legacy actuation on live agents);
- **G2** enrolling any NEW project/agent beyond the already-approved `arbitrage2-opus`,
  `seo-audit`, `job`;
- **G3** any push/PR/publication of this branch;
- **G4** touching a Telegram/credentialed notification channel config (secret-bearing).

Autonomous work proceeds through P0–P3 (and P4 for already-approved agents only) now.

---

# ADDENDUM 2026-08-03 — auto-discovery, CTO inbox, delivery matrix (non-negotiable)

Three owner requirements folded into V2 as first-class scope (not new watchers).

## R1 — Zero-manual-registration agent discovery

**Visibility never depends on a static YAML allowlist; static policy limits ACTIONS
only.** The Control Plane engine enumerates live tmux Claude agents every tick and
reconciles the durable `agent` AgentRegistry:

- lifecycle: `discovered → classifying → {managed | observe_only | blocked_unknown_scope}
  → dead → recovered`; fields `first_seen_at, pid, command, cwd, conversation_id,
  duplicate_of`.
- `classify_scope(cwd, session, config)`: a `mode: auto` session (or a cwd under its
  root) → **managed** (inherits bounded safe policy); a configured non-auto or
  allowed-root-but-unconfigured cwd → **observe_only** with an inferred candidate
  project + **one** correlated owner decision (gate); anything else →
  **blocked_unknown_scope** + owner decision. Unknown scope is never ignored.
- reconciliation without duplicates: rename/move/restart matched by `conversation_id`
  (old target retired, not duplicated); resume preserves `conversation_id`; a dead record
  whose conversation returns → **recovered**; >1 live agent on one cwd → the oldest
  first-seen is primary, others flagged `duplicate_of` with **no conflicting command**.
- Impl: `core/control_plane/discovery.py`. **Live canary (read-only, no config edit):**
  6 real agents auto-discovered — `arbitrage2-opus`→managed, `email`/`ezetta-video`/
  `owneros-direct-fix`/`polyinput`/`security`→observe_only, with owner-decision gates for
  unknown scope; recorded in registry + CTO inbox.

## R2 — CTO event inbox / push contract

The append-only `event` table **is** the durable canonical CTO inbox (v2 fields:
`project_id, agent_id, severity, owner_action_required, action_taken, dedup_key,
supersedes, resolves`). Contract (`core/control_plane/cto.py`):

- `emit(...)` records a correlated event and, for `high`/`critical` or
  `owner_action_required`, enqueues a durable **owner push** (outbox).
- `cto_brief_since(consumer, ack=)` returns exact **verified deltas** since the consumer's
  persistent cursor (`cto_cursor`), never cached prose; `ack_through` advances the cursor
  monotonically. Restart-safe: cursor lives in the DB → no loss / no duplication.
- Four explicitly-separated concerns (no impossible claims): (a) server-side continuous
  control; (b) owner push; (c) durable CTO inbox consumed on ChatGPT's **next** invocation;
  (d) optional scheduled wake. Standard consumer flow: read cursor → live-verify changed/
  critical items → respond; a stale cursor / failed delivery surfaces as a health error.
- APIs: `GET /api/v1/control-plane/cto/brief`, `POST .../cto/ack`, `.../registry`,
  `.../notifications/status`.

## R3 — Delivery capability matrix (fail-closed; same-chat honesty)

The intended behavior is a **proactive new assistant turn in the same ChatGPT chat** — not
merely server awareness or Telegram. Because that needs a platform inbound trigger that may
not exist, delivery is a capability matrix (`core/control_plane/delivery.py`), fail-closed:

| Tier | Proactive? | Availability rule | Status now |
|---|---|---|---|
| `same_chat_wake` | yes (ideal) | ONLY if a real inbound trigger (`CONTROL_PLANE_SAMECHAT_WAKE_URL`) is configured AND probed healthy | **unavailable / not complete** (no proven inbound trigger) |
| `owner_push` (Telegram) | yes | `TELEGRAM_BOT_TOKEN`+`CHAT_ID` or `WATCHDOG_TELEGRAM_ENABLED` | disabled → **RED** |
| `scheduled_chatgpt` | no (hourly) | `CHATGPT_HOURLY_ENABLED` | fallback only; hourly; `notifications_enabled=false` → **not the pinger** |
| `cto_inbox` | no (pull) | always | durable floor |

- `notifications_status()` is **RED** unless a proactive channel (same-chat wake or a
  healthy owner push) is enabled. `notifications_enabled=false` is a red health error,
  **never** "working delivery"; a red posture raises a durable critical blocker event.
- **Same-chat instant wake is NOT claimed complete** — `same_chat_wake_complete` is true
  only when a real end-to-end assistant turn is proven with no user turn. Current honest
  status: **red / not complete** (live canary: `status=red, notifications_enabled=false,
  same_chat_wake_complete=false, reasons=[owner_push disabled, same_chat_wake unavailable]`).
  This is a recorded **owner gate G5** (wire a supported inbound trigger or accept
  push+inbox as the delivery contract).

## Acceptance A–F (mapped)

| # | Requirement | Where proven |
|---|---|---|
| A | manual new agent discovered, no config edit → new-agent CTO event | `test_manual_new_agent_discovered...` + live canary (6 agents) |
| B | known project → managed; unknown → observe_only + one decision | `test_classify...`, `test_unknown_project...` + live canary |
| C | complete/idle → controller safely continues, verified | accepted emergency repair (continuation watchdog) → migrating into Actuator (P2/P4) |
| D | event pushed + in inbox; forced failure visible/retried, never silent | `test_control_plane_delivery.py` (RED, fail-closed, retry) |
| E | CTO consumer cursor: exact deltas, ack, restart no loss/dup | `test_cto_cursor_deltas_ack_and_restart...` |
| F | two agents same project → duplicate detected/refused, no conflict | `test_duplicate_agents_same_cwd_flagged` |

## Updated genuine owner gates

- **G1** cutover to lease-gated actuation on live agents (P4).
- **G2** enrolling NEW projects for AUTONOMOUS action (discovery/observe needs no gate).
- **G3** any push/PR/publication.
- **G4** Telegram/credentialed channel config (secret-bearing).
- **G5** same-chat proactive wake: provide a supported inbound trigger, or accept
  push+durable-inbox as the delivery contract. Until then delivery health is RED by design.

Discovery + CTO inbox + delivery health ship now as **P1 SHADOW (observe-only)** — no pane
actuation — so they are safe and reversible ahead of the P4 actuation cutover.
