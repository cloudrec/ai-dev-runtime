# Handoff: Full Cycle intake start race (DEV task, not started)

Trigger: an automated instruction was received (Owner OS API). It is not owner approval.

This long session hands the task to a clean session, as the instruction allows. No code was changed for it here.

## Observed (gaika-catalog-walk, 2026-11-18)
- The project directory was born at 12:48:41Z. The `fc_project` row landed at 12:49:02Z, a copy window of about 21 s.
- A second `fullcycle_project_start` inside that window got `intake_directory_exists`, while the first call completed normally.
- Evidence: `reports/loop/gaika_first_real_2026-11-18/README.md`.

## Code (live = branch `758ebe4`, the file is unchanged since dc7f6c7)
In `core/fullcycle/intake.py` `start()`, roughly lines 255–345:
- It validates, then `existing = specs.get_project(...)`.
- If there is no project: `len(working) >= max_active`, then `os.path.lexists(path)` → `intake_directory_exists`, then `_copy_repo` (a long step, outside any lock or transaction), then `_hand_to_agent`.
- Then `gates.check_project_root`, `specs.create_project`, and `specs.put_spec`. An identical body returns the latest version without a bump (spec.py:166).
- `_copy_repo` (intake.py:147) runs `mkdir`, then on failure `rmtree`s only the directory it made.

## Gaps
1. No per-project serialization. A concurrent or retried identical call fails confusingly while the first one runs.
2. The capacity check (`working < max_active`) is also outside a lock. Two different projects started together can both pass it.
3. If the process dies between `mkdir` and `create_project`, the directory stays orphaned. Every later start then refuses with `intake_directory_exists` forever, and nothing records what it is.

## Proposed smallest design (for the clean session to verify)
**A durable pending marker,** table `fc_intake_pending(project_id PK, spec_sha, repo, by, pid, started_ts)`. It is written under `BEGIN IMMEDIATE` together with the capacity and existence checks, before `mkdir`, and deleted in the same transaction that creates the project row.
- Pending counts toward `max_active`.

**Same id while pending:**

| Case | Answer |
|---|---|
| Same spec sha and repo | `{"ok": false, "pending": true, "reason": "intake_in_progress"}`. No second copy, no agent, no spec bump. |
| Different sha or repo | `project_start_in_progress_with_another_spec` (explicit) |

**Stale pending.** A marker counts as stale once its pid is gone or its age exceeds a bound (e.g. 15 min).
- A start that meets a stale marker refuses with `intake_partial_copy_needs_review:<path>` and **never deletes or overwrites** the directory.
- The directory and the marker stay as evidence. Recovery stays a deliberate, separate act.

**A directory with neither a marker nor a project** (pre-existing orphan) keeps the current `intake_directory_exists` refusal.

**Unchanged:**
- allowlist (`repos.get`), check gating, `require_root_work`, fcagent ownership and the `max_active` semantics;
- identical retry after creation: returns the existing project with the same `spec_version` and `created: false`.

## Tests required
- **Deterministic concurrency.** Patch `_copy_repo` with an Event-gated fake. Call A blocks inside the copy, call B runs and gets `intake_in_progress` (same spec) or the conflict reason (other spec). Release A. Assert:
  - one `fc_project`;
  - spec v1;
  - one copy call;
  - no agent start.
- **Capacity race.** Two different ids with `max_active=1`, both gated, give exactly one created.
- **Crash.** Kill between `mkdir` and the row (raise `SystemExit` in the fake after `mkdir`). The next start refuses with `intake_partial_copy_needs_review`, and the directory is still present and unmodified.
- **Retry after done:** idempotent, no spec bump. A conflicting spec on an existing project keeps today's new-version semantics.
- The old version must fail the concurrency and crash tests. Run them on `758ebe4` in a scratch worktree.
- **Mutants:**
  - no marker;
  - marker outside the transaction;
  - pending not counted in capacity;
  - stale removal by deletion;
  - same-sha check ignoring the repo.
- `tests/test_fullcycle_intake.py`, related fullcycle suites, then the full suite.

## Constraints
- DEV only: no deploy, restart, push, permission change or payment agent. No GAIKA changes.
- The schema addition is created on first use. Release later through `deploy/adaptive_offer/install.sh --check`/`--apply`, rolling back with the backup it prints.

## Clean-session start
In a new Claude session in `/opt/owneros-autonomy-loop` (same branch), send:

> Read reports/OWNER_OS_INTAKE_RACE_HANDOFF_2026-11-18.md and reports/OWNER_OS_LOOP_HANDOFF_2026-11-17.md (Current state). Implement the intake start race fix as specified there, DEV only, with the listed tests, an old-version failing reproduction, mutants and the full suite; then stage_done. No deploy, push, permission change or GAIKA change.
