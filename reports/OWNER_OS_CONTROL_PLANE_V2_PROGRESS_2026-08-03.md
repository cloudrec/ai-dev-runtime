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
