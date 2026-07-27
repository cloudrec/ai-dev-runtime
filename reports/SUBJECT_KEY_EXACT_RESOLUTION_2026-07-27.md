# Exact-keyed alert resolution via stable subject_key — 2026-07-27

Continuation. Internal only. No ACAP/Mess agent touched or duplicated; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `8a81b36..c9ff1dc`
  - `c9ff1dc` feat(alerts): exact-keyed resolution via stable subject_key
- Runtime: no change this pass (subject_key is an Owner-OS notification concern).

## What changed
- `Notification.subject_key` column (additive). **Migration-safe:** `ALTER TABLE …
  ADD COLUMN IF NOT EXISTS` + an idempotent `backfill_subject_keys` run at startup
  (only NULL rows, derived from the body; underivable rows stay NULL).
- Emitters carry an explicit `subject_key` in the OwnerEvent payload —
  `worker:<name>` (health) / `agent:<id>` (commander). `_subject_key` derives it
  (incl. nested commander payload) when absent, so old events still key correctly.
- `_dispatch` stamps `subject_key` on the delivered Notification (OwnerEvent →
  Notification threading).
- `resolve_current` is now **EXACT-keyed** on `subject_key` (the old body/substring
  match remains only as an explicit legacy fallback). Worker recovery resolves by
  `subject_key='worker:<name>'`.

## Correctness — no false close
Two same-type alerts (`health.worker_down`) with different `subject_key` resolve
independently. **Live-verified:** `_ck_a` + `_ck_b` both down → recover only `_ck_a`
→ `worker:_ck_a`=resolved, `worker:_ck_b`=**sent** (untouched). Dedup + the commander
burst digest are unchanged (digest spans multiple agents → no single subject_key).

## Tests
- `test_notifications` incl.: subject_key derivation (worker/agent/nested/explicit/
  none), body backfill, and the integration case — two worker_down rows, different
  subject_key, resolving one leaves the other `sent`.
- Focused batch (`notifications`, `health`, `cto_snapshot`, `agent_notifier`,
  `mission_control`, `briefing`, `daily_brief`) **95 passed**. Reaper (vanished tmux)
  + restart persistence: subject_key is a persisted DB column; the reaper's
  vanished-session tests remain green from `9b8a837`.

## Live verification
- Startup: `subject_key` column present; backfill filled 11/182 legacy rows
  (idempotent — reruns fill nothing new).
- Fresh commander notification carried `subject_key=agent:seo-audit:0.0`, `sent`.
- Canary → `status=sent, message_id=154`. mission-control workers `delivery_ok`;
  notification health nominal.

## Rollback
- `cd /opt/seo && git revert c9ff1dc && docker compose build backend && docker compose up -d backend`.
  The `subject_key` column is additive and harmless if unused; resolved rows stay
  resolved (audit kept). No data lost.

## Next safe notification/orchestration defect (continuing)
- Aggregate `health.worker_*` bursts (a batch of workers going stale/recovering in
  one monitor pass) into a single owner digest — same treatment the commander events
  already get — so a mass restart cannot fan out N separate Telegram messages.
