# Owner OS autonomy — Stage 9 handoff + independent-review packet (2026-11-01)

An automated instruction was received via the Owner OS API (tech lead). It is not owner approval.
The author wrote this packet; it is **not an independent review**.
**No live autonomous E2E has been run.** The Stage 6 rehearsal mocks the agent, the ChatGPT page and the reviewer.
**PROD: NO-GO.**

**Superseded as entry point by `OWNER_OS_AUTONOMY_STAGE10_HANDOFF_2026-11-01.md` (`dca2287`).** This file was the canonical entry point. It supersedes the Stage 8 handoff. For the 7 questions, see `OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md`.

## 1. State
- Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`.
- At the start of Stage 9: HEAD `361e90e`, tree clean.
- Stage 9 fix commit: **`81e1b8e`**. The docs commit follows it.
- Live `/root/ai-dev-runtime` is still at `9557004`. Nothing was promoted.

## 2. Risk addressed: stale stall alert after a late result
**Reproduced (RED):**
- After a stall alert, the attempt's own `done` result arrives.
- The alert stays `pending` in `fe.pending_reviews`, and its wake stays open.
- A later answer is applied as stale.
- Three tests failed before the fix, including the rewritten `test_answering_a_stall_after_the_late_result_is_stale`.

**Fix (`81e1b8e`, `core/fullcycle/engine.py` +24):**
- `_supersede_stall` (`engine.py:790`) is called for a `done` result (`:895`), before verification.
- It marks the stall review of THIS attempt `superseded` (`decided_by='engine'`), only if that review is still `pending`.
- A superseded review:
  - is not applied;
  - leaves the inbox;
  - its undelivered wake retires through the existing `wake_bridge._fullcycle_review_closed` (`core/wake_bridge.py:1723`, which treats any status other than `pending` as closed);
  - is refused by `submit_decision` with `review_superseded` (`engine.py:196-198`).
- Idempotent; emits no event.

**Behaviour that is kept (tests in `tests/test_fullcycle_stall.py`):**

| case | expected | test |
|---|---|---|
| stall with no result | stays pending | `:374` |
| alert already decided | never superseded | `:382` |
| a late `blocked` or `context_exhausted` report reused the slot | stays pending (now a real question) | `:393` |
| result of another attempt | supersedes nothing | `:407` |
| supersede after a late result | done; wake closes; only the real review stays pending | `:344` |
| idempotence | repeated ticks change nothing; no extra events | `:361` |
| late decision on a superseded alert | refused (`review_superseded`); no resend | `:154` |

**Not changed:** wake delivery and failure handling, owner gates, the F1 path. In the F1 path the alert is already decided.

**Contract note for the reviewer:**
- `fc_review.status` gains the value `superseded`.
- No schema change: it is an existing TEXT column.
- Every consumer compares against `pending` or `decided` (`engine.py` inbox, apply, settled; `wake_bridge.py:1723`). The `/opt/seo` MCP reaches reviews only via `api/v1.py` → engine functions (read-only check).
- Confirm that no other reader enumerates statuses.

**Diff:** `reports/stage9/diff_361e90e..81e1b8e.patch`.

## 3. Tests
| run | result | evidence |
|---|---|---|
| `pytest tests/test_fullcycle_stall.py` before the fix | 3 failed, 26 passed | session output |
| same, after the fix | 29 passed | session output |
| focused: fullcycle*, autonomy-cycle, task-contract, context-budget, agent_watch*, wake_bridge*, wake-delivery | **593 passed, exit 0** | `reports/stage9/focused_stall_supersede.txt` |
| full isolated suite at `81e1b8e` (`python3 -m pytest -q -p no:cacheprovider tests/`) | **4316 passed, 4 skipped, 0 failed, exit 0** (628 s) = 4310 + 6 new | `reports/stage9/full_suite_81e1b8e.txt` |

Baselines:
- `e9afad4`: 4310 passed, 4 skipped, 0 failed.
- `bf38344`: 4309 passed, 4 skipped, 0 failed.
- No known baseline failures.

## 4. Independent review still required (Stage 7 findings; not resolved by self-review)
**Commit range for review:**
- `9557004..81e1b8e`, code only: `-- core tools hooks config`.
- Patches: `reports/stage3/`, `reports/stage4/`, `reports/stage5/`, `reports/stage8/diff_f1_*.patch`, `reports/stage9/diff_*.patch`.

**Q1–Q7** (line references in the Stage 7 packet, still valid apart from the shifts listed after the questions):
1. **No second envelope while an attempt may be active.** Fence `_stall_fence_open`; `_clean_session_ready`; held cancel; idle detection by pane text.
2. **Stall alert once per attempt, survives a restart.** This now includes supersede.
3. **No agent self-report used as verification evidence (G1).**
4. **No task stuck forever, never two at once.** Includes F1 (`e9afad4`).
5. **`attempt_in_flight_for_agent` / D2b.** Session match, fail-closed blast radius.
6. **Additive live-DB column `fc_task.unknown_polls`.** Plus the new status value `superseded`.
7. **Stage 3 items.** Hook `hooks/owneros_hook.py:39`, planner, `base_ref`, `job_executor` blocked paths.

Line shifts after Stage 7, as of `81e1b8e`:
- `_stall_fence_open` late-result branch: `engine.py:839-847`.
- `_supersede_stall`: `engine.py:790`.

**Known risks still open:**
- Live idle detection is a pane-text heuristic.
- A long-fenced resend gets no second alert.
- Non-git Full Cycle roots are refused, and live roots have not been checked.
- D2b has a blind window on the first dispatch.
- A fresh live session is accepted on the owner's "resume" without verification.
- Owner gates have no SLA.
- A stall alert is NOT superseded when the attempt crashes instead of delivering a result; its answer is then stale.
- The live tree is dirty, and one systemd unit is failed.
- No live no-human-prompt E2E.

## 5. Rollback (branch only)
- `git revert 81e1b8e e9afad4 bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92`.
- Stage 3: see its report §1.
- Superseded rows are harmless to old code: they are not `pending`, so they are ignored.

## 6. Next action
A separately approved independent reviewer answers Q1–Q7 for `9557004..81e1b8e`. After that, the owner-gated promotion and canary plan in Stage 7 §6.
