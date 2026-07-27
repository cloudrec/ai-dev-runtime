# One deduped owner event on reconciliation clear — 2026-07-27

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `8a769ec..d8887aa`
  - `d8887aa` feat(health): emit one deduped owner event when reconciliation clears stale alerts

## What changed
`reconcile_worker_alerts` collects the cleared subject_keys (SQL `RETURNING`) per
reason (alive / orphaned / legacy). When a sweep ACTUALLY clears ≥1 alert it emits
exactly ONE owner event `health.worker_reconciliation_cleared`:
- stable `subject_key = "reconcile:worker-alerts"`,
- `cleared_count`, `subjects[]`, and `by_reason` evidence.
A zero-clear sweep emits nothing → idempotent repeats never spam Telegram. The event
delivers via rule #13 (`health.worker_*` → telegram) and is EXCLUDED from the
worker-flap digest (`_HEALTH_FLAPS` = down/stale/recovered only), so it lands as its
own message.

## Live verification (configured owner channel)
- Injected an orphan `worker_down` (`worker:_orphanX`, no heartbeat).
- sweep 1: `reconciled=1` → ONE `health.worker_reconciliation_cleared` →
  **telegram / sent**, `subject_key=reconcile:worker-alerts`.
- sweep 2: `reconciled=0` → NO new event. Total reconcile OwnerEvents = **1** (no
  Telegram spam on the idempotent repeat).

## Tests
- `test_notifications` (reconcile group): zero-clear/no-event, multi-clear single
  event (count + subjects + stable subject_key), repeat-sweep dedup, recovery/reopen
  (a re-downed worker clears again → a new event), legacy NULL-subject old-only,
  worker identity change (renamed→orphan cleared, new alive cleared, unrelated
  genuinely-down untouched).
- Focused batch (notifications/health/cto_snapshot/mission_control/briefing/
  daily_brief/agent_notifier) **103 passed**.

## Rollback
- `cd /opt/seo && git revert d8887aa && docker compose build backend && docker compose up -d backend`.
  Reconcile still clears (returns count); only the summary event is dropped. Resolved
  rows keep their audit.

## Next safe defect — DONE (`7c3ef7d`)
`_fmt` renders `health.worker_reconciliation_cleared` human-friendly. **Live:**
"🧹 Auto-cleared 1 stale worker alert(s)" / "worker:_orphanY (1 orphaned) — No owner
action needed" → telegram/sent. Test added.

## Next after that (queued)
- `mission_control.workers.sweep` exposes last_full_sweep + expected/seen; add a
  staleness threshold on the sweep itself (health_monitor loop not running → no sweep
  in N minutes) and surface it as a single owner alert, so a dead monitor loop (which
  would silence all worker alerting) is itself detectable.
