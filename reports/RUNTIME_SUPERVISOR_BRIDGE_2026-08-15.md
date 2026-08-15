# Runtime → Event/Wake/Supervisor Bridge — 2026-08-15

## Incident

Runtime jobs were invisible to Owner OS monitoring. Concrete failures:

- Job `888f5266` (task OWNER-193, Venture Radar) failed 2026-08-15 07:05Z:
  `branch failed: error: Your local changes to the following files would be
  overwritten by checkout: reports/OWNER_OS_WAKE_BRIDGE_REPAIR_2026-08-11.md,
  reports/phase3_postfix_soak.jsonl`. No event, no notification, no wake.
- Jobs `54a8a047` (OWNER-192) and `43c0888c` (OWNER-200) failed minutes later
  with the same dirty-checkout error (`43c0888c` on /opt/seo's dirty
  `reports/OWNER_OS_MANAGEMENT_CONTROL_V4_2026-07-21.md`).
- The same class had already killed jobs for tasks 151/180/182.

## Root causes

1. **Dirty-checkout**: the executor's branch stage ran `git checkout -b work
   base` inside the live project working tree (`core/git_write.py`
   `create_work_branch`). Any owner dirty file in the checkout's path aborted
   the job; a successful job left the control repo sitting on its work branch.
2. **Silence**: `core/job_executor.py` / `core/job_store.py` emitted no events
   on any lifecycle transition. Failures terminated in `runtime_jobs.db` only.
3. **No watchdog**: nothing watched job `heartbeat_at`/`updated_at`; a stuck
   `planning`/`queued` row stayed stuck forever (five `queued` rows from
   July sat unnoticed for a month). `reap_orphaned()` existed but was never
   called from production code.

## Changes (commits cc0d2a4, 0aca450, 3f4cdc4, 9addae5 — local only)

1. **Isolated worktrees** (`core/job_workspace.py`, executor rewiring): every
   repo job materializes its branch as `git worktree add -b <branch> <dir>
   <base>` under `/var/lib/ai-runtime/worktrees`. The primary tree — dirty
   files included — is preserved byte-for-byte by construction. Edits, tests,
   commits, pushes run in the worktree; the branch survives its removal on
   every exit path. Rollback of an isolated job = discard the worktree.
2. **Lifecycle → wake pipeline** (`core/runtime_events.py`, hooks in
   `core/job_store.py`): every transition is a durable CTO event, deduped per
   (job, status). failed/blocked → `task_failed`/`action_blocked` (high,
   owner-action); waiting_approval → `owner_decision_required`; completed →
   `task_completed`; stage moves → routine `runtime_job_state` (no wake).
   Routes derive from project_path (control repo → `owner-os`). Emission is
   best-effort: a control-plane outage can never fail a job write.
3. **Watchdog** (`core/runtime_watchdog.py`, runs in the wake companion —
   outside the runtime service, so a dead service cannot kill its own
   watchdog): `runtime_job_stalled` only on evidence — execution-stage job
   silent on BOTH heartbeat and updated_at past 120s, or `queued` never picked
   up past 600s. `waiting_approval` is never a stall. One emission per
   episode, re-arm on life, reminder hourly, restart-safe state in
   control_plane.db.
4. **Bounded supervisor** (`core/runtime_supervisor.py`): auto-retry only for
   dirty_checkout (only with isolation active), worker crash, orphaned reap.
   One retry per failed job, one per task lineage, via the runtime HTTP API,
   `approval_required` carried verbatim (owner gates never auto-approved), no
   retry while any peer job for the task is active or newer.
5. **Surfaces**: `GET /api/v1/runtime/status`; `runtime_blockers` section in
   `observability_summary` (stalled jobs are a red reason).
6. **Fixes found by the live recovery run**: BackupEngine entry-cleanup no
   longer deletes a concurrent snapshot's fresh tmp (ENOENT race between
   parallel same-repo jobs); runtime event emission refuses inside pytest
   unless CONTROL_PLANE_DB points at a sandbox (a worktree suite with an old
   conftest leaked 126 test events into the live log — neutralized via
   wake_bridge.acknowledge); tests/conftest.py resolves the repo root
   relative to itself (worktree/stash baselines import their own tree) and
   pins AGENT_CONTROL_DB + RUNTIME_WORKTREE_ROOT; dangling
   /opt/mess/reports/PROJECT_STATE.md removed from config/project_queues.yaml
   (the authoritative queue file remains); RUNTIME_TEST_TIMEOUT=600 in
   configs/.env (repo suite ~380s > old 300s default).

## Tests

Full suite: **1977 passed, 0 failed** (was 1970 passed / 5 failed at start;
the 5th was the dangling mess pointer). New fixtures: exact job-75
dirty-checkout reproduction (old path still fails on it, worktree path
succeeds; dirty bytes asserted equal), planning-без-heartbeat stall (jobs
74/76 shape), queued-never-picked-up, no-false-stall on live heartbeat,
waiting_approval-is-not-a-stall, dedupe/re-arm/restart persistence, retry
lineage idempotency and budget, approval preservation, concurrent-backup tmp
survival, pytest live-DB guard.

## Live proof

- Services restarted: ai-runtime, owner-os-wake-companion.
- First companion pass: watchdog emitted `runtime_job_stalled` for 5 genuinely
  stranded July `queued` rows (test debris; cancelled with audit note), and
  the supervisor auto-retried all three failed lineages exactly once each
  (200→6761283f, 193→e4a1b151, 192→dd6ee850) through the API.
- All three retries PASSED the branch stage in isolated worktrees — the
  dirty-checkout class is dead. Dirty files verified byte-identical (sha256)
  in both repos throughout.
- Wake delivery verified end-to-end same morning (event 4030 delivered to the
  owner-os chat, `submitted_and_user_turn_appeared`); runtime events queue
  behind the same cooldown/coalescing as agent events.

## Recovered job states (tasks 192/193/200)

- OWNER-193: supervisor retry hit the (now fixed) backup race; operator
  redispatches on the deployed feature line — final state in the runtime job
  ledger; the fallback PLAN document is committed on branch
  `ai-runtime/193-owner-os-2-0-venture-radar` when the planner returns
  non-JSON (twice today), i.e. truthful `fallback_plan_only`, never a false
  "completed".
- OWNER-192: retry failed honestly — its worktree branched from stale `main`,
  whose old conftest imported live code (leak now guarded). Substantively,
  Agent Fabric's runtime foundation is THIS change-set; the task continues on
  the feature line.
- OWNER-200: /opt/seo repo suite cannot even collect on the host (42 import
  errors — backend deps live in the baked Docker image). Any host-run
  code_change job there fails validation regardless of isolation.

## Remaining material issues

1. `/opt/seo` jobs need container-aware validation (or a non-code job kind);
   host pytest is structurally impossible there.
2. The AI planner frequently returns non-JSON / times out under concurrency →
   deterministic fallback plans. Feature-scale tasks (Agent Fabric, Venture
   Radar) will keep ending `fallback_plan_only` until planned by a real agent.
3. This repo's live line is a work branch by name
   (`ai-runtime/182-...`), so implicit base resolution falls back to stale
   `main`; jobs must pass `base_branch` explicitly (or the owner merges the
   line to `main` — owner decision, not taken).
4. Telegram push channel: runtime notifications enqueue but the channel is
   red (pre-existing dead-letters). The wake path is the working channel.
5. `test_live_shipped_config...` guarded config↔disk coherence and was red
   for 11 days before this — nothing consumed that signal (loop_liveness is
   pull-only). Same gap class the runtime bridge just closed for jobs.
