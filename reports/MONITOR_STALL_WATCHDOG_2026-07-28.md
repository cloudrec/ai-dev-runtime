# Stalled health_monitor watchdog — 2026-07-28

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `7c3ef7d..2f2ba71`
  - `2f2ba71` feat(health): detect a stalled health_monitor loop (independent watchdog)

## What changed
The notification worker (a SEPARATE loop) runs `check_monitor_liveness` each tick —
an INDEPENDENT watchdog, so a dead monitor is itself alerted:
- reads the durable `monitor_state.last_full_sweep_at`; if no full sweep for
  `MONITOR_STALE_MINUTES` (default 10) → ONE deduped owner event
  `health.monitor_stalled` with exact `subject_key=monitor:health_monitor` and
  evidence (age / last-sweep / threshold);
- when sweeps resume → `health.monitor_recovered` + auto-resolves the stalled alert
  (`resolve_current`, exact-keyed).
- `monitor_state.stalled` flag is the dedup memory (survives restart → no storms).
- A grace window after checker start + a future-timestamp guard absorb restart and
  clock skew. `health_monitor` sweeps immediately on start, so a restart refreshes the
  row within one tick.
- Status exposed via `monitor_sweep_state` (`stalled`, `age_seconds`) →
  `mission_control.workers.sweep`.
- Rule "Worker health → owner" extended to `health.monitor_*` → telegram; `_fmt`
  renders both events human-friendly. The additive `stalled` column runs in a
  SAVEPOINT so a duplicate-column error never poisons the transaction.

## Live verification (configured owner channel)
- Forced stale `last_full_sweep_at` (25 min, past grace) → `check_monitor_liveness`
  → `health.monitor_stalled` → **telegram / sent**
  ("🚨 Health monitor STALLED — worker alerting is blind"), subject_key
  `monitor:health_monitor`.
- Sweeps resumed → `health.monitor_recovered` → **telegram / sent**
  ("✅ Health monitor recovered"); the prior stalled notification → **resolved**;
  `monitor_state.stalled=False` (clean state).

## Tests
- `test_health`: healthy/no-event, single alert + stable subject_key, repeat dedup,
  recovery, restart grace (stale DB row absorbed), never-swept NULL row alerts,
  clock-skew-future is fresh. Focused batch (health/notifications/cto_snapshot/
  mission_control/briefing/daily_brief/agent_notifier) **110 passed**.

## Rollback
- `cd /opt/seo && git revert 2f2ba71 && docker compose build backend && docker compose up -d backend`.
- Rule #13 pattern was also updated in the DB (add `health.monitor_*`); to revert:
  `UPDATE notification_rules SET event_patterns='["health.worker_*"]' WHERE id=13;`.
- `monitor_state.stalled` column is additive/harmless; resolved rows keep audit.

## Next safe notification/orchestration reliability defect (continuing)
- Cross-watch: `health_monitor` should also detect a stalled `notification_worker`
  (the two watchdogs currently only cover one direction) — persist a notification-worker
  sweep marker and alert if IT stops, so a dead delivery loop is caught by the monitor.
