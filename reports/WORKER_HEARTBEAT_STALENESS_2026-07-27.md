# Worker-heartbeat staleness alerts + process_failed investigation — 2026-07-27

Continuation. Internal only. No running ACAP/Mess process touched; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `648e8cf..abe877d`
  - `abe877d` feat(health): worker-heartbeat staleness + deduped stale/recovered/down alerts

## Task 1 — heartbeat staleness surfaced + owner-visible alerts — DONE, live
- `health.EXPECTED` now includes `agent_notifier` (both delivery-pipeline workers
  required: agent_notifier pulls commander events → OwnerEvents, notification
  dispatches → Telegram).
- `health.check_and_alert` emits ONE owner event per liveness CHANGE:
  `health.worker_stale`, `health.worker_recovered`, `health.worker_down`. The stored
  `liveness` field is the dedup memory → a persistently-stale worker alerts once, not
  every tick. Thresholds are explicit (`stale_after_s = interval*2+15`,
  `dead_after_s = interval*4+60`).
- New rule **"Worker health → owner" `["health.worker_*"] → telegram`** makes these
  owner-visible (were local-only; the pattern needs the `*` suffix to prefix-match).
- Surfaced on **mission control** (`overview().workers`: per-worker liveness +
  thresholds + `delivery_ok`) and the **daily brief** (REAL BLOCKERS line
  "⚠ delivery pipeline stale/down …").
- **Live:** a throwaway `_canary_hb` heartbeat aged to stale → `health.worker_stale`
  → **telegram/sent**; a real worker recovery → `health.worker_recovered` →
  **telegram/sent**; canary row removed. Real workers untouched. mission control
  shows `agent_notifier:alive, notification:alive, delivery_ok=True`.

## Task 2 — agent.commander.agent_process_failed @ 2026-07-27T15:35:04Z — RESOLVED (stale test artifact)
Verdict: **not a real process failure**. Evidence:
- OwnerEvent 8665 (agent `job:0`) payload evidence = generic `"agent_process_failed
  evidence"`, dedup_key `agent_process_failed:job:0` — the exact synthetic strings
  from the earlier digest-verification injection.
- ZERO `agent_process_failed` rows in the runtime `commander_events` — it never
  originated from a real runtime transition.
- The `job` runtime record's last state is `idle` (not dead); job had no unfinished
  orchestrator task (so the dead-agent watcher correctly never fired). The tmux
  session is merely absent now, with no unfinished work — not a crash the watcher
  detected.
Action: emitted `agent.commander.agent_process_failed_resolved` (evidence-bearing) →
**telegram/sent**. No agent recreated, nothing destructive.

## Tests
- `test_health` + `test_daily_brief` + `test_mission_control` + `test_notifications`
  **50 passed** — incl. state-change-only dedup (stale once), recovered, down; the
  agent_notifier worker registered.

## Rollback
- `cd /opt/seo && git revert abe877d && docker compose build backend && docker compose up -d backend`.
- Rule #13 pattern was also corrected in the DB (`["health.worker_"]`→`["health.worker_*"]`);
  to revert: `UPDATE notification_rules SET event_patterns='["health.worker_"]' WHERE id=13;`
  (or delete rule 13 and let seed recreate it from code).

## Next safe watcher/orchestration defect (continuing)
- The `job` tmux session is absent with a stale `idle` runtime record. Add a
  reaper/verification so a supervised session that vanishes (pane gone, not just
  dead) is reconciled — its record marked ended and, if it had approved unfinished
  work, one deduped owner event — without recreating the agent.
