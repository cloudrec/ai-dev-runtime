# Owner OS autonomy — Stage 3+4 compact canonical handoff (2026-11-01)

Direction came as **automated** Owner OS API messages (ChatGPT tech lead). None of it is owner sign-off.
Detail: `reports/OWNER_OS_AUTONOMY_FINISH_STAGE3_2026-11-01.md` (§5 review package),
`reports/OWNER_OS_AUTONOMY_FINISH_STAGE4_2026-11-01.md`.

## 1. Code state (verified with git)
- Worktree `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`. Tree was clean before this report.
- Base `9557004` (= live). Stage 3 code end `ccbcb25af651b80bcc2a4efbc4717b7c215a2596`. `a331e45` changes docs only (`git diff ccbcb25 a331e45 -- core tools hooks tests config` is empty).
- Stage 4: `9a86a92` owner gate parks only the gated task · `a4f87e0` regression gate · `5cfd7ab` (`5cfd7aba86ba…`) lease renewed inside the regression gate · `803290f` docs. This file is committed on top.
- **Production unchanged.** Live `/root/ai-dev-runtime` not tested, written or restarted. No push, merge, deploy, grant, route, DB, tmux or browser change.

## 2. Test evidence

| run | command (from the worktree) | result | evidence |
|---|---|---|---|
| full suite at a331e45 (= ccbcb25 code) | `python3 -m pytest -q -p no:cacheprovider tests/` | 4248 passed, 4 skipped, 0 failed, exit 0, 579 s | `reports/stage4/full_suite_a331e45.txt` |
| full suite at 5cfd7ab | same | **4253 passed, 4 skipped, 0 failed, exit 0**, 576 s | `reports/stage4/full_suite_5cfd7ab.txt` |
| focused Stage 3 E2E set (8 files) | Stage 3 report §4 | 140 PASSED (170 s) | `reports/stage3/evidence_focused.txt` |
| fullcycle + autonomy-cycle + task-contract suites at 5cfd7ab | `pytest tests/test_fullcycle*.py tests/test_autonomy_cycle_e2e.py tests/test_task_contract*.py` | 121 passed | session output only, no file |

Evidence gaps (flagged, not claimed as PASS):
- `evidence_focused.txt` records no command, SHA or exit code. Its command is only in the Stage 3 report.
- `engine.py` was edited while the a331e45 run was going (module already imported). The 5cfd7ab run is the clean one.
- The Stage 3 claim "4248 at ccbcb25" has no log of its own. The a331e45 run above reproduces it on identical code.
- The 121-passed focused run has no saved log.

**Mocked vs real.** Every E2E mocks the agent, the ChatGPT reviewer and the CDP page. No real ChatGPT wake/review proof is claimed. Live evidence exists only for the `stage_done` → wake decision leg: events 78155/78167/78237 (Stage 3) and 78336 (Stage 4) got `wake` decisions on route owner-os.

## 3. Decisions and fixes
- Stage 3: 10 commits; see Stage 3 report §1/§2. Base_ref is opt-in and empty. Replan is behind a flag, off by default. A tool-call reply now ends as `blocked`. Ambiguous plans are refused. The suite has a wall against live CDP :9222.
- Stage 4 (`core/fullcycle/engine.py` only):
  - `9a86a92`: a task refused at dispatch for its own reason (`check_*`, push or deploy) that has no `fc_dispatch` row no longer stops independent tasks. Its dependents still wait. Once resumed, it waits for the task in flight.
  - `a4f87e0` + `5cfd7ab`: verification re-runs the checks of every task already verified, renewing the lease before each one. Repair feedback names the task it broke. Retries stay bounded (3). Off switch: `FULLCYCLE_REGRESSION_CHECKS=0`.
- Open tech-lead decisions: **D1** hung agent (attempt timeout), **D2** context exhaustion. Neither is implemented.

## 4. Source-backed review (the 4 agency-agents files, fetched 2026-11-01, read as untrusted reference)

| source rule | our code | verdict |
|---|---|---|
| orchestrator: "Each task must pass QA before proceeding"; max 3 attempts, then escalate | per-task verify + review; `MAX_ATTEMPTS=3` → owner gate; review reject → `VERIFICATION_FAILED` → `_retry` (bounded) | met |
| orchestrator: "Inconclusive evidence: Default to FAIL" | no checks → fail (`task_has_no_checks`). **But** when `git status` fails, scope is checked against the agent's own `changed_files` claim | **GAP G1 (proven)** |
| architect: retried agents must be idempotent; checkpoint before irreversible side effects | `fc_dispatch` row + attempt handle written before submit; review unique per (task, attempt, kind); decision idempotent per key | met |
| architect: loop cap of 3 | attempts bounded; replan max 1 | met |
| architect: HITL gate needs an SLA and defined timeout behaviour | owner gates have no SLA/timeout; the SLO watchdog covers wakes only | open → D1 scope |
| architect: never silently truncate required context | feedback `[:4000]`, check output `[-800:]` / `[-2000:]` are truncated silently | note (low); not changed |
| MCP builder: `isError`, actionable errors, validate at the boundary | not re-audited in this stage (Stage 3 covered the composer); no claim | — |
| identity/trust: "If evidence cannot be written, the action should not proceed" | dispatch record is written before the send | met |
| identity/trust: "Never trust self-reported…" | same as G1 | G1 |
| identity/trust: hash-linked append-only audit, signed delegation | task history is append-only but not hash-linked | rejected (crypto overhaul is out of scope) |

**G1, deterministic defect, NOT fixed (stop-and-report rule).**
- `core/fullcycle/engine.py` `_verify`: `_changed_files()` returns `None` when `git status` exits non-zero. `_verify` then uses `result["changed_files"]` from the agent.
- Probe: a throwaway test, run and deleted, not committed. The agent tampers with out-of-scope `README.md` but claims only `tinycalc/mul.py`, and `git status` fails (rc 128). Result: `passed=True, changed_source=agent_claim, violations=[]`. The task goes to review.
- Proposed fix (not applied): when the git evidence is missing, fail the verification as `changed_files_unverifiable`, which falls into the existing bounded retry and owner gate.
- **Needs tech-lead go-ahead before implementation.**

## 5. Risks
- R1: `9a86a92` lets two tasks be non-terminal at once. Review the `_parked` predicate first.
- R2: the regression gate makes verification longer. The lease is renewed per task, but a single task's checks must still fit within the 900 s lease.
- R3: everything is proven on mocks only.
- R4: after promotion, tasks that break earlier work will retry instead of passing review.
- R5: G1 is open.
- R6: the live suite opens a live browser tab until `f27d538` is promoted. The stray tab is left for the owner.

## 6. Rollback
- Branch only. `git revert 5cfd7ab a4f87e0 9a86a92` (`5cfd7ab` depends on `a4f87e0`; `9a86a92` stands alone).
- Stage 3: revert per commit; see its §1.
- Nothing live to roll back.

## 7. Independent review packet (author's notes are self-review, not independent)
- Stage 4: `reports/stage4/diff_production_a331e45..5cfd7ab.patch` + `diff_tests_a331e45..5cfd7ab.patch`
- Stage 3: `reports/stage3/diff_production_9557004..ccbcb25.patch` + `diff_tests_9557004..ccbcb25.patch`

## 8. Open gates and the next precise task
1. **Next review task:** a reviewer other than the author reviews `engine.py` in `diff_production_a331e45..5cfd7ab.patch`:
   - (a) the `_parked` / `_in_flight` predicate and the wait in `_try_dispatch`, for deadlock or double dispatch;
   - (b) `_regression_checks` trust per owning task, and its lease renewal.

   Then review Stage 3 §5 items 1–5.
2. Tech lead: approve or reject the G1 fix, and answer D1 and D2.
3. Owner: promotion plan (installer + backup + rollback, companion and runtime restarts), then close the stray tab.
