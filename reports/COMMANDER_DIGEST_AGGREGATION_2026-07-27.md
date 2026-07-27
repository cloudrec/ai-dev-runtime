# Commander burst → one owner digest — 2026-07-27

Continuation. Internal only. No running ACAP/Mess agent touched or duplicated; no
destructive/external-credential actions.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `8e33964..b9fe872`
  - `b9fe872` feat(notifier): aggregate agent.commander.* burst into one owner digest

## What changed
`notifications.process_new_events` now, per poll:
- Collapses the NON-critical `agent.commander.*` burst (completed, waiting_input,
  unexpected_idle, recovered) into ONE digest per matching rule
  (`_dispatch_aggregate_commander`), deduping identical rows by evidence identity
  `(event_type, agent, dedup_key/evidence_hash)` and keeping each distinct event's
  line + evidence snippet + a per-type count in the title.
- Keeps CRITICAL events OUT of the digest — `agent_process_failed` (test/process
  killed), `agent_externally_blocked`, `agent_owner_decision` still deliver
  individually so they are never buried.
- Records honest per-Notification send status (telegram `sent` / local `logged`);
  rule-level dedup (advisory lock + dedup_key) and throttle still apply.

## Tests
- `test_notifications` + `test_agent_notifier` **43 passed**, incl:
  - mixed completed/waiting/idle burst → one digest, evidence preserved, formatting;
  - duplicate `unexpected_idle` rows collapse to one line;
  - mixed burst keeps `process_failed`(test-killed) + `externally_blocked` separate;
  - critical-commander classification.

## Live verification
- Injected a mixed burst (completed, waiting_input, idle×2, process_failed) →
  `process_new_events`:
  - `agent.commander.digest` → **telegram / sent** — `🔔 3 agent events
    (completed×1, unexpected_idle×1, waiting_input×1)` (the duplicate idle deduped).
  - `agent.commander.agent_process_failed` → **telegram / sent** individually, plus
    `local / logged` (honest, never fake `sent`).
- Canary via `notifications.canary()` → `status=sent, message_id=127`. Channel =
  configured Telegram owner surface (not a ChatGPT async push).

## Deploy / rollback
- Backend rebuilt + healthy.
- Rollback: `cd /opt/seo && git revert b9fe872 && docker compose build backend &&
  docker compose up -d backend` — reverts only the aggregate path; individual
  delivery (prior `8e33964`) still works.

## Next safe watcher/orchestration defect (continuing)
- Verify the notification worker loop actually invokes `poll_once` +
  `process_new_events` on a live cadence (delivery only reaches Telegram if the loop
  runs); add a heartbeat/last-run timestamp to the status surface so a stalled
  notifier worker is itself detectable.
