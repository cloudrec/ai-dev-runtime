# Owner OS handoff — 2026-09-18 (restart gate pending)

Provenance: written on an automated instruction; no owner sign-off is recorded here.

## State
- Branch `ai-runtime/220-windows-bridge`, HEAD **2bf04dd** (← 3ec0a66 ← f713ac2). Both new
  commits local only. Tracked tree clean; 34 untracked `reports/*.md` untouched.
- Full suite at HEAD: **3350 passed**.
- Live: `ai-runtime` pid 6557 (22:51:21 CEST 09-17, code f713ac2); companion pid 3508
  (22:51:10 CEST 09-17). Only live Claude on this cwd: `owner-os-opus-clean:0.0`.

## What the two commits change at runtime
- 3ec0a66 — `ack_commander_events` audits `commander_events_acked` (ids + flipped
  `id/agent/event_type/ts`), returns rows flipped. Lives in ai-runtime.
- 2bf04dd — `is_supervised(target)` under `*` with no project: unregistered target →
  False (`*` = registered targets only). Lives in the companion; behaviourally inert there
  (no bare caller), closes the public-predicate hole that answered True for the self session.
- Test-only: conftest isolates `AGENT_CONTROL_AUDIT`, `CONTINUATION_WATCHDOG_SESSIONS`,
  `NATIVE_SUPERVISOR_*`; canary test judges the tracked tree.

## The gate (owner-typed line required)
`systemctl restart ai-runtime` — then the five proofs in
`OWNER_OS_RESTART_READINESS_2026-09-18.md` (new pid; `commander_events_acked` rows appear
with populated `events[]`; self still `denied: self_project`; notifications status
consistent; no self `agent_process_failed`). Companion restart optional.

## Other gates, unchanged
- Push of 3ec0a66 + 2bf04dd (pane draft "запушь последний коммит" is not authorization).
- `configs/.env` `NATIVE_CANARY_TARGET=hostsecure:0.0` since 09-11, provenance unknown.
- Dedicated Owner OS Telegram bot (BotFather).

## Known, not defects
- `agent.commander.agent_process_failed` ×3 @ 21:34:55Z = six distinct store rows
  7235–7240 re-stamped by the consumer (`OWNER_OS_TRIPLE_PROCESS_FAILED_21_34_55_2026-09-18.md`).
- 12 `job:0.0` fixture rows in the live `agent_control.jsonl` (09-18, suite pollution,
  now prevented) — ignore when reading that log.
- Event 54494 (`owner-os-opus-final` death) = orchestrator rotation; `live_in_same_cwd: []`
  was correct at scan time.
