# Pre-delivery revalidation for condition events — 2026-07-28

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive registry ops; no external credentials; no ChatGPT async delivery claimed.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `acd3e91..9a6e1b8`
  - `9a6e1b8` fix(notifier): pre-delivery revalidation for blocker/waiting/stall/test-killed events

## What changed — the last documented blind spot
Completion events were already revalidated against current pane/state before delivery
(suppressed if the agent was active again). Extended the SAME revalidation to condition
events — `agent_externally_blocked`, `agent_owner_decision`, `agent_waiting_input`,
`agent_unexpected_idle`, `agent_process_failed`, `agent_recovery_failure`:
- **emit→recover→deliver race**: if the agent is ACTIVE again at delivery time
  (state working/shell_running, or an active-execution marker in the tail) → the event
  is stale → SUPPRESS + retract (`commander.suppressed_stale`) and mark delivered so it
  can never be sent late.
- **bounded freshness window** (`COMMANDER_EVENT_FRESHNESS_SECS`, default 600): an OLD
  event whose claimed `to_state` no longer matches the current state, or whose agent is
  GONE, is **fail-closed** suppressed.
- **exact subject-key**: revalidation uses `orch[agent]` (the current record) + the
  event's `to_state` evidence.
- **restart-safe**: `orch` is re-fetched fresh each poll; the durable
  `commander_delivered` (event_id + fingerprint) ledger prevents double delivery.
- **no Telegram spam**: `commander.suppressed_stale` is now internal-audit-only (never
  dispatched to Telegram) — a suppressed, never-seen alert makes no owner noise.

## Live verification (configured owner channel)
- STALE blocker (agent working again at delivery) → **suppressed**
  ("agent active again — agent_externally_blocked no longer holds"), NOT delivered to
  Telegram.
- TRUE blocker (agent still blocked) → delivered **telegram / sent**.
- Test-canary rows non-destructively resolved afterward.

## Tests
- `test_agent_notifier`: still-valid blocker delivers; agent-active-again suppresses
  (incl. tail marker); test-killed while running suppresses; old+diverged and
  old+agent-gone fail-closed; a FRESH diverged event is NOT aged out; suppressed_stale
  is internal-only. Focused batch (agent_notifier/notifications/health/cto_snapshot/
  mission_control) **97 passed**.

## Health
- Backend rebuilt healthy; `ai-runtime` active, orchestrator `GOAL_COMPLETE_WAITING_EXTERNAL`.
- Current alerts remain honest: a suppressed stale event is never counted current;
  true blockers still deliver + show current until recovery/vanish resolves them.

## Rollback
- `cd /opt/seo && git revert 9a6e1b8 && docker compose build backend && docker compose up -d backend`.
  Reverts to completion-only revalidation; resolved rows keep audit.

## Next safe watcher/notification reliability defect (continuing)
- Symmetric emit→recover race for the RUNTIME watcher itself: when the orchestrator
  sweep detects an agent recovered, retract any not-yet-delivered stale commander
  event at the SOURCE (runtime commander_events) too, so the queue never holds a
  contradicted event even before the notifier's pre-delivery check.
