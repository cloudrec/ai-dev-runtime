# Owner OS autonomy — Stage 11 independent-review intake (read-only) — 2026-11-01

An automated instruction was received via the Owner OS API (tech lead). It is not owner approval. The author wrote this intake; it is **not an independent review**. **Live E2E: NOT PROVEN.** **PROD: NO-GO.**

This file is self-contained for a clean reviewer context.

## 1. State (verified with git; no code changes; no suite re-run, because the evidence is consistent)
- Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`.
- HEAD `2784d5a` (docs). Code is identical to **`dca2287`** (checked with `git diff --quiet`). Dirty files: **0**.
- Range `9557004..HEAD`: 29 commits. Code only (`core tools hooks config`): 17 files, +769/−68. `9557004` is still the live HEAD.
- Full suite at `dca2287`: **4320 passed, 4 skipped, 0 failed, exit 0** (661 s). Evidence: `reports/stage10/full_suite_dca2287.txt`; its first line is `sha: dca2287b908e…`.
- Earlier full suites, all with 0 failures:

  | commit | passed | skipped |
  |---|---|---|
  | 81e1b8e | 4316 | 4 |
  | e9afad4 | 4310 | 4 |
  | bf38344 | 4309 | 4 |
  | 5cfd7ab | 4253 | 4 |
  | a331e45 | 4248 | 4 |

## 2. Stage 3 regression evidence
- `reports/stage3/evidence_focused.txt`: 140 passed (170 s). It covers 8 files: autonomy-cycle E2E, fullcycle E2E, stop-wake loop, settled, delivery verification, base_ref, no-live-tree-imports, planner fallback.
- Caveat: the file does not record a SHA, command or exit code. The code was re-covered by every later full suite.

## 3. Seven review questions
Files are in `core/fullcycle/engine.py` (written `E`) unless named otherwise. Tests are in `tests/`.

**Q1 — Can a second envelope be sent while an attempt may still be active?**
- Code:
  - `E:458` `_try_dispatch` → `E:466` → `_stall_fence_open` `E:827`
  - `_clean_session_ready` `E:856`
  - held cancel in `_step_working` `E:875`
  - crash guard `E:1005-1006`
  - `quiescent` at `fullcycle/tmux_adapter.py:135,138` and `executor.py:100` (returns None, meaning not idle)
- Tests: `test_fullcycle_stall.py` (fence, cancel, crash-bypass at `:451`), `test_fullcycle_context.py`, `test_fullcycle_e2e.py:262`. Mutation-checked.
- Residual risk: live "idle" is classified from pane text (`waiting_input`). A resend held by the fence gets no second alert.

**Q2 — Can the stall alert fire more than once, or be lost on restart?**
- Code: `_stall_alert` `E:814`; the review row is unique per (task, attempt, kind); the deadline comes from the persisted `fc_dispatch.ts`; `_supersede_stall` `E:790` runs on a late `done` result and on a crash.
- Tests: `test_fullcycle_stall.py` (once, restart, unknown streak, supersede, crash).
- Residual risk: the new `fc_review.status` value `superseded`. A late answer gets `review_superseded` (`E:196`).

**Q3 — Is any agent self-report still used as evidence?**
- Code: `_verify` `E:616` (`E:624` `changed_files_unverifiable`); `_refusal` `E:412`; `_git_refusal` `E:569`; `_commit_pending` `E:1048`.
- Tests: `test_fullcycle_e2e.py` (`test_g1_*`).
- Residual risk: non-git Full Cycle roots are now refused, and live roots have not been checked. The agent's `summary` and `question` are still passed through as text for the reviewer.

**Q4 — Can a task get stuck, or can two tasks run at once?**
- Code: `_parked`/`_in_flight` `E:444-456`; `_next_task`; the F1 move-to-WORKING at `E:843-846`; the crash guard.
- Tests: `test_fullcycle_e2e.py:313,338,353`; `test_fullcycle_stall.py` (`test_f1_*`, `test_crash_after_alert_*`).
- Residual risk: no proof that all BLOCKED/OWNER_DECISION-with-existing-review paths are covered.

**Q5 — Context rotation guard (D2b).**
- Code: `attempt_in_flight_for_agent` `E:1259`; `agent_context_budget.py:565` (`fullcycle_holds`), `:628` (the guard before any handoff write or `/clear`).
- Tests: `test_fullcycle_context.py` (in-flight predicate, budget guard).
- Residual risk: a blind window on the first dispatch, before the handle has a target. Failing closed defers rotation.

**Q6 — Live DB changes.**
- Code: `E:119` `_MIGRATIONS` and `E:122` `connect()` add the column `fc_task.unknown_polls` (additive). New status value `superseded` (no schema change).
- Evidence: the full suite passes.
- Residual risk: the migration runs against the live control-plane DB on its first connect after promotion, so the DB must be backed up first.

**Q7 — Stage 3 changes.**
- Code:
  - `hooks/owneros_hook.py:39` (runs in every Claude session on the host)
  - `core/ai_planner.py:137,141,171`
  - `core/git_write.py:130,146,173,200`
  - `core/job_executor.py:436-466` (replan; the flag is off)
- Tests: `test_planner_fallback.py`, `test_job_base_ref.py`, `test_no_live_tree_imports.py`.
- Residual risk: the hook's path behaviour and the "tool-call reply → job blocked" behaviour change.

### Narrow excerpts (at `dca2287`)
```
E:444  def _parked(self, task):            # OWNER_DECISION + task-scoped refusal + no fc_dispatch row
E:454  def _in_flight(self, task): return state not terminal, not CREATED, not parked
E:839  obs = self.adapter.poll(prev)
E:840  if obs.state == DONE:  redispatch=0; if state != WORKING: _move(WORKING)  # F1; return False
E:849  quiet = True if obs.state == CRASHED else self.adapter.quiescent(prev)
E:850  if quiet is not True: note("resend_fenced"); return False
E:912  elif obs.state == CRASHED: ... E:917 self._supersede_stall(task, reason="crashed")
E:918      if not self.adapter.owns_agent: _block_for_owner(task, "agent_crashed_not_owned")
E:1005 elif kind == "blocked" and task["state"] == tc.BLOCKED and \
E:1006         not self._stall_review(task, attempt):   # a pure stall alert never resends past a gate
```

## 4. agency-agents coverage (all 4 files were read on 2026-11-01 as untrusted reference)
**orchestrator**
- Per-task QA gate and ≤3 retries: met.
- "Inconclusive evidence → FAIL": done (G1).

**multi-agent-systems-architect**
- Idempotency and checkpoint-before-side-effect: met.
- **Gap:** human-gate timeout behaviour. Owner gates still have **no SLA**. D1 covers only silent attempts.

**mcp-builder**
- **Gap:** this work did not re-audit the error shapes (`isError`, actionable errors) of the Full Cycle MCP/API.

**agentic-identity-trust**
- Fail closed when evidence can't be written: met (`fc_dispatch` row is written before the send).
- Hash-linked, signed audit trail: **rejected** as out of scope (no crypto overhaul).

## 5. Live E2E: NOT PROVEN
- The Stage 6 rehearsal (`reports/stage6/rehearsal_autonomy_cycle_b968696.txt`, 7 passed) mocks the agent, the ChatGPT page and the reviewer.
- Live evidence covers only the stage_done → wake *decision* (events up to 78548).
- The live tree is `9557004`, dirty, and contains none of this branch.

## 6. Independent review: REQUIRED (no self-approval)
Reviewer instructions:
1. In `/opt/owneros-autonomy`, read only this file. If needed: `git diff 9557004..dca2287 -- core tools hooks config`, plus the patches in `reports/stage{3,4,5,8,9,10}/`.
2. For each of Q1–Q7, return CONFIRMED-SAFE, DEFECT (with file:line and a repro), or NEEDS-DECISION.
3. Adversarial focus:
   - any path that sends while an attempt may be active or while an owner gate is open (Q1, Q4);
   - replay or duplicate decisions;
   - the migration on the live DB (Q6);
   - the hook's blast radius (Q7).
4. Do not modify the branch, the live tree, grants or the DB. Do not run the suite from `/root/ai-dev-runtime`, because it opens a live browser tab.
5. After the review: tech-lead decisions, then the owner-gated promotion and canary plan (`OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md` §6).

**Rollback** (branch only): `git revert dca2287 81e1b8e e9afad4 bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92` (for Stage 3, see its §1).
