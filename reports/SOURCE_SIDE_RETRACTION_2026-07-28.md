# Source-side retraction of stale commander events — 2026-07-28

Continuation (defense-in-depth). Internal only. No ACAP/Mess agent touched or
duplicated; no destructive registry ops; no external credentials; no ChatGPT async
delivery claimed.

## Commits (deployed)
- **ai-runtime** (`main`): `3bbd1a7..6459202`
  - `6459202` feat(commander): source-side retraction of stale condition events on recovery
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `9a6e1b8..3dd06e7`
  - `3dd06e7` chore(notifier): commander_event_retracted lineage is internal-audit-only

## What changed
`agent_control.retract_stale_condition_events(agent, state)` — when the orchestrator
sweep sees an agent ACTIVE/COMPLETED again, it retracts that agent's still-UNACKED
condition events (`agent_externally_blocked`, `agent_owner_decision`,
`agent_waiting_input`, `agent_unexpected_idle`, `agent_process_failed`,
`agent_recovery_failure`) at the SOURCE — acking them so the notifier never fetches
(nor delivers) a contradicted alert. Wired into `refresh_and_resolve`.
- **stable subject_key**: lineage marker carries `agent:<id>`.
- **exact event lineage**: each retraction records `commander_event_retracted` with the
  exact `retracted_event_id` + type.
- **race-safe** emit→recover→retract: `UPDATE … SET acknowledged=1 WHERE id=? AND
  acknowledged=0` — only rows still unacked flip (safe against the notifier's own ack).
- **restart-safe idempotency**: acked state persists; the lineage marker's dedup_key
  `retract-src:<id>` guarantees exactly one marker; a re-run returns `[]`.
- **second barrier intact**: the notifier's pre-delivery revalidation (`9a6e1b8`) still
  suppresses any contradicted event that slipped through before the source ack.
- **no false/dup Telegram**: retracted events are acked → never fetched; the
  `commander_event_retracted` marker is internal-audit-only (no Telegram).
- **no-op unless active/completed**: a still-blocked agent keeps its alert.

## Live verification
- Emitted a stale `agent_externally_blocked` + `agent_process_failed` for a synthetic
  agent → `retract_stale_condition_events(agent, "working")` acked both (ids returned),
  leaving only `commander_event_retracted` markers (subject_key `agent:…`, exact
  `retracted_event_id`); second call → `[]` (idempotent).
- Owner OS: a `commander_event_retracted` OwnerEvent → `process_new_events` produced
  **NO** notification (internal-only, no Telegram).
- `ai-runtime` active; backend healthy.

## Tests
- Runtime `test_commander_retraction`: acks condition events + records lineage; no-op
  when not active; idempotent/restart-safe (one marker); exact-agent-only. Full runtime
  suite **773 passed**.
- Owner OS `test_agent_notifier` + `test_notifications` **66 passed**.

## Rollback
- Runtime: `git revert 6459202 && systemctl restart ai-runtime.service`.
- Owner OS: `git revert 3dd06e7 && docker compose build backend && docker compose up -d backend`.
- Retraction only acks/records (never deletes); reverting stops future source acks — the
  notifier pre-delivery revalidation still catches stale events.

## Remaining blind-spot scan (notification/orchestration)
Delivery pipeline now has THREE barriers against a stale alert: (1) source-side
retraction on recovery, (2) notifier pre-delivery revalidation, (3) post-delivery
resolution/reconciliation on recovery/orphan/vanish. Plus bidirectional stall watchdog,
exact-keyed resolution, restart-safe dedup, honest current-alert cleanup, no-spam
digests. No remaining open blind spot found; further items are marginal defense-in-depth
(e.g. surface the retraction/suppression counts in mission-control evidence for
observability) — no external/credential/destructive gate touched.

## Next safe internal item (queued)
- Observability: expose per-sweep `retracted`/`suppressed` counts in
  `mission_control.workers` (or a commander-health section) so the owner can see the
  barriers are firing — read-only, no new delivery.
