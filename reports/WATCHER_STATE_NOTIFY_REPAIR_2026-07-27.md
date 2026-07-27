# Watcher state-detection + notification-delivery repair — 2026-07-27

Owner-directed. Internal only. No running agents touched, no processes duplicated,
no destructive cleanup. Verified while ACAP regression runs (no heavy load tests).

## Commits (deployed)
- **ai-runtime** (`main`): `bef95a2..d631715`
  - `d631715` fix(state): shell_running + waiting_input states, narrow false externally_blocked
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `4e7bd65..f8f6093`
  - `f8f6093` fix(notifications): local sink reports honest 'logged', never fake 'sent'

## Done this pass (of the owner's 7 items)
### (1) Correct state detection for ALL tmux Claude agents — FIXED + live-verified
- `classify_state` gains `shell_running` (live non-Claude foreground command = work,
  not idle/blocked) and `waiting_input` (a real, non-ghost `❯` line typed/pasted but
  not submitted — an idle agent with a staged command is no longer lost).
- `_STATE_EXTERNAL_RE` narrowed to strong agent-level block phrases and matched only
  against the CURRENT status (last ~500 chars). Benign shell output ("timed out",
  "network error", "502/503") no longer mis-classifies a capacity agent running a
  live shell as `externally_blocked`.
- New signals default off + computed only for at-rest agents → running/active agents
  are unaffected (no extra capture, no reclassification).
- **Live (post-deploy):** 9 agents — `externally_blocked=[]` (false block gone);
  `waiting_input=[mess-qa-automation:0.0]` (previously invisible, now surfaced);
  1 working undisturbed.

### (3) Notification delivery — honest + live-verified
- `notifications._send('local', …)` returned `status='sent'` while delivering to no
  surface → "history says sent but nothing arrives". Now returns `logged` (recorded
  locally, NOT delivered), so only a real channel reads as delivered.
- **Real channel = Telegram, live-verified reachable**: `_send('telegram', …)` →
  `status=sent, message_id=107, destination=5498907359, error=None`. Delivery is to
  Telegram — NOT an async push to ChatGPT (which cannot receive one).

## Tests
- Runtime `test_agent_control` 80 passed (added shell_running/waiting_input/
  no-false-external cases; corrected the old "timed out → externally_blocked" test to
  the owner-confirmed correct behaviour). Full runtime suite 757 passed; the lone
  `test_phase13` process-group-reaping failure reproduces on the clean baseline
  (environmental, ACAP-load), not from this change.
- Owner OS `test_notifications` 14 passed (local → logged, not sent).

## Deploy / rollback
- ai-runtime restarted (supervisor/orchestrator loop only — tmux agents untouched);
  backend rebuilt + healthy.
- Rollback: `git revert d631715 && systemctl restart ai-runtime.service`;
  `cd /opt/seo && git revert f8f6093 && docker compose build backend && up -d backend`.
  All changes are additive/behaviour-narrowing; revert cannot strand an agent.

## Deferred — next safe orchestration/notification defects (continuing)
2. Transition EVENTS on completed / waiting_input / genuine blocker / unexpected
   idle-stall / test-process-killed with dedup+evidence. Partial today: the watcher
   already emits `agent_recovery_failure` (idle/exited-unfinished) and
   `completion_retracted`, deduped by evidence; a `waiting_input` transition event is
   not yet emitted.
4. daily brief / portfolio still shows `queue_size=0` — ACAP, Mess-QA and future work
   are not in the orchestrator plan, so the portfolio is empty. Needs those existing
   agents registered as plan work (without creating agents).
5. Archive/remove `panel`, `shop`, old standalone `youtube` from the active registry
   (keep YouTube only as a Content Factory module) — owner-approved, non-destructive
   archive, pending.
