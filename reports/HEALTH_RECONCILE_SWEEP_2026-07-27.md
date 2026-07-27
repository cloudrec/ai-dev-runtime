# Health reconcile stale worker alerts + persist sweep counts — 2026-07-27

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `2636deb..772e457`
  - `772e457` fix(health): reconcile stale worker alerts + persist monitor sweep counts

## Defect — 3 current health.worker_down while health is all-green
Root cause:
- A worker that went down then up BETWEEN two monitor passes never recorded a
  dead/stale liveness, so the recovery transition (and its resolve) never fired — the
  `worker_down` stayed open.
- Local-`logged` alerts (rule #1 → local no-op, never delivered) were counted as
  current warning/critical.
- Orphaned test-canary worker_down (worker row deleted) had no path to close.

## Fix
- `notifications.reconcile_worker_alerts` — EXACT-keyed, idempotent: resolves
  `worker_down`/`worker_stale` for workers that are now ALIVE or ORPHANED (no
  heartbeat), touching only open (`sent`/`logged`) rows; a genuinely-down worker is
  never closed. Runs every `check_and_alert` pass, so it self-heals the
  down→up-between-passes case.
- `cto_snapshot._notifications` current = OPEN + owner-facing only: excludes
  `resolved` and non-delivered statuses (`logged`/`suppressed`/`throttled`).
- Persist `monitor_state` (`last_full_sweep_at`, `expected_workers`, `seen_workers`)
  each sweep (survives restart). The `health.worker_*` digest now reads
  "K of M workers" — distinguishing a partial monitor pass from a genuine mass event;
  also surfaced in `mission_control.workers.sweep`.

## Live verification
- After deploy + one `check_and_alert`: **current worker_down = 0**,
  `warning_critical = 0`, `resolved = 7`; real workers **10/10 alive**, probes 4/4.
- Orphan `_ck_b` down → `resolved: worker no longer monitored (orphaned)`; alive `_ck_a`
  → resolved (recovered/orphaned). Only 2 legacy rows (2026-07-13, NULL subject_key,
  body has no worker → unkeyable) remain — OUT of the current window, not current.
- `monitor_state` = `{last_full_sweep_at, expected_workers:10, seen_workers:10}`.
- No duplicate Telegram: reconcile is a status UPDATE, emits nothing.

## Tests
- `test_notifications`: reconcile resolves alive + orphan but NOT a genuinely-down
  worker (exact-keyed); idempotent (second pass resolves 0). Plus subject_key +
  digest + burst regression. Focused batch (notifications/health/cto_snapshot/
  mission_control/briefing/daily_brief/agent_notifier) **99 passed**.

## Rollback
- `cd /opt/seo && git revert 772e457 && docker compose build backend && docker compose up -d backend`.
  `monitor_state` table is additive/harmless; resolved rows keep their audit.

## Next safe defect — DONE (`8a769ec`)
`backfill_subject_keys` now falls back to the linked OwnerEvent payload (join on
event_id) for legacy rows whose body has no worker; `reconcile_worker_alerts` also
resolves OLD (>24h) NULL-subject worker_down/stale as un-attributable legacy. **Live:**
OPEN worker_down/stale = **0** — no worker alert can be stuck un-reconcilable. Recent
NULL-subject rows are left (may still be live). Test added.

## Next after that (queued)
- Emit ONE deduped owner event when a reconciliation actually resolves stale alerts
  (currently silent) — so the owner sees "3 stale worker alerts auto-cleared", closing
  the loop between a silent self-heal and the alert surface.
