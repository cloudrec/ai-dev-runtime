# Owner OS autonomy — Stage 10 handoff (2026-11-01) — CANONICAL ENTRY POINT

An automated instruction was received via the Owner OS API (tech lead). It is not owner approval.
This file was written by the author and is **not an independent review**.
**No live autonomous E2E has been run.** **PROD: NO-GO.**
This file supersedes the Stage 9 handoff as the place to start. The 7 review questions, with line refs, are in `OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md`.

## 1. State
- Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`.
- At the start of Stage 10, HEAD was `c065d2e`, not `81e1b8e`. `c065d2e` changes docs only; I checked that the code equals `81e1b8e`.
- Fix commit: **`dca2287`**. The docs commit follows it.
- Live `/root/ai-dev-runtime` is still at `9557004`. Nothing was promoted.

## 2. Risk: a crash after a stall alert
**Reproduced, and more severe than "stale".**
- The attempt is stalled and has been alerted, then it CRASHES. The crash path puts the task BLOCKED behind an **owner gate**: `agent_crashed_not_owned`, or `max_attempts_N_after_crash`.
- The stall alert stayed pending.
- If the tech lead answered it, the generic `blocked` branch applied it. `_retry` then **resent past the open owner gate**.
- RED evidence: the task reached `VERIFYING` instead of staying `BLOCKED`. 4 tests failed.

**Fix (`dca2287`, `core/fullcycle/engine.py` +15/−4):**
1. A `CRASHED` observation calls `_supersede_stall(task, reason="crashed")` (`:917`, function at `:790`). The pending alert for that attempt becomes `superseded`. A late answer is refused with `review_superseded`.
2. Defensive guard (`:1006`): a pure stall alert (payload `stalled`) is never applied to a BLOCKED task; it is treated as stale.
   - A slot reused by a late `blocked` report has no `stalled` key, so it is still applied.
   - Mutation check: removing the guard makes `test_a_stall_answer_never_bypasses_a_crash_owner_gate` fail.

**Tests** (`tests/test_fullcycle_stall.py`):

| test | line | what it checks |
|---|---|---|
| `test_crash_after_alert_unowned_goes_to_owner_and_retires_the_alert` | `:429` | exactly one owner event; alert superseded with reason `crashed`; answer refused; task stays BLOCKED and gated; still 1 envelope |
| `test_a_stall_answer_never_bypasses_a_crash_owner_gate` | `:451` | race where the decision is recorded after the gate opens: nothing resent |
| `test_crash_after_alert_owned_recovers_once_and_retires_the_alert` | `:470` | one restart, attempt 2 sent once, verified |
| `test_crash_after_alert_at_the_limit_goes_to_owner` | `:490` | crash at the attempt limit goes to the owner gate |

There is no false success in any case.

**Diff:** `reports/stage10/diff_81e1b8e..dca2287.patch`.

## 3. Test results
| run | result | evidence |
|---|---|---|
| new tests before the fix | 4 failed | session output |
| stall + context suites after the fix | 61 passed | session output |
| focused: fullcycle*, autonomy-cycle, task-contract, context-budget, agent_watch*, wake_bridge*, wake-delivery | **597 passed, exit 0** | `reports/stage10/focused_crash_after_stall.txt` |
| full suite at `dca2287` (`python3 -m pytest -q -p no:cacheprovider tests/`) | **4320 passed, 4 skipped, 0 failed, exit 0** (661 s) = 4316 + 4 new | `reports/stage10/full_suite_dca2287.txt` |

Previous full-suite run: `81e1b8e`, 4316 passed, 4 skipped, 0 failed. No baseline failures.

## 4. Seven questions still requiring independent adversarial review (`9557004..dca2287`, code only)
1. **No second envelope while an attempt may be active.** Check the fences, held cancel, pane-text idle detection, and now the crash guard at `:1006`.
2. **Stall alert exactly once per attempt, survives a restart.** It is now superseded on a late result or a crash.
3. **G1:** no agent self-report used as verification evidence.
4. **No stuck task, no two tasks at once.** Covers F1 (`e9afad4`) and this crash path.
5. **`attempt_in_flight_for_agent` / D2b:** session matching and fail-closed behaviour.
6. **Live DB:** the additive column `fc_task.unknown_polls`, plus the new `fc_review.status` value `superseded`.
7. **Stage 3 items:** the hook at `hooks/owneros_hook.py:39`, the planner, `base_ref`, and the `job_executor` blocked paths.

## 5. Security gates (unchanged)
- No change to grants, secrets, auth or routing.
- Full Cycle sends through `agent_control` as `fullcycle:engine`.
- These all still go to the owner gate: a live clean session (`clean_session_required`), permission prompts, task hazards, the attempt limit, and crashes of a live agent.
- The test suite walls off the live browser port (`:9222`) and uses temp DBs.

## 6. Remaining open risks
- Live idle detection relies on pane text.
- A resend held behind the fence gets no second alert.
- Live non-git roots have not been checked (G1b).
- D2b has a blind window on the first dispatch.
- A fresh live session is not verified.
- Owner gates have no SLA.
- The live tree is dirty and has one failed systemd unit.
- No live no-human-prompt E2E.

## 7. Rollback (branch only)
- `git revert dca2287 81e1b8e e9afad4 bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92`.
- For Stage 3, see its report §1.
- Superseded rows are harmless to old code.

## 8. Next (start a clean session from this file)
1. A separately approved adversarial reviewer answers §4 for `9557004..dca2287`.
2. Then the owner-gated promotion and canary plan (Stage 7 §6).
