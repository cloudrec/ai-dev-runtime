# Transition events wired to Telegram + direct-agent card + archive verify — 2026-07-27

Continuation. Internal only. No running ACAP/Mess agent touched or duplicated; no
project data deleted.

## Commit (deployed)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `ff21391..8e33964`
  - `8e33964` fix(notifier): evidence-based delivery fingerprint for transition events
  - (portfolio direct-agent surfacing `ff21391` earlier this pass)
- Runtime transition events + direct-active truth already shipped (`a7c43e2`).

## Delivered
### Transition events → actual Telegram notifier, evidence dedupe, honest status — DONE, live
- The runtime emits transition events (`agent_completed`, `agent_waiting_input`,
  `agent_externally_blocked` / `agent_owner_decision` genuine blocker,
  `agent_process_failed` = test/process killed, `agent_unexpected_idle` stall,
  `agent_recovered`) into `commander_events`, deduped by evidence_hash.
- `agent_notifier` turns each into an `OwnerEvent` `agent.commander.<type>`;
  `notifications.process_new_events` matches **rule #10 `["agent."] → telegram`** and
  sends via `notifications._send`, recording the REAL status on the Notification row.
- `_event_fingerprint` now folds in the runtime `dedup_key`/`evidence_hash` +
  `from_state`/`to_state`, so a re-stall or a new blocker delivers again while
  identical rows collapse — evidence-based dedupe end to end.
- **Live:** `process_new_events` dispatched real transitions →
  `agent.commander.agent_unexpected_idle` and `…agent_completed` recorded
  **channel_kind=telegram, status=sent**. Canary delivered (`message_id=120`,
  `error=None`). Local sink honestly returns `logged` (never fake `sent`). Channel =
  configured Telegram owner surface — NOT a claimed ChatGPT async push.

### direct_agents card (ACAP / Mess as active work) — DONE
- Portfolio `mission_control.overview()` exposes `queues.direct_agents_active` +
  a `direct_agents` list (pulled from the runtime `direct_active_agents`).
- Daily brief shows them under INTERNAL WORK IN PROGRESS. So ACAP/Capacity and Mess
  appear as active work even with runtime jobs=0 — without creating agents or plan
  duplicates (reporting-only; never dispatched to).

### Registry archive still holds — VERIFIED
- `panel`, `shop`, `youtube` → `status=archived, exclude_from_auto=t`; 0 active
  standalone `youtube` projects. Rows still present (data intact). YouTube remains a
  Content Factory module only.

## Tests / deploy
- `test_agent_notifier` **26 passed** (added evidence-fingerprint collapse / distinct
  cases). Backend rebuilt + healthy.

## Rollback
- `cd /opt/seo && git revert 8e33964 && docker compose build backend && docker compose up -d backend`
  — reverts only the fingerprint granularity; transitions still deliver.
- Registry un-archive (if ever needed): `set_controls(status='discovered',
  exclude_from_auto=False)` for the three slugs — data was never deleted.

## Next safe watcher defect (continuing)
- Throttle/aggregate high-frequency transition storms (many `agent_unexpected_idle`
  in one poll) into a single owner digest, so a flapping agent cannot spam Telegram —
  extend rule `throttle_seconds` / an aggregate path for `agent.commander.*`.
