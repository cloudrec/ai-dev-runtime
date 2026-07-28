# DIRECT AGENT TRUTH in Owner OS — 2026-07-28

Internal only. No new agents, no external actions, no destructive ops. Existing
running ACAP/Mess agents untouched (read-only evidence).

## Problem
Owner OS compact status showed Runtime running=0 → looked like nothing works, while
live direct tmux Claude agents were actually working. Owner needs the reality.

## Commits (deployed)
- **ai-runtime** (`main`): `898dfa9` (PROJECT_STATE checkpoint), `4ef0add`
  (feat: truthful DIRECT AGENTS snapshot)
- **Owner OS** (`feat/social-stage4-telegram-wordpress-20260720`): `7758644`
  (feat: DIRECT AGENTS daily-brief block)

## What changed
- **Runtime** `build_direct_agents` (pure/tested) merges LIVE tmux panes
  (`agent_list`: target, cwd, state, alive, duplicates-by-cwd, redacted `queued_input`
  + `last_pane_line`) with stored records (current_task, blocker, last_fresh_activity_ts,
  completion_evidence) → truthful per-agent view: session/target, project cwd, raw state
  (working|shell_running|waiting_input|idle|externally_blocked|dead|stale) + display
  bucket working|waiting|idle|dead, current/last task, blocker, last-activity age, queued
  input, has_conversation, duplicate_cwd, owner_action. Exposed as
  `status().direct_agents`. `agent_list` now attaches redacted `queued_input`/`last_pane_line`.
- Source of truth = actual tmux/process/conversation evidence; secrets redacted via
  `ac.redact`. Runtime jobs stay in their own section — never conflated. Idle is never
  shown as active. `externally_blocked`/`waiting_owner` = OWNER ACTION, never "stalled".
- **Owner OS** daily brief renders `▓ DIRECT AGENTS (live tmux — separate from Runtime
  jobs)` with a working/waiting/idle/dead count line + per-agent lines ordered
  working→waiting→idle→dead, showing cwd, task/last-result, age, ⌨ queued input, ⛔
  blocker, ⚠ OWNER ACTION, ⚠ DUP cwd.

## Live smoke (real agents)
Runtime `status().direct_agents` = 9 agents: **working** owneros-direct-fix, mess-qa;
**waiting** email (queued input), security + seo-audit (waiting_owner, owner-action);
**idle** capacity, payment, polyinput, safeguard. Brief block rendered e.g.:
```
▓ DIRECT AGENTS (live tmux — separate from Runtime jobs)
Working: … · Waiting/owner: … · Idle: … · Dead/stale: …
  • owneros-direct-fix [working] /root/ai-dev-runtime: reports/PROJECT_STATE_… (age 115s)
  • email [waiting_input] /opt/email: …  ⌨ queued: Дополнение по безопасности: пока…
  • security [waiting_owner] /opt/security: …  ⛔ (unreadable prompt)  ⚠ OWNER ACTION (not stalled)
```
Next owner brief can now honestly answer who is working and on what.

## Tests
- Runtime `test_direct_agents` (11): working≠idle+task, idle bucket, shell-running=working,
  stale+dead→dead, waiting/queued command, externally-blocked=owner-action, no-conversation,
  duplicate-cwd both flagged, non-agent skipped, secret redaction, completion last_result.
  Full runtime suite **784 passed**.
- Owner OS `test_daily_brief` (18) incl. the DIRECT AGENTS render (working/waiting counts,
  OWNER ACTION, blocker, order).

## Deploy / health
- ai-runtime restarted (read-only agent_list; panes untouched); backend rebuilt healthy.

## Rollback
- Runtime: `git revert 4ef0add && systemctl restart ai-runtime.service`.
- Owner OS: `git revert 7758644 && docker compose build backend && docker compose up -d backend`.
  All additive/read-only; revert removes the block, no data touched.

## Note
The full brief render is slower now (per-agent `-e` pane captures for queued-input +
cto_snapshot); it runs once/day (send_if_due) or on demand — acceptable. Optimization
(cache the snapshot / cap capture to at-rest agents — already done) is a later item.
