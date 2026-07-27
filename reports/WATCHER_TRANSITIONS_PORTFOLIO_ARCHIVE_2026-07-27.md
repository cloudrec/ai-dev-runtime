# Commander watcher transitions + portfolio truth + registry archive — 2026-07-27

Continuation of WATCHER_STATE_NOTIFY_REPAIR_2026-07-27.md. Internal only. No
running agent touched or duplicated; no project data deleted. Verified light while
ACAP regression runs.

## Commits (deployed)
- **ai-runtime** (`main`): `0e3d000..a7c43e2`
  - `a7c43e2` feat(watcher): state-transition events + direct-agent orchestration truth
- **Owner OS**: registry archive is a DB data operation (audited OwnerEvent), no new
  code; notification honesty already shipped in `f8f6093` (prior pass).

## Items delivered
### 1 — Transition events + notifications (dedup + evidence) — DONE, live-verified
`agent_watcher.transition_event` (pure, 5 new tests) emits ONE deduped owner event
with pane evidence when an agent enters a notable state: `agent_completed`,
`agent_waiting_input`, `agent_externally_blocked` / `agent_owner_decision` (genuine
blocker), `agent_process_failed` (dead/exited/test process killed), an active→idle
stall (`agent_unexpected_idle`), or stuck→active `agent_recovered`. Keyed by
(agent, event, evidence_hash) → unchanged state never re-notifies. Wired into
`refresh_and_resolve` for EVERY agent incl. non-plan ones.
**Live:** real transitions fired on a sweep — `owneros-direct-fix agent_waiting_input`
+ `agent_recovered`, `mess-qa-automation agent_unexpected_idle`.

### 2 — Direct ACAP/Capacity + Mess in orchestration truth — DONE, live
Root cause (inventory): ACAP/Capacity (`/opt/capacity`) and Mess
(`mess-qa-automation`, `/opt/mess`) run live tmux agents but have ZERO
`orchestrator_task` rows, so `queue_size` truthfully read 0. `status()` now reports
`direct_active_agents` / `direct_active_count` (agents working/shell_running/
waiting_input outside the plan), also injected into the plan status.
**Live:** `direct_active_count=2` (mess-qa working, owneros waiting_input) while
`queue_size=0` — status no longer implies nothing is running. Agents never
dispatched-to or touched.

### 3 — Non-destructive registry archive — DONE
`project_controls.set_controls(status='archived', exclude_from_auto=True)` (one
audited OwnerEvent each) for `panel`(16), `shop`(20), `youtube`(27). Verified:
`status=archived, exclude_from_auto=t`, all 3 rows STILL PRESENT (count=3, no data
deleted). YouTube retained as the Content Factory module (a separate module, not
this standalone project row).

### 4 — shell-running/testing = working, not blocked/idle — DONE (prior `d631715`), re-verified
`externally_blocked=[]` live (benign shell output no longer false-blocks);
`shell_running` state available; `derive` treats it as fresh/active.

### 5 — Delivery canary + real transition — DONE
Telegram canary delivered (`status=sent, message_id=110, destination=5498907359,
error=None`) alongside real detected transitions. Channel = configured Telegram
owner surface — NOT a claimed async ChatGPT push.

## Tests / deploy
- Runtime full suite **762 passed** (added transition tests; the previously-flaky
  `test_phase13` reaping test also passed this run — environmental, as reported).
- ai-runtime restarted (loop only — tmux agents untouched).

## Rollback
- Transitions + direct-active: `git revert a7c43e2 && systemctl restart ai-runtime.service`.
- Registry archive (reversible, per project): re-run `set_controls` with
  `status='discovered', exclude_from_auto=False`, or
  `UPDATE global_projects SET status='discovered', exclude_from_auto=false WHERE slug IN ('panel','shop','youtube');`
  — data was never deleted.

## Next safe defect (continuing per directive)
- Owner OS portfolio/mission_control `overview()` still computes `runtime_jobs_active`
  from `RuntimeJob` only; surface the runtime's `direct_active_count` there so the
  Owner-facing portfolio card (not just the daily brief) shows ACAP/Mess directly.
