# Owner OS autonomy — Stage 7 independent REVIEW PACKET (2026-11-01)

An automated instruction was received via the Owner OS API (tech lead); it is not owner approval.
The author prepared this packet. **It is not an independent review.** **No live autonomous E2E has passed.**

## 1. Git state
- Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`.
- HEAD before this packet: `11c435fc8519af8b5da836842fed160025fb950e`. Tree clean.
- Base `9557004` = the live tree's HEAD. The live tree is **dirty** (38 status entries).
- Code head `bf38344` (all later commits are docs). 22 commits in `9557004..HEAD`.

**Code commits (oldest → newest):**
- 2fe3756 tests import their own checkout
- e63e9ac pseudo tool-call is named, never shadows the plan
- 4a83161 tool-call reply → job blocked
- 2d5acf2 opt-in per-project base_ref
- 191519c stage_done imports own checkout
- ff93b10 ≤1 bounded replan (flag off)
- ccbcb25 two different plans refused
- 9a86a92 owner gate parks only the gated task
- a4f87e0 regression gate
- 5cfd7ab lease renewed in the regression gate
- eb3b30d G1
- 9fb024a D1
- bf38344 D2 + D2b
- (test-only: aacad1b, f27d538)

**Scoped production files** (`git diff --stat 9557004..bf38344 -- core tools hooks config`; 17 files, +733/−67):
- `core/fullcycle/engine.py` +347
- `core/job_executor.py` +135
- `core/git_write.py` +110
- `core/ai_planner.py` +49
- `core/fullcycle/tmux_adapter.py` +45
- `core/agent_context_budget.py` +16
- `core/fullcycle/executor.py` +6
- `config/project_base_refs.yaml` (empty map)
- `hooks/owneros_hook.py` (1 line)
- `tools/{wake_companion,cdp_composer,stage_done,base_ref_report,…}.py`

**Diffs:**
- `reports/stage3/diff_production_9557004..ccbcb25.patch`
- `reports/stage4/diff_production_a331e45..5cfd7ab.patch`
- `reports/stage5/diff_production_b7d0da1..bf38344.patch`
- The matching `diff_tests_*` patches.

## 2. Test evidence (from files; no suite re-run in Stage 7)
| run | code SHA | result | file |
|---|---|---|---|
| full suite | 5cfd7ab | 4253 passed, 4 skipped, 0 failed, exit 0 | `reports/stage4/full_suite_5cfd7ab.txt` |
| full suite | bf38344 | **4309 passed, 4 skipped, 0 failed, exit 0** (774 s) | `reports/stage5/full_suite_bf38344.txt` |
| focused E2E set (8 files) | ccbcb25 era | 140 passed (file lacks SHA, command, exit) | `reports/stage3/evidence_focused.txt` |
| **rehearsal** `tests/test_autonomy_cycle_e2e.py` | bf38344 | 7 passed, exit 0 | `reports/stage6/rehearsal_autonomy_cycle_b968696.txt` |

**What the rehearsal is:**
- Real: engine, contracts, git, `wake_bridge`, `wake_routes`, `wake_companion`, the `cdp_composer` logic.
- Isolated: temp DBs and a private tmux.
- **Mocked:** the agent, the ChatGPT page (fake CDP) and the reviewer.

It is **not a live E2E**. The only real evidence is the wake *decision* for live `stage_done` (events 78155 → 78453, route owner-os, on live code 9557004).

**Stage 7 probe:** one throwaway test, run and deleted, not committed. It confirmed finding F1 (§4).

## 3. Seven-question independent review checklist
Code lines are at HEAD. Answer each with CONFIRMED, PLAUSIBLE or REJECTED, plus file:line.

**Q1 — Can a second envelope be sent while the previous attempt may be active?**
- Code paths:
  - `engine.py:454` `_try_dispatch` → `:462` `_stall_fence_open`, defined at `:804`
  - `:819` late-result drop
  - `:829` `_clean_session_ready`, which needs `quiescent() is True`
  - held cancel at `:851`
  - `_owner_answers` resume at `:1083`
  - single-executor wait inside `_try_dispatch`
  - `_in_flight` at `:450`
- Adapters:
  - `tmux_adapter.py:135,138` idle states are `waiting_input`, `dead`, `stale`
  - base `executor.py:100` returns `None`, which is treated as "not idle"
- Tests:
  - `test_fullcycle_stall.py:166,183,206,241`
  - `test_fullcycle_context.py:101,115`
  - `test_fullcycle_e2e.py:262,353`
- Mutation check: removing the fences fails 3 + 3 tests.

**Q2 — Can the stall alert fire more than once per attempt, or be lost across an engine restart?**
- Code paths:
  - `_stall_alert` `engine.py:791`
  - `_request_review` `:533`, whose `INSERT OR IGNORE` at `:536` is unique per (task, attempt, kind)
  - `_stall_review` `:751`
  - deadline from the persisted `fc_dispatch.ts` (`_sent_ts`)
  - UNKNOWN counter in `fc_task.unknown_polls`
  - constants at `:71,72`
- Tests: `test_fullcycle_stall.py:78,86,101,114,127`.

**Q3 — Is any agent self-report still used as verification evidence (G1)?**
- Code paths:
  - `_verify` `engine.py:612`, with `changed_files_unverifiable` at `:620`; the claim is stored only as `claimed_changed_files`
  - `_refusal` `:408`, with `git_status_unavailable` at `:425`
  - `_git_refusal` `:565`, with `project_root_not_git` at `:572,575`
  - `_commit_pending` `:1013`
- Note for the reviewer: `summary` and `question` still come from the agent, but only as text shown to the reviewer.
- Tests: `test_fullcycle_e2e.py:418,434,444,454,466,481`.

**Q4 — Do `_parked`/`_in_flight` (Stage 4) interact with the D1/D2 states so that two tasks run, or a task is stuck forever?**
- Code paths:
  - `_parked` `engine.py:440`, scoped refusals at `:437`
  - `_in_flight` `:450`
  - `_next_task` `:1139`
  - BLOCKED, OWNER_DECISION and held-cancel WORKING all count as in flight
- **Author finding F1 (stuck, CONFIRMED by probe), see §4.**
- Tests: `test_fullcycle_e2e.py:313,338,353`, `test_fullcycle_stall.py:241`.

**Q5 — `attempt_in_flight_for_agent`: is the session match correct, and what is the blast radius of failing closed on live rotation?**
- Code paths:
  - `engine.py:1224`
  - `agent_context_budget.py:565` `fullcycle_holds` and `:628` its guard, placed before any handoff write or `/clear`
  - the caller key is `agent["target"]` (`agent_orchestrator.py:526,673`)
  - the handle `target` is set in `tmux_adapter.py` `submit`
- Fail-closed effect: any error defers context rotation for that pane, never forces it.
- Tests: `test_fullcycle_context.py:222,230,235,242,248,266`.

**Q6 — Is the schema migration safe on the live control-plane DB?**
- Code paths: `engine.py:119` `_MIGRATIONS`; `:122-130` `connect()`, which adds a column via `PRAGMA table_info` + `ALTER TABLE ADD COLUMN`.
- Additive only. Old code names its columns, so it ignores the new one.
- It runs at the first `connect()` after promotion, so the DB must be backed up first.

**Q7 — Stage 3 items: are they correct and safe?**
- `hooks/owneros_hook.py:39` `realpath` root. This hook runs in every Claude session on the host.
- `tools/wake_companion.py`, `tools/cdp_composer.py` repo-root derivation.
- `core/job_executor.py` tool-call → blocked, `BaseRefBlocked`, replan at `:436-466` (flag off).
- `core/ai_planner.py:137` `_looks_like_tool_call`, `:141` `_extract_json`, `:171` "several different plans".
- `core/git_write.py:130` `BaseRefBlocked`, `:146` `configured_base_ref`, `:173` `_staleness`, `:200` `resolve_job_base`.
- Tests: `test_planner_fallback.py`, `test_job_base_ref.py`, `test_no_live_tree_imports.py`.
- Details in Stage 3 report §5.

## 4. Findings and cross-cutting areas
**F1 — CONFIRMED, author probe, NOT fixed.** A liveness defect; it cannot cause a double send.
1. A stall is answered when the task is already at `max_attempts`.
2. `_retry` → `_block_for_owner` → the task goes to BLOCKED with an owner gate.
3. The late result of the stalled attempt arrives, and the owner answers "resume".
4. `_request_dispatch` → `_stall_fence_open` finds the late result (`engine.py:819`) and sets `redispatch=0`.
5. The task stays **BLOCKED** forever: `_step` at `:736` does nothing because a `blocked` review already exists. The late result is never verified, and nothing tells the owner.

Probe output: `FINAL BLOCKED late_result_after_stall redispatch=0 envelopes=1`.

Proposed minimal fix, needing tech-lead approval:
- On a late result, if the state is not WORKING, move the task to WORKING (BLOCKED → WORKING and OWNER_DECISION → WORKING are legal transitions), so the next poll verifies the result.
- Add a test for this sequence.

**Security / identity / grants:**
- No grant, secret or auth code changed.
- Full Cycle sends through `agent_control.agent_send` with actor `fullcycle:engine` (`tmux_adapter.py:104-105`).
- `automation_grants.allows_send` applies to `api:` actors only; Full Cycle does not use it.
- Grants have no session-lifecycle action, so a live clean session is an owner gate (`clean_session_required`, `engine.py:967`).
- The test conftest walls `:9222` and uses temp DBs.

**Idempotency / replay:**
- `fc_dispatch` `INSERT OR IGNORE` (`engine.py:471`) is keyed `fullcycle:{task}:{attempt}`, written before submit.
- Reviews are unique per (task, attempt, kind) (`:536`).
- `submit_decision` (`:175`): the same key returns the same answer (`:186`); a different key on a decided review returns `review_already_decided` (`:197,214`).
- Events use dedup keys.
- Tests: `test_fullcycle_e2e.py:108,262`, `test_fullcycle_stall.py:206`, `test_fullcycle_context.py:164`.

**Full Cycle decision semantics:**

| review kind | decisions | effect |
|---|---|---|
| review | approve / reject | as before |
| blocked (agent question) | answer / cancel | retry with the answer, or cancel |
| blocked + `stalled` (D1) | answer | fenced resend |
| | cancel | held until the attempt is proven over |
| blocked + `context_exhausted` (D2) | answer | clean session after a safe stop, then resend with the handoff |
| | cancel | cancel |
| project | add_tasks / escalate | as before |

- Owner gates remain for: permission prompts, task-scoped hazards, project-wide refusals, the max-attempts limit, `clean_session_required`, and `clean_session_failed`.

**Wake routing:**
- No routing code changed in Stage 4/5. Full Cycle reviews emit `task_completed` (`engine.py:60`) via `_offer_to_wake_bridge` (`:227`) → `wake_routes.resolve` (`core/wake_routes.py:361`).
- The D1/D2 reviews use the same path but were not exercised through the companion (Stage 6 §2).

**DB migration / rollback:** Q6. Code rollback (branch): `git revert bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92` (Stage 3: per its §1). The added column may stay; old code ignores it.

**Live PROD skew:**
- Live is at 9557004 and dirty.
- The branch is 22 commits ahead, all unpromoted.
- These live services are active: companion, chromium, fullcycle-worker, ai-runtime.
- One systemd unit shows `loaded failed` (unidentified).

**Unresolved blockers:**
1. F1.
2. Live Full Cycle roots not checked against G1b.
3. A stale stall review stays pending after a late result.
4. A fresh live session is not verified (it rests on the owner's "resume").
5. Live idle detection is a pane-text heuristic.
6. A long-fenced resend gets no second alert.
7. D2b has a blind window on the first dispatch.
8. No live no-human-prompt E2E.

## 5. agency-agents cross-reference (real gaps only)
- **orchestrator — "Inconclusive evidence: Default to FAIL":** applied (G1).
- **architect — "Timeout behavior must be defined" for human gates:** D1 covers silent attempts. Owner gates themselves still have **no SLA** (open).
- **architect — "any agent that can be retried MUST be idempotent":** met (dispatch/review/decision latches).
- **identity/trust — "If evidence cannot be written, the action should not proceed":** met (`fc_dispatch` row before send).
- **identity/trust — hash-linked audit:** rejected (out of scope).
- **MCP builder:** not re-audited in Stages 4–7. No claim.

## 6. Minimal owner-approved promotion + canary plan (nothing done)
1. Independent review of §3, plus a tech-lead decision on F1 (fix before promotion is recommended).
2. Owner decides the 38 uncommitted entries in the live tree.
3. Read-only check of live Full Cycle project roots, so G1b does not stop a live project unexpectedly.
4. Back up the live control-plane DB and agent-control DB, plus a tarball of the live tree.
5. Install the branch at a recorded SHA. Restart `owner-os-wake-companion`, `owner-os-fullcycle-worker` and `ai-runtime`. Run one hook execution check (Q7, hook line 39).
6. Rollback: restore the tree tarball and DB backups, then restart the same services.
7. Canary, owner-approved:
   - a sandbox git project;
   - its own ChatGPT route binding;
   - one task;
   - pass criteria, with no human prompt: event → companion delivery verified → ChatGPT `ack_review` + `submit_decision` → next task → `project_completed`;
   - abort on any owner-gate event, or on no delivery within the SLO.
