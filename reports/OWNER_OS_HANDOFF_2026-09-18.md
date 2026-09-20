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

## Update 2026-09-20 (automated instructions; no owner sign-off recorded)

HEAD **76c8fa3**, branch ahead **33** of origin, tracked tree clean, 34 untracked reports
untouched. Full suite **3361 passed**. Live processes unchanged: ai-runtime pid 6557 and
companion pid 3508, both from 22:51 CEST 09-17 (code f713ac2).

New local commits since 217af2f:
- efae654 — `is_supervised` reads the denylist over `project_candidates` (project + every
  cwd segment); both scan paths pass cwd. Root cause of events 55184/55938: 26 continuations
  typed into the parked, deny-listed `diamond-auction:0.0`. Companion loads it.
- 73c2ed1 — predicate fails closed if the denylist check raises; read-only proof tool
  `tools/verify_denied_target_not_continued.py` (exit 0 = first post-restart decision is
  `skip/value_bearing_send_blocked`, no `continue`).
- 76c8fa3 — `commander_delivery_report` no longer calls 394 orphaned (pre-window, never
  listable) rows a stalled drain; health summary loses a false `same_chat_drain_stalled`.
  ai-runtime loads it.

Reports: `OWNER_OS_EVENT_55938_AUCTION_CONTINUED_UNDER_WILDCARD_2026-09-20.md`,
`OWNER_OS_CERTBOT_WALL_AND_DRAIN_FALSE_RED_2026-09-20.md`.

Gates, unchanged in kind: (1) `systemctl restart owner-os-wake-companion`, then run the
proof tool; (2) `systemctl restart ai-runtime`; (3) `git push` (33 commits); (4) certbot
renewal conf for payment.clients.help (28 days to expiry) and the expired
youtube.seoseo.cheap cert — other projects. Until (1), diamond-auction is protected only by
the 6 h gate from 55938 (≈21:43Z 09-20); after it clears, pre-fix code resumes on the next
idle pass.
