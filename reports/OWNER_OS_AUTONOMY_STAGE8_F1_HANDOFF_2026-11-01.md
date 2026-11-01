# Owner OS autonomy — Stage 8: F1 closure + canonical handoff (2026-11-01)

An automated instruction was received via the Owner OS API (tech lead). It is not owner approval.
**No independent review has been done.** **No live autonomous E2E has been done**; the Stage 6 rehearsal is not a live E2E.
**PROD: NO-GO.**

**Superseded as entry point by `OWNER_OS_AUTONOMY_STAGE9_HANDOFF_2026-11-01.md` (stall-alert supersede, `81e1b8e`).**

Entry points:
- this file
- `OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md` (the 7 questions)
- `OWNER_OS_AUTONOMY_STAGE3_5_HANDOFF_2026-11-01.md`

## 1. State
- Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`.
- At the start of Stage 8: HEAD `a63699c`, tree clean, code identical to `bf38344`.
- F1 fix commit: **`e9afad4`**. The commit for this handoff follows it.
- Live `/root/ai-dev-runtime` is still at `9557004`. Nothing was promoted.

## 2. F1
**Where:**
- Defect site: `core/fullcycle/engine.py` `_stall_fence_open`. The late-result branch, at :817-821 before the fix, cleared the resend intent and left the state alone.
- Second line involved: `_step` at :736. It does nothing for a BLOCKED task that already has a `blocked` review.

**Severity:** medium, liveness only.
- The task is stuck forever, the result is never verified, and nobody is notified.
- It is safe: nothing is ever sent twice.

**Deterministic repro:**
1. Run with `max_attempts=1`; the agent hangs.
2. The stall alert fires at 7200 s.
3. The tech lead answers → `_retry` at the limit → BLOCKED with an owner gate.
4. The stalled attempt's result file arrives.
5. The owner answers "resume" → the fence finds the result and sets `redispatch=0`.
6. Before the fix, the task stays BLOCKED with `last_reason=late_result_after_stall`.

**Fix** (`e9afad4`, +4 lines, :820-823):
- If the task is not WORKING, `_move(task, WORKING, evidence={"late_result_after_stall": n})`.
- That transition is legal from BLOCKED and from OWNER_DECISION.
- The next poll collects and verifies the result (WORKING → AGENT_DONE → VERIFYING → VERIFIED_DONE).
- No new send path: the resend stays dropped.

**Regression test:** `tests/test_fullcycle_stall.py::test_f1_late_result_after_limit_and_owner_resume_is_verified`.
- Before the fix: RED (timeout, task BLOCKED).
- After the fix: GREEN.
- It asserts `attempt == 1`, exactly one envelope, and the history tail WORKING → AGENT_DONE → VERIFYING → VERIFIED_DONE.

**Diff:** `reports/stage8/diff_f1_a63699c..e9afad4.patch`.

## 3. Tests
| run | command | result | evidence |
|---|---|---|---|
| F1 RED | `pytest tests/test_fullcycle_stall.py -k f1` before the fix | 1 failed (BLOCKED, timeout) | session output |
| stall suite | `pytest tests/test_fullcycle_stall.py` | 23 passed | session output |
| focused family (fullcycle*, autonomy-cycle, task-contract, context-budget, agent_watch*) | listed in the file | **458 passed, exit 0** | `reports/stage8/focused_f1.txt` |
| full isolated suite at `e9afad4` | `python3 -m pytest -q -p no:cacheprovider tests/` | **4310 passed, 4 skipped, 0 failed, exit 0** (605 s) = 4309 + 1 F1 test | `reports/stage8/full_suite_e9afad4.txt` |

Earlier evidence still holds for the unchanged code:
- `bf38344`: 4309 passed, 4 skipped, 0 failed.
- `5cfd7ab`: 4253 passed, 4 skipped, 0 failed.
- Stage 6 rehearsal: 7 passed. It mocks the agent, the ChatGPT page and the reviewer.

## 4. Remaining risks for the seven questions (unchanged except Q4)
- **Q1 (no second envelope while active):** live idle detection reads pane text (`waiting_input`). A long-fenced resend gets no second alert.
- **Q2 (one alert per attempt, survives restart):** a stale stall alert stays pending after a late result. Answering it is applied as stale.
- **Q3 (G1, no self-report as evidence):** non-git Full Cycle roots are refused; live roots have not been checked.
- **Q4 (no task stuck, no two tasks at once):** **F1 is closed.** Reviewer: confirm there are no other paths into a stuck BLOCKED or OWNER_DECISION task with a review or gate already present.
- **Q5 (in-flight check for context rotation):** there is a blind window on the first dispatch. Failing closed defers rotation for that pane.
- **Q6 (live DB migration):** the additive column `fc_task.unknown_polls` is created on the live DB at the first connect. Back up the DB first.
- **Q7 (Stage 3 items):** the hook at `hooks/owneros_hook.py:39` runs in every session on the host. The planner and base_ref changes still need review.
- **Other:** a fresh live session is accepted on the owner's "resume" without verification. Owner gates have no SLA. The live tree is dirty; there is one failed systemd unit (unidentified).

## 5. Rollback (branch only)
- `git revert e9afad4 bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92`.
- Stage 3: see its report §1.
- Nothing live to roll back.

## 6. PROD: NO-GO
Blockers:
- no independent review of Q1–Q7;
- no owner-approved promotion plan (Stage 7 §6);
- no live no-human-prompt canary.

## 7. Next action
A separately approved independent reviewer answers Q1–Q7 in the Stage 7 packet. Q4 now includes the F1 fix in `e9afad4`.
