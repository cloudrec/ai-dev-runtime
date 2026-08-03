# Owner OS Control Plane V2 — progress report (P0 + P1)

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local only; not pushed)
**Architecture:** `reports/OWNER_OS_CONTROL_PLANE_V2_ARCHITECTURE.md`

## Delivered this cycle

### P0 — durable single source of truth (`5822bac`)
- `core/control_plane/{store,api}.py` + `control_plane.db` (separate, additive, reversible).
- Entities: project, goal, work_item, agent, agent_turn, event, decision, owner_gate,
  evidence, notification, budget, resource_lease, policy.
- EXPLICIT unknown/stale (`is_stale` never infers health from absence); evidence rows
  anchor freshness; `resource_lease` with a **monotonic fence token** (single holder,
  restart-safe); owner-gate correlation; durable notification outbox.
- Legacy DBs backed up + checksummed: `backups/control_plane_v2_p0/`.

### P1 — zero-config discovery + CTO inbox + delivery matrix, SHADOW/observe-only (`e713433`)
- `discovery.py` — enumerate live tmux Claude agents each tick; reconcile the durable
  AgentRegistry with **no allowlist for visibility**; classify managed/observe_only/
  blocked_unknown_scope; rename/resume/restart reconciled by conversation_id (no
  duplicates); dead→recovered; duplicate agents flagged (oldest-first-seen primary), no
  conflicting command.
- `cto.py` — the event log AS the durable CTO inbox; `cto_brief_since(cursor)` = verified
  deltas only; persistent per-consumer cursor (restart-safe); `emit()` enqueues owner push
  for high/critical/owner-action.
- `delivery.py` — fail-closed capability matrix; `notifications_status()` RED unless a
  proactive channel is healthy; `notifications_enabled=false` is a red error, never
  healthy; same-chat wake NOT claimed complete without a proven E2E turn.
- `engine.py` — P1 shadow loop (discovery + channel-health), started in `api/main.py`,
  gated `CONTROL_PLANE_ENABLED`. **No pane actuation.**
- APIs: `GET /api/v1/control-plane/{cto/brief, registry, notifications/status}`,
  `POST /cto/ack`.

## Live evidence (deployed in `ai-runtime.service`, PID 567625)

- Startup log: `control plane engine started (SHADOW/observe-only, interval 30s)`.
- Registry auto-populated by the live loop with **6 real agents, no config edit**:
  `arbitrage2-opus→managed`, `email / ezetta-video / owneros-direct-fix / polyinput /
  security → observe_only`.
- Event log: 6 `new_agent_discovered`, 4 `owner_gate_opened` (unknown-scope decisions),
  1 `notifications_red` (critical).
- Delivery posture honest: `status=red, notifications_enabled=false,
  same_chat_wake_complete=false`.
- **Zero actuation** confirmed: 0 `cp:` pane deliveries — the shadow plane issued no
  commands.
- Tests: **854 passed** (25 new control-plane: foundation 11, discovery/CTO 8, delivery 6).

## Acceptance matrix status

| # | Scenario | Status |
|---|---|---|
| A | manual new agent discovered, no config edit | **PASS** (test + live: 6 agents) |
| B | known→managed, unknown→observe_only + one decision | **PASS** (test + live) |
| C | complete/idle → safe continue verified | accepted emergency repair (continuation watchdog) — being migrated into the single Actuator (P2/P4) |
| D | event pushed + inbox; forced failure visible/retried | **PASS** (delivery tests; live RED blocker) |
| E | CTO cursor deltas, ack, restart no loss/dup | **PASS** (test) |
| F | duplicate agents detected, no conflict | **PASS** (test) |
| 5 | restart mid-action no duplicate command | **PASS at unit** (fence token); full live proof in P2 |
| 7/10/11/12 | stale-corrected / loop-bounded / lease-contention / unsafe-blocked | primitives in place (evidence-freshness, policy, lease); wired in P2–P5 |

## Owner gates recorded (not blocking safe phases)

- **G1** cutover to lease-gated actuation on live agents (P4).
- **G2** enrolling NEW projects for AUTONOMOUS action (discovery/observe needs no gate).
- **G3** any push / PR / publication of this branch.
- **G4** Telegram/credentialed channel config (secret-bearing).
- **G5** same-chat proactive wake: provide a supported inbound trigger, or accept
  owner-push + durable CTO inbox as the delivery contract. Until then delivery is RED by
  design (honest, not a bug).

## Remaining phases (safe, autonomous unless a gate is hit)

- **P2** Actuator + Leases: fold the verified-delivery logic (submitted+pane_changed+
  prompt_consumed+conversation_modified+state_transitioned, robust retry, fence guard)
  into ONE actuator behind a flag; prove restart no-dup live.
- **P3** Notifier: outbox delivery attempts/receipts/retry/backoff; commander_events shim.
- **P4** Controllers under lease (GATE G1): migrate supervisor-approve / continuation /
  context-budget / phase-advance to lease-gated actions; disable each legacy actuation
  path as its replacement goes live (one-owner guarantee).
- **P5** WorkItem/Goal engine (done→next-or-gate, contention, strategic review).
- **P6** CTO API v2 read model + demote ChatGPT hourly to fallback.
- **P7** Sustained multi-agent live canary + cutover + legacy deprecation.

## Rollback

- Disable: `CONTROL_PLANE_ENABLED=0` → shadow loop no-ops; restart. Legacy loops untouched.
- Remove: drop `control_plane.db` (gitignored runtime state); `git revert e713433 5822bac`.
- Legacy DBs preserved read-only in `backups/control_plane_v2_p0/` with checksums.

## Scope / safety

No push/PR/publication. No trading service, exchange credential, or secret touched. P0/P1
are additive and observe-only; no legacy controller was modified or disabled yet (that is
the P4 cutover, owner-gated G1).

---

# ADDENDUM 2026-08-03 — live recovery + recovery-path fix + provenance invariant

## Live recovery of arbitrage2-opus (SIGTERM 143 @ 04:17:43) — PASS

- **Proved dead before touching anything:** session `arbitrage2-opus` had exactly 1 pane,
  `pane_dead=1`; process 3384800 GONE; no live Claude on `/opt/arbitrage2` (no duplicate);
  conversation `64715514-…` jsonl present on disk (7.5 MB).
- **Root defect (fixed, `918db00`):** `agent_resume` refused because `has-session`
  succeeds even when every pane is dead. New `_session_liveness` (pane_dead AND
  process-alive per pane) → if all-dead, fenced `kill-session` then `claude --resume`; a
  live pane is still refused. Deterministic tests added.
- **Recovery evidence:** new pane live, **PID 605876**, cwd `/opt/arbitrage2`, cmdline
  `claude --resume 64715514-f6bc-4290-9390-cda19127bc17`; **same conversation** (only one
  jsonl, mtime advanced 04:17→04:25→…), `duplicate_created=false`; driven to **working**
  on the documented safe step `Continue with the fault-matrix extension and replay
  harness`.
- **Correlated CP events:** `session_ended` → `recovery_attempted` → `recovery_verified` →
  `recovery_working_verified` (all under one correlation id).
- **Notification evidence preserved:** the death reached chat via `agent_notifier`; that
  path is untouched.

## Owner-decision PROVENANCE invariant (fixed, `6d56cc7`)

- **Critical finding:** the resumed transcript showed `User answered Claude's questions:
  Stop selling, waitlist instead` — a **pane UI answer summary @02:23 with NO authenticated
  owner decision** (the ChatGPT conversation has none). The transcript already carried a
  queued "URGENT owner-decision integrity rule" flag for exactly this string. Acting on it
  would have been an unverified business/payment/publication decision.
- **Hard rule implemented:** no owner-gated action may proceed from raw pane text / UI
  answer summary / model default / resumed transcript / automation prose. Resolution
  requires a durable correlated `owner_decision` (source_channel + authenticated actor +
  timestamp + question/gate id + exact answer + consumption state). Unknown/untrusted/
  mismatched/consumed ⇒ **gate stays open, action blocked, critical inbox event**.
  `TRUSTED_CHANNELS` excludes pane/transcript/UI/automation sources.
- **Live handling:** opened a **blocked** `owner_gate` (`unverified_owner_decision`) +
  `decision_provenance_unverified` (severity=critical, owner_action_required) for the
  stop-selling claim; the Esc dismissed the menu with **no selection**; the agent was
  driven only to the safe engineering step. The stop-selling claim was never acted on.
- **Tests:** forged/stale/resumed text, missing decision, untrusted channel,
  answer-to-wrong-question, duplicate-answer, empty-answer, verified happy path.

## P2 status

Actuator/leases primitives are in place: `resource_lease` + monotonic fence
(`lease_is_current`, restart-safe, tested P0); verified-actuation pattern (submitted +
pane_changed + prompt_consumed + conversation_modified + state_transitioned, robust retry)
proven in the accepted continuation watchdog and exercised live in this recovery; the
provenance invariant gates any owner-gated actuation. P2 next: fold these into ONE
lease-gated Actuator service behind a flag and prove restart no-duplicate on a live agent
(the fence primitive already blocks stale actuation). P4 cutover remains owner-gated (G1).

Suite after this addendum: see final line of the progress run (control-plane + recovery +
provenance tests all green).

---

# ADDENDUM 2026-08-03 (2) — P2 Actuator/Leases + P3 Notifier outbox

## P2 — single lease-gated Actuator (`a4cebdd`)

`core/control_plane/actuator.py` — the ONE canonical path that may command a pane, gated
`CONTROL_PLANE_ACTUATOR_ENABLED` (**default OFF**; shadow/canary, not the P4 cutover):
- **Lease + monotonic fence guard** — caller must hold the current lease for
  `agent:<target>` at the current fence; a stale fence (a queued/retried action from before
  a restart, after which the controller re-acquired higher) is rejected. Fence re-asserted
  AFTER delivery (a mid-action re-lease does not record our success).
- **Policy gate (deny-by-default)** — `autonomous_safe` is NARROW (a documented
  continuation meta-instruction only); prohibited (destructive/live/payment/credential/
  publication) and owner_approval (any other free-form text) are blocked + raise a
  correlated owner gate.
- **Idempotency** — `cp_action` ledger (store v4), keyed by (target, conversation, action);
  a verified action is never re-issued.
- **Verified delivery folded** from the accepted continuation watchdog (5 proofs + one
  robust retry, else blocker + gate); on success advances the agent SoT to working + evidence.
- tests: disabled no-op; stale/no lease rejected; prohibited + owner-approval blocked with
  gate and no delivery; safe+lease verified; idempotent; restart stale-fence rejected while
  re-leased fence proceeds; verify-fail → blocker+gate.
- **Live restart-no-duplicate canary (synthetic target, no real pane touched):** across a
  fence1→fence2 restart + idempotent replay, **exactly ONE delivery** — stale fence rejected
  (0 sends), current fence verified (1), replay `already_verified` (0). The running service
  keeps the actuator DISABLED (verified live: env unset; `cp_action` holds only the synthetic
  canary row — no real agent actuated).

## P3 — notifier outbox drain (`<this commit>`)

`core/control_plane/notifier.py` — drains pending/failed notifications through the
fail-closed delivery matrix, records receipts, bounded retry, then dead-letters + raises a
critical event (a stuck/disabled channel is never silent). `cto.emit(push=False)` makes
channel-health meta-events (notifications_red, dead_letter) inbox-only so they cannot
recurse through the down channel. Wired into the shadow engine tick (sends notifications
only; touches no pane).
- **Live evidence:** shadow loop draining; owner-decision pushes for the observe_only
  agents are correctly `failed` (owner_push channel disabled = RED) — visible, not silent;
  `notifications_red` deduped + inbox-only; no recursion storm.

## Cumulative

- Commits (local, branch `owner-os/control-plane-v2`): architecture `d0b72ab`; P0 `5822bac`;
  P1 `e713433`; progress `1afdf8b`; recovery fix `918db00`; provenance `6d56cc7`; addendum
  `2f01fd8`; P2 `a4cebdd`; P3 (this).
- Full suite: **874 passed** (control-plane P0–P3 + discovery/CTO/delivery/provenance/
  actuator/notifier + recovery).
- Deployed: shadow engine (observe-only) + notifier drain live; **actuator DISABLED**;
  legacy controllers untouched.
- Rollback: `CONTROL_PLANE_ENABLED=0` (stop shadow) / `CONTROL_PLANE_ACTUATOR_ENABLED` stays
  unset; drop `control_plane.db`; `git revert` the phase commits. Legacy DBs backed up.

## Genuine owner gates (unchanged, blocking only cutover)

G1 P4 actuation cutover on live agents · G3 push/PR/publication · G4 Telegram/credential
channel config · G5 same-chat proactive wake trigger. P2/P3 shipped without hitting any
(actuator proven via synthetic canary; no live pane actuated).

---

# ADDENDUM 2026-08-03 (3) — P4 PREPARATION (dormant) + blocker resolution (`b55a560`)

Preparation only — NO cutover, NO legacy actuation disabled, all live flags OFF.

- **Watchdog → Actuator routing (dormant):** new `CONTINUATION_VIA_ACTUATOR` flag (default
  OFF) + `deliver_via_actuator` bridge that acquires the agent lease and routes delivery
  through the canonical lease-gated Actuator. With the actuator disabled (or not our lease)
  routing is a safe no-op (delivers nothing); flag-off keeps the legacy inline path
  unchanged. Both `CONTINUATION_VIA_ACTUATOR` and `CONTROL_PLANE_ACTUATOR_ENABLED` verified
  default OFF.
- **Blocker-resolution events:** `resolutions.resolve_blocker` + `api.close_gates` close
  SYSTEM blocker gates (actuation_failed/continuation/…) and emit a correlated
  `blocker_resolved` event (resolves link, inbox-only) — the all-clear. NEVER closes an
  owner-DECISION gate (scope/business/unverified_owner_decision), which still requires a
  verified owner_decision. Wired into the actuator: an action blocked earlier that later
  verifies clears its blocker.
- **Tests (+5):** bridge routes+verifies under a watchdog lease; routing no-op when actuator
  disabled; run_once route-on + actuator-off delivers nothing; blocked→verified emits
  blocker_resolved and closes the system gate but preserves the owner-decision gate;
  resolve_blocker never closes an owner-decision gate. **Full suite: 879 passed.**
- Deployed dormant (code loaded; both flags OFF → behavior identical to before). Legacy
  continuation watchdog still runs its proven inline path live.

## STOP — owner gate G1

P0→P3 shipped + P4 wiring is READY and dormant. The next step, **P4 cutover** (turning on
`CONTROL_PLANE_ACTUATOR_ENABLED` + `CONTINUATION_VIA_ACTUATOR` on live agents and disabling
the legacy inline actuation path), changes how real agents are commanded and is **owner-gated
(G1)**. Stopping here per instruction. To proceed later: enable both flags for one canary
agent, watch the CTO inbox for `action_verified` / `blocker_resolved`, then disable the
legacy path once green.
