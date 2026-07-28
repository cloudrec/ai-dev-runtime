# PROJECT STATE checkpoint — 2026-07-28

## System
- **Runtime** `/root/ai-dev-runtime` (systemd `ai-runtime.service`): supervises existing
  tmux Claude agents; orchestrator plan (goal→task→dispatch); durable `commander_events`
  (sqlite `agent_control.db`). Deployed branch `main`.
- **Owner OS** `/opt/seo/backend` (docker `seo-backend-1`, postgres `seo-postgres-1`
  db=traffic_os): daily brief, notifications, mission control. Deployed branch
  `feat/social-stage4-telegram-wordpress-20260720` (its remote default; no `main`).

## Reliability line completed (recent)
Event→notification pipeline hardened with THREE stale-alert barriers:
1. source-side retraction (`agent_control.retract_stale_condition_events`, runtime `6459202`),
2. notifier pre-delivery revalidation (`agent_notifier.revalidate_condition_event`, `9a6e1b8`),
3. post-delivery resolution/reconciliation on recovery/orphan/vanish/legacy.
Plus: bidirectional stall watchdog (`health.check_monitor_liveness` + worker heartbeats),
exact-keyed resolution via `Notification.subject_key`, restart-safe dedup
(`commander_delivered`, `NotificationState.last_event_id`), honest current-alert cleanup
(cto_snapshot excludes resolved/logged), no-spam digests (commander + health worker),
mission_control `commander_barriers` observability. All live-verified via Telegram.

## Known truthful data available
- Runtime `agent_control.agent_list()` → live tmux panes: target, claude_cwd, state
  (working|shell_running|waiting_input|idle|externally_blocked|dead|stale via
  `classify_state`), is_agent, alive, `duplicates` (by cwd). `redact()` available.
- Runtime `agent_orchestrator.all_records()` → per-agent stored: current_task,
  blocker_text, last_fresh_activity_ts, completion_evidence, notification_state, project.
- `status()` already exposes `records`, `direct_active_agents`/`direct_active_count`,
  `orchestrator` plan.

## CURRENT TASK — DIRECT AGENT TRUTH IN OWNER OS
Owner OS compact status shows runtime running=0 → looks like nothing works, while live
direct tmux agents ARE working. Add a truthful DIRECT AGENTS block to the daily
brief/status: per agent session/target, project cwd, state working|idle|waiting|dead,
current/last task, blocker, last-activity age, queued input, duplicate-cwd; keep Runtime
jobs separate; never present idle as active; source = live tmux/process/conversation
timestamps; redact secrets; owner-action shown separately; externally-blocked ≠ stalled.
Tests for working/idle/stale/dead/duplicate-cwd/no-conversation/queued-command/redaction.
Deploy after tests + live smoke; rollback + report.
