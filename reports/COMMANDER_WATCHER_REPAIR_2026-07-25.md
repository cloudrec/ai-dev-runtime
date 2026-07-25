# Commander + Watcher/Stuchalka repair — 2026-07-25

Internal only. No external publishing/payments/credentials/account/ad-spend. No
duplicate agents or runtime jobs created. Existing agents reused.

## Commits (deployed)
- **ai-runtime** (`/root/ai-dev-runtime`, `main`): `44dfc96..80665ad`
  - `80665ad` feat(watcher): stuchalka — idle/exited-on-unfinished → safe same-conversation resume or agent_recovery_failure
- **Owner OS** (`/opt/seo`, `feat/social-stage4-telegram-wordpress-20260720` = remote default): `b073c60..859d584`
  - `859d584` fix(brief/notifications/backup): 4-category brief, current-only alerts, actionable backup
  - (earlier same session: `362666e` mcp stale-briefing self-heal)

## What changed (by required outcome)
1. **Brief classification** (`daily_brief.py`): four fixed categories — ▓ LIVE STATE,
   ▓ WAITING OWNER ACTION, ▓ REAL BLOCKERS, ▓ HISTORICAL EVENTS. `completion_retracted`
   renders under HISTORICAL as "self-corrected (not a failure)", never a blocker
   (asserted). SEO waiting only on owner OAuth/accounts/publication → WAITING OWNER
   ACTION "internal build complete", not "unfinished". Spend = **0** when the
   collector is connected with no spend (unavailable only when the collector is down).
2. **Watcher/stuchalka** (`core/agent_watcher.py` + `agent_orchestrator` wiring +
   `orchestrator_plan.assigned_unfinished_task`): detects an EXISTING agent idle
   (past a dwell) or exited/dead while its assigned task is dispatched/in-progress.
   Idle+auto → resume the SAME pane/conversation (bounded `MAX_RESUMES`, idempotent
   send), never a duplicate. Exited/dead, non-auto, budget-locked, or retries
   exhausted → ONE `agent_recovery_failure` event. Deduped by `(agent, condition,
   evidence_hash)` with a 24h window: unchanged stall never re-alerts; any change
   (state/blocker/completion/context-growth/repetition) makes a new key.
3. **email:0.0**: live inspection shows the agent is **alive** (claude pid 2941291,
   cwd `/opt/email`, conversation **630096c4** = newest transcript), actively
   progressing, waiting on its 4 architecture questions. The "Claude exited with
   code 1 / No conversation to continue" is **stale scrollback**, not current. No
   resume performed — the conversation is already live; resuming would duplicate.
   Sending/cron/warmup untouched, no emails sent, no limit changes.
4. **Backup** (`backup_dr.status`): exact `hours_since_last` + single safest action
   `POST /api/owner/backup/snapshot` (never an auto-run); no backup ⇒ stale=True.
   Surfaced in briefing.py `backup.recommended_action`.
5. **Notifications current-only** (`cto_snapshot._notifications`): `warning_critical`
   / `delivery_failed` count only the current window (120m); older warning/critical
   → `historical_24h`. History is not a current alert.
6. **Misclassification fix (live defect)**: the earlier `owner_decision_request`
   event for email:0.0 (commander_events id=93) was **retracted** — deleted from the
   runtime store and the Owner OS `commander_delivered` ledger. Crash/failed-resume/
   idle-unfinished now classify as `agent_recovery_failure`, never owner decision.

## Tests
- Runtime full suite: **757 passed** (added 13 watcher tests incl. recovery-failure
  classification + resume-not-duplicate + dedup-on-change).
- Owner OS: focused `test_daily_brief` / `test_cto_snapshot` / `test_backup_dr` /
  `test_mcp` green (added: 4-category + retracted-not-blocker + spend-0; notifications
  current-only x2; backup actionable x3). Full suite **852 passed**; the 49 failures /
  18 errors are pre-existing sqlite-env issues (`test_paid_growth`, `test_editorial_
  naturalness`, `social_listening_briefs`), **none in touched files** (verified vs
  baked baseline).

## Service state (live)
- `ai-runtime.service`: **active**; supervisor + orchestrator started 09:55:30.
- Orchestrator ticks **fresh**: last_tick 07:55:31 → **07:57:03** (advancing).
- `seo-backend-1`: **healthy** (rebuilt with the brief/notifications/backup fixes).
- Owner OS scheduler alive (emit_event/opportunity_scan ran 07:47–07:49).

## Live evidence
- Brief: `▓ LIVE STATE … Spend today: 0`; `▓ WAITING OWNER ACTION … internal build
  complete: 1: SEO Growth OS internal build (Part F)`; `▓ REAL BLOCKERS`; `▓ HISTORICAL
  EVENTS`.
- Notifications: `warning_critical=0, current_alerts=0, historical_24h=2,
  delivery_failed=0`.
- email:0.0: exactly ONE claude root process (2941291, cwd /opt/email) — no duplicate
  tmux/Claude process; input box healthy. `owner_decision_request=0`,
  `agent_recovery_failure=0` for email (watcher does not false-flag the healthy agent;
  it has no orchestrator task, so no stall is raised).

## Unresolved / follow-ups
- **email:0.0 awaits owner** on its 4 architecture questions (mode split, staged
  approval, temp/audit-cron 25/day disposition). The agent is healthy and idle; this
  is genuine owner input, not a failure. Conservative defaults it already proposed
  (preserve cron/warmup, backup-first to `/opt/backups/email-outreach/`, additive
  migrations, no real sends) match policy.
- `agent_status` "recent activity" still echoes raw pane scrollback, which can include
  stale crash text. The watcher is immune (keys on real process liveness), but the
  raw display field could be trimmed to fresh lines in a later pass.

## Rollback
- Owner OS: `cd /opt/seo && git revert 859d584 && docker compose build backend && docker compose up -d backend`.
- Runtime: `cd /root/ai-dev-runtime && git revert 80665ad && systemctl restart ai-runtime.service`.
- The retracted email event (id=93) was an erroneous record; no restore needed. The
  watcher is additive and fail-safe (wrapped in try/except; no-op when no orchestrator
  task is assigned), so a revert cannot strand an agent.
