# Delivery/reconciliation blind spots — agent recovery + logged cleanup — 2026-07-28

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive registry cleanup; no ChatGPT async delivery claimed.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `2f2ba71..e26dbfb`
  - `e26dbfb` fix(alerts): resolve agent blocker alerts on recovery + clear logged alerts

## Audit findings (blind spots) + fixes
1. **`resolve_current` only cleared `status='sent'`** — an alert delivered to a LOCAL
   sink (`status='logged'`) was never resolvable, so it lingered inconsistently. Now
   clears `sent` OR `logged` (honest cleanup), consistent with `reconcile_worker_alerts`.
2. **No agent-level recovery cleanup** — worker + monitor recoveries resolved their
   alerts, but an agent's blocker alerts (`agent_externally_blocked`,
   `agent_owner_decision`, `agent_process_failed`) stayed CURRENT forever after the
   agent recovered. `resolve_agent_alerts_on_recovery` (called from
   `process_new_events`) now resolves an agent's open blockers when an
   `agent_recovered` / `agent_completed` event lands — EXACT-keyed by `subject_key`
   (`agent:<id>`) so another agent's same-type alert is never touched. Symmetric to
   worker/monitor recovery.

## Restart / dup-suppression audit (verified already-safe, no change needed)
- `process_new_events` advances the durable `NotificationState.last_event_id` cursor
  (survives restart) → events are not reprocessed.
- `_dispatch` idempotency: a prior successful (`sent`) delivery of the same
  (event, transport, destination) suppresses a resend even after a restart/replay.
- `agent_notifier` commander delivery uses the durable `commander_delivered`
  (event_id + fingerprint) ledger → no double delivery across restart.
- Bidirectional heartbeat freshness stands: notification-worker watches the
  health-monitor sweep (`health.monitor_stalled`); health-monitor watches the
  `notification`/`agent_notifier` heartbeats (`health.worker_down`).

## Live verification (configured owner channel)
- Injected `agent_externally_blocked` for two agents (delivered), then an
  `agent_recovered` for one → `process_new_events`: blocker resolution
  `{agent:_recovA:0.0: resolved, agent:_recovB:0.0: sent}` — exact-keyed, no false
  close. Test-canary rows then non-destructively resolved (0 open left).

## Tests
- `test_notifications`: logged-alert resolution; agent recovery resolves only that
  agent's blockers; a non-recovery event is a no-op. Focused batch
  (notifications/health/cto_snapshot/mission_control/briefing/daily_brief/
  agent_notifier) **113 passed**.

## Mission control / daily brief evidence
- `cto_snapshot.notifications` current alerts already exclude resolved/logged; agent
  recovery now drops the agent's blocker from current, so `warning_critical` /
  `current_alerts` stay honest. `mission_control.workers.sweep` continues to expose
  `stalled` + `age_seconds`.

## Rollback
- `cd /opt/seo && git revert e26dbfb && docker compose build backend && docker compose up -d backend`.
  Resolved rows keep their audit (external_ref / delivery proof).

## Next safe reliability defect (continuing)
- Orphaned-agent blocker cleanup: when the runtime reaper marks an agent VANISHED (no
  recovery event will ever come), resolve that agent's still-open blocker alerts by
  subject_key — the direct symmetric gap to worker-orphan reconciliation.
