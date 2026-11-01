# Owner OS autonomy — Stage 3–5 canonical handoff (short) — 2026-11-01

All direction was automated Owner OS API instructions (ChatGPT tech lead). **Nothing here is owner approval.**
This file supersedes `OWNER_OS_AUTONOMY_STAGE3_4_HANDOFF_2026-11-01.md` as the entry point.

## State (HEAD after Stage 6 docs: see `git log -1`; code unchanged since bf38344)
- Worktree `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`. Base `9557004` = live.
- **PROD unchanged.** No push, merge or deploy. No change to grants, secrets, the global DB, the live browser, tmux or other sessions.
- Code SHAs:

| stage | commit | what |
|---|---|---|
| 3 | `ccbcb25` | Stage 3 code end |
| 4 | `9a86a92` | owner gate on one task parks only that task |
| 4 | `a4f87e0` | regression gate |
| 4 | `5cfd7ab` | lease renewed inside the regression gate |
| 5 | `eb3b30d` | G1 |
| 5 | `9fb024a` | D1 |
| 5 | `bf38344` | D2 + D2b |

- Docs commits follow the code commits. `git log --oneline 9557004..HEAD` lists them.

## Tests (worktree only; never run from `/root/ai-dev-runtime`)
All runs use `python3 -m pytest -q -p no:cacheprovider tests/`.

| code SHA | result | log |
|---|---|---|
| a331e45 (= ccbcb25 code) | 4248 passed, 4 skipped, 0 failed, exit 0 | `reports/stage4/full_suite_a331e45.txt` |
| 5cfd7ab | 4253 passed, 4 skipped, 0 failed, exit 0 | `reports/stage4/full_suite_5cfd7ab.txt` |
| **bf38344** | **4309 passed, 4 skipped, 0 failed, exit 0** | `reports/stage5/full_suite_bf38344.txt` |

- Focused runs at bf38344: fullcycle + autonomy-cycle + task-contract + context-budget + agent_watch = 457 passed.
- Mutation checks: removing the D1 fence fails 3 tests; removing the D2 fence and D2b guard fails 3 tests.
- **Mocked in every E2E:** the agent, the ChatGPT reviewer, the clock and the browser.
- **Real:** only the live stage_done → wake decision (events 78155 … 78391).

## G1 / D1 / D2 — implemented per the tech-lead decisions
Detail is in `reports/OWNER_OS_AUTONOMY_STAGE5_G1_D1_D2_2026-11-01.md` §1.
- **G1 (`eb3b30d`):** a git status or HEAD error fails closed (`changed_files_unverifiable`, `git_status_unavailable`, `git_head_unavailable`). Full Cycle refuses non-git roots (`project_root_not_git`). A failed `git add` never marks a task committed.
- **D1 (`9fb024a`):** at 7200 s, or after 20 UNKNOWN polls in a row, ONE `blocked`/`stalled` review per attempt goes to the tech lead.
  - The engine never kills, restarts or resends by itself.
  - A resend or cancel waits until the old attempt is proven over: late result first, then `quiescent()` True.
  - Max 3 attempts.
- **D2 (`bf38344`):**
  - `context_exhausted` + a validated handoff go to a tech-lead review.
  - A clean session starts only after a safe stop. The test adapter restarts the pane; a live agent goes through the owner gate `clean_session_required`, because grants have no session-lifecycle action.
- **D2b (`bf38344`):** context budget never checkpoints or `/clear`s a pane with a Full Cycle attempt in flight. Fails closed.

## Unresolved (explicit)
1. **Non-git Full Cycle roots:** refused. Live roots not inspected; check read-only before promotion.
2. **Stale stall review:** after a late result the alert stays pending in the inbox; an answer is applied as stale.
3. **Unverified fresh-session identity:** a live clean session is accepted on the owner's "resume" without checking it is new.
4. **Live idle evidence:** relies on pane-text classification (`waiting_input`).
5. **No escalation for a long-fenced resend:** it waits silently after its one alert.
6. **D2b blind window:** the first dispatch, before the adapter returns its handle.
7. **On promotion, live DB change:** the additive `ALTER TABLE fc_task ADD COLUMN unknown_polls` hits the live control-plane DB, so back it up.
8. **No live no-human-prompt E2E.** Final autonomy is NOT claimed.

## Rollback
- Branch only. Revert newest first: `git revert bf38344 9fb024a eb3b30d 5cfd7ab a4f87e0 9a86a92`.
- Stage 3: see its report §1.
- Nothing live to roll back.

## Independent critical review request (for a separately approved reviewer; not self-review)
**Scope (bounded):** `core/fullcycle/engine.py`, `core/fullcycle/tmux_adapter.py`, `core/fullcycle/executor.py`, `core/agent_context_budget.py`.

**Diffs:**
- Stage 5: `reports/stage5/diff_production_b7d0da1..bf38344.patch`
- Stage 4: `reports/stage4/diff_production_a331e45..5cfd7ab.patch`
- Stage 3: `reports/stage3/diff_production_9557004..ccbcb25.patch`

**Tests:** the matching `diff_tests_*` patches. Full-suite logs as above.

**Questions to answer, each with a file:line verdict:**
1. Can any path send a second envelope while the previous attempt may be active? Check:
   - `_try_dispatch` → `_stall_fence_open` / `_clean_session_ready`
   - the held cancel in `_step_working`
   - `_owner_answers` resume after `clean_session_required`
2. Can the stall alert fire more than once per attempt, or be lost across an engine restart?
3. Is any agent self-report still used as verification evidence (G1)?
4. Do `_parked` / `_in_flight` (Stage 4) interact with D1/D2 states so that two tasks run, or a task is stuck forever?
5. `attempt_in_flight_for_agent`: correct session match; fail-closed blast radius on live rotation.
6. Schema migration safety on the live control-plane DB.
7. Stage 3 package §5 items 1–5 (hook `sys.path`, composer, `job_executor` blocked paths, planner, `git_write` base_ref).

**Out of scope:** policy changes to grants, owner gates, timeouts.

**Expected output:** CONFIRMED / PLAUSIBLE / REJECTED per question, with file:line.

## Stage 6 (isolated rehearsal + preflight) — `reports/OWNER_OS_AUTONOMY_STAGE6_REHEARSAL_2026-11-01.md`
- Rehearsal `tests/test_autonomy_cycle_e2e.py` at code bf38344: **7 passed, exit 0**. Evidence is in `reports/stage6/rehearsal_autonomy_cycle_b968696.txt`.
- Mocked: the agent, the ChatGPT page (fake CDP) and the reviewer. Everything else in the loop is real code, running on temp DBs and private tmux.
- Live preflight (read-only):
  - live tree at `9557004`, dirty;
  - live does not contain the branch code;
  - companion, chromium and fullcycle-worker are active;
  - CDP is listening but was not contacted.
- A real ChatGPT E2E is **blocked at owner gates**: review → promotion → canary route binding. Not simulated; not claimed.

## Stage 7 review packet — `reports/OWNER_OS_AUTONOMY_STAGE7_REVIEW_PACKET_2026-11-01.md`
- The 7 review questions now have exact line references.
- **New author finding F1 (CONFIRMED by a throwaway probe, not fixed):**
  - Sequence: a stall is answered when the task is already at max_attempts → owner "resume" → late result arrives.
  - Outcome: the task stays BLOCKED forever and the late result is never verified. No double send.
  - The fix needs tech-lead approval.

## Next
1. Independent review (above).
2. Tech lead resolves the open items.
3. Owner-gated promotion plan: installer, backup of the control-plane DB, rollback, companion/runtime restarts, read-only check of live roots.
4. Live no-human-prompt E2E canary (owner-approved).
