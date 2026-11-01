# Owner OS autonomy finish — Stage 4 canonical report & successor handoff (2026-11-01)

Direction: an **automated** Owner OS API message (ChatGPT tech lead), task #328 described there as
owner-approved — not independently verified here, and **not** owner sign-off for anything in this report.
Previous: `reports/OWNER_OS_AUTONOMY_FINISH_STAGE3_2026-11-01.md` (§5 review package, §7 handoff).

## 0. State in one screen

| | |
|---|---|
| Worktree / branch | `/opt/owneros-autonomy` / `owneros/autonomy-finish` |
| Stage 3 end | `ccbcb25` (code) + `a331e45` (docs) — verified: `git log` shows a331e45 on top of ccbcb25; HEAD at session start = `a331e45f953b904a0584c9b6a63f529b30998d9e`, tree clean |
| Stage 4 commits | `9a86a92` owner-gate scope fix, `a4f87e0` regression gate, `5cfd7ab` lease renewal in regression gate, + this report commit |
| PROD | **Untouched.** Live `/root/ai-dev-runtime` not read-for-write, not tested, not restarted. No push/merge/deploy, no grant/route/DB/tmux/browser change. Stray live tab left as is. |
| Approved for PROD | **Nothing.** No independent review yet (this session's notes are self-review). |

## 1. (A) Stage 3 verification

| run | head | result |
|---|---|---|
| full suite, worktree, isolated | a331e45 | 4248 passed, 4 skipped, 0 failed (579 s) — matches Stage 3 §8. Caveat: engine.py was edited mid-run (already imported); the head run below is authoritative |
| full suite, worktree, isolated | 5cfd7ab | **4253 passed, 4 skipped, 0 failed** (576 s) = 4248 + 5 new tests; evidence `reports/stage4/evidence_full_suite.txt` |
| fullcycle + autonomy-cycle + task-contract suites | 5cfd7ab | 121 passed |
| `tests/test_fullcycle_e2e.py` | 5cfd7ab | 18 passed (13 old + 5 new) |

Command (worktree only, never the live tree): `python3 -m pytest -q -p no:cacheprovider tests/`.
Note: `pytest-timeout` is not installed — `--timeout` is rejected (first attempt exit 4, no tests run).

## 2. (B) Acceptance cases — gap ranking (fullcycle engine + wake path)

"Covered" = an offline test drives the real code; the agent, the ChatGPT reviewer and the browser page are
**mocked** in every row (see Stage 3 §4). No row is proven against real ChatGPT by this stage.

| # | case | status before Stage 4 | evidence / action |
|---|---|---|---|
| 1 | stage_done → wake ChatGPT → verified review → next task, no owner poke | covered (mocked reviewer + fake CDP page) | `test_autonomy_cycle_e2e.py::test_stage_done_wakes…`, `::test_spec_to_done…`; real-ChatGPT leg = live canary only |
| 2 | failed QA → specific repair → regression | **GAP: no regression gate** — a later task that broke an earlier verified task passed its own checks, went to review, was approved; caught only by project checks, unattributed | **fixed `a4f87e0`** |
| 3 | dead/stalled agent → exactly-once recovery | crash: covered (restart + redispatch, crash-after-send no double send, lease). **Hang (alive, silent): GAP** — WORKING forever, no attempt deadline | decision D1 (§5) — not self-implemented |
| 4 | duplicate events → single send/task | covered | `test_review_events…duplicate_wake_is_harmless`, `test_a_crash_right_after_the_send…`, idempotent `submit_decision`, `register_delivery` idempotent |
| 5 | waiting_input → safe answer | covered: agent `blocked` → GPT answer → redispatch; permission prompt → owner only, never auto-answered | `test_a_blocked_agent_gets…`, `test_permission_prompt_waits…` |
| 6 | exhausted context → clean session compact handoff | **GAP (fullcycle)**: engine has no context signal; handoff exists only as manual practice + `agent_context_budget` elsewhere | decision D2 (§5) |
| 7 | hazardous owner gate blocks ONLY the hazardous task | **GAP: one gated task halted the whole project** (`_next_task` treated OWNER_DECISION as in flight) | **fixed `9a86a92`** |
| 8 | Full Cycle ack/approve/reject → actual continuation | covered | `test_reviewer_rejection…`, `test_spec_to_done…` |
| 9 | all TZ criteria pass → done | covered: project checks → `project_completed` once; failing → project review → GPT adds tasks | `test_spec_to_done…`, `test_fullcycle_settled.py` |

## 3. (C) Fixes

### 3.1 `9a86a92` — owner gate on one task parks only that task (`core/fullcycle/engine.py`)
* New `_parked(task)`: OWNER_DECISION **and** `last_reason` is a task-scoped dispatch refusal
  (`permission_denied:check_*`, `…push_requires_owner`, `…deploy_requires_owner`) **and** no `fc_dispatch` row
  for the task at all (never possibly reached the agent → nothing of it in the tree).
* `_in_flight(task)` = non-terminal, not CREATED, not parked. `_next_task` uses it (was: any non-terminal non-CREATED).
* `_try_dispatch` waits (`dispatch_waiting_for_other_task`, intent kept) while another task is in flight —
  keeps the single-executor invariant when a parked task is resumed. A parked task stays "parked" until a send
  is recorded, so the in-flight task never waits on it (no deadlock).
* Unchanged: project-wide refusals (root, executor, payment, dirty tree), permission prompts mid-task, crash/
  dispatch-failure blocks still halt the project; dependents of a parked task still wait; owner "resume" is
  still not a waiver (gates re-checked).
* Tests: `test_a_hazardous_task_gate_parks_only_that_task`, `test_a_dependent_of_a_parked_task_still_waits`,
  `test_a_resumed_parked_task_waits_for_the_task_in_flight` (2 failed before the fix, reproduced).

### 3.2 `a4f87e0` — regression gate (`core/fullcycle/engine.py`)
* `_verify` also runs `_regression_checks`: checks of every VERIFIED_DONE task, each with its own trust
  (`gates.check_argv` for reviewer-added ones). Any failure → verification fails → `VERIFICATION_FAILED` →
  bounded retry (`max_attempts`, default 3, then owner gate). Feedback names the broken task and check output.
  Evidence stored under `verification.regression`.
* Off switch `FULLCYCLE_REGRESSION_CHECKS=0`. Cost: O(verified tasks × checks) per verification, each bounded by
  `CHECK_TIMEOUT`. `LEASE_TTL` (900 s) is sized for one task's checks, so **`5cfd7ab`** renews the lease before each
  regressed task; `LeaseLost` mid-verify leaves the task VERIFYING and the next holder re-verifies (existing path).
  Test: `test_the_regression_gate_renews_the_lease_per_verified_task`.
* Test: `test_a_task_that_breaks_an_earlier_verified_task_is_repaired_before_review` (failed before: approved at attempt 1).

## 4. task #328 — agency-agents patterns (third-party reference only; nothing installed, no agents spawned)

Source files were **not fetched in this session**; the mapping uses the tech lead's summary in the instruction.

| concept (source) | verdict | where |
|---|---|---|
| evidence-based task-by-task dev↔QA gate (orchestrator) | **adopted** — existing per-task verify + review, now with regression evidence | 3.2 |
| bounded retries ≤3, never infinite (orchestrator) | already present (`MAX_ATTEMPTS=3`, `MAX_DISPATCH_FAILURES`); kept | engine `_retry` |
| failure taxonomy (multi-agent architect) | **partially adopted**: refusals split into task-scoped vs project-wide | 3.1 |
| watchdog/lease recovery (architect) | lease exists; attempt-level hang watchdog → D1 | §5 |
| per-step checkpoint/handoff (architect) | existing: dispatch intent/latches persisted before each send; context handoff → D2 | §5 |
| idempotent event/task/delivery, one wake → next task (architect) | already present; no change | §2 row 4 |
| typed MCP command/ack/error/timeouts; delivered ≠ submitted (MCP builder) | already present (`cdp_composer` verified delivery vs latched-unverified); no change | Stage 3 §4 |
| scoped expiring grants, fail-closed (identity/trust) | **rejected for this stage** — grants are live/global and owner-gated; instruction forbids altering them | — |
| multi-agent orchestrator / many specialised agents | **rejected** — roles fixed: owner approver, ChatGPT tech lead, Owner OS transport, Claude executor | — |

## 5. Decisions for the tech lead (scoped; work continued without them)

* **D1 — hung agent (case 3).** Proposal: `FULLCYCLE_ATTEMPT_TIMEOUT` (e.g. 2 h) on WORKING; on expiry open ONE
  owner gate `attempt_stalled` (dedup by gate_id) — **no kill**. On owner "resume": if `adapter.owns_agent`,
  restart the pane first, then redispatch attempt+1 (old attempt's result path is ignored). Needs decision because
  killing a pane mid-work and timeouts are concurrency/policy choices.
* **D2 — context exhaustion (case 6).** Proposal: agent result may carry `status:"context_exhausted"` + handoff
  text; engine restarts the pane (fresh session) and redispatches the SAME attempt number's next attempt with the
  handoff as feedback, counted against `max_attempts`. Needs decision: touches pane lifecycle shared with
  agent_watch / context budget modules.

## 6. Risks

* R1 `9a86a92` changes scheduling: two tasks can be non-terminal at once (one parked). Mitigation: send path
  re-checks in-flight; parked = no dispatch record ever. Review the `_parked` predicate first.
* R2 regression gate lengthens verification ticks; lease renewed per task (5cfd7ab), but a single task's checks
  still must fit `LEASE_TTL` as before.
* R3 Every E2E here is mocked on the agent/ChatGPT/browser side. No real ChatGPT wake proof claimed.
* R4 Behaviour change on promotion: tasks that previously sailed through with a broken earlier task now retry.
* Carried from Stage 3: live suite opens a live tab until f27d538 is promoted; stray tab left for the owner.

## 7. Rollback

Branch-only. Revert: `git revert 5cfd7ab a4f87e0 9a86a92` (9a86a92 independent of the other two; 5cfd7ab depends on a4f87e0). Or disable the regression gate with
`FULLCYCLE_REGRESSION_CHECKS=0`. Nothing live to roll back.

## 8. Review packet (self-prepared — NOT an independent review)

* `reports/stage4/diff_production_a331e45..5cfd7ab.patch` (core/fullcycle/engine.py only)
* `reports/stage4/diff_tests_a331e45..5cfd7ab.patch`
* Reviewer questions: (1) is `fc_dispatch` absence a sound "never reached the agent" proof across crash windows
  (row inserted before submit → yes by construction)? (2) `LEASE_TTL` vs regression-extended ticks; (3) should
  `permission_denied:check_*` from a GPT-added task also emit a tech-lead review, not only an owner event?
* Stage 3 §5 package (9557004..ccbcb25) still needs its own independent review.

## 9. Successor handoff

1. `cd /opt/owneros-autonomy && git status && git log --oneline a331e45..HEAD`
2. Read this file; Stage 3 §5/§7 only if needed. Never run tests from `/root/ai-dev-runtime`.
3. Open, in order: independent review (Stage 3 §5 + §8 here) → tech-lead answers D1/D2 → implement D1 → owner-gated
   promotion plan (installer + backup + rollback).
