# Runtime planner — a delivered plan discarded on process timeout (job 86, 2026-08-29)

## The incident

Runtime job `7cc48c98-2e94-44d8-bb4f-6c465f2663a3` (`task_id=86`, "Global Claude
Context Lifecycle Manager: save handoff, rotate context, restore project state")
was approved on 2026-07-15 and died three minutes later:

```
09:33:46  planning via AI provider
09:34:07  planning… still running (20s elapsed)
   …      (heartbeats every 20s)
09:36:47  planning failed: planner timed out
```

`status=failed`, `error="planner timed out"`, no plan, no outcome. The task itself
is **already satisfied** by later work, so the job was never retried — but the
harness defect that ate it was still live and unguarded.

## Root cause

`core/ai_planner.plan()` checked the process deadline *before* it ever looked at
what the provider had written:

```python
stdout, stderr, timed_out, returncode = _invoke_cli(...)

if timed_out:
    raise PlannerError("planner timed out", timed_out=True)   # stdout dropped here
```

The deadline (`RUNTIME_PLAN_TIMEOUT`, default 180s) is enforced on the **process**,
not on the work. The CLI can write a complete, valid result envelope and only then
linger past the deadline — slow teardown, or a detached grandchild still holding
the pipe — at which point `_invoke_cli` kills the whole process group and reports
`timed_out=True`. The bytes were already drained and sitting in `stdout`. The old
code threw them away unread.

Two consequences, both silent:

1. **A satisfiable job is downgraded to a non-implementation.** `job_executor`
   catches the `PlannerError` and builds the deterministic fallback plan, so the
   job ends `fallback_plan_only` — which `core/job_kinds` correctly treats as
   "a PLAN was recorded, the task is NOT implemented", and which
   `release_controller` refuses to release. Real planner output, discarded, and
   the job reports as not-implemented work.
2. **The spend is reported as unknown.** The timeout error was raised with no
   accounting, so the provider's `usage`/`total_cost_usd`/`duration_ms` — present
   in the delivered envelope — were lost from the job artifacts.

The existing regression test `test_timeout_falls_back_and_reaches_coding_stage`
only ever exercised a planner that produced **nothing** (`time.sleep(30)`), so the
delivered-then-lingered case had no coverage at all.

## Reproduction (before the fix)

A fake CLI that writes a complete valid plan envelope, flushes, then sleeps past
the deadline, run through `job_executor.execute` with job 86's shape
(`allowed_paths=[]`, `autonomy_level=execute_safe`, `task_id=86`):

```
status  = fallback_plan_only
outcome = fallback_plan_only
summary = [fallback] deterministic local plan for: Global Claude Context Lifecycle Manager
changed = reports/runtime/fallback/PLAN-86-global-claude-context-lifecycle-manager.md
```

The provider's own plan (`summary: "real plan from provider"`, file op `NOTES.md`)
never reached the pipeline.

## The fix

`core/ai_planner.plan()` — parse the envelope first, then treat the timeout as a
question about the *output* rather than about the process:

- On `timed_out`, if stdout holds something and the envelope is not an error
  envelope, try `_extract_json` + `_validate`. A plan that **parses and validates**
  is used exactly as a normal plan; nothing downstream can tell the difference,
  and both `plan()` call sites in `job_executor` already handle it unchanged.
- If it does not validate, fall through to the original
  `PlannerError("planner timed out", timed_out=True)` — a timeout with nothing
  usable is still a planner failure, and still routes to the deterministic
  fallback. Salvage never swallows a real timeout.
- That error now carries the parsed accounting (`**acct`), so a delivered-but-
  unusable envelope no longer reports its token/cost/timing as unknown.

The `returncode != 0` check is deliberately skipped on the salvage path: a killed
process group always reports failure, which says nothing about whether the bytes
it already wrote are a good plan.

After the fix, the same repro reaches the coding stage on the provider's real plan:

```
status  = completed
outcome = context_restored
summary = real plan from provider
changed = NOTES.md
```

## Tests

Added to `tests/test_planner_fallback.py`:

- `test_plan_delivered_before_timeout_is_salvaged_not_discarded` — job-86 shape;
  asserts the provider plan is used, no fallback markers/artifacts, the provider's
  own file op reaches the coding stage, and the planner is still called **exactly
  once** (salvage is not a retry). *Fails on baseline.*
- `test_timeout_error_preserves_accounting_when_envelope_was_delivered` — an
  unsalvageable timeout still reports `timed_out` **and** the real cost/tokens.
  *Fails on baseline.*
- `test_timeout_with_no_usable_output_still_falls_back` — truncated mid-plan output
  still ends `fallback_plan_only` with `fallback_timed_out=True`. Guards against
  over-salvaging; passes both before and after, by design.

## Scope / state

- Branch `fix/planner-timeout-harness`, isolated worktree
  `.claude/worktrees/planner-timeout-harness`, baseline `b30ebf8`.
- Rollback: `git worktree remove .claude/worktrees/planner-timeout-harness --force`
  and `git branch -D fix/planner-timeout-harness`. Nothing outside the worktree was
  touched; the main tree's dirty + untracked reports are untouched.
- Local commit only. No push, no deploy, no restart, no external messaging.
- Job 86 itself was **not** retried — it is already satisfied. Only the harness
  defect that caused its failure was fixed.

## Full-suite result

`pytest -q -p no:randomly` → **2542 passed, 2 failed** (18m40s).

Both failures are **pre-existing on baseline `b30ebf8`** — verified by reverting
only `core/ai_planner.py` + `tests/test_planner_fallback.py` and re-running the
two in isolation, where they fail identically. Neither is caused by this change:

- `tests/test_delivery_attribution.py::test_agent_send_threads_attribution_to_the_record`
  — unrelated (agent_send attribution record).
- `tests/test_phase13.py::test_planner_hanging_parent_with_no_children_still_times_out`
  — `assert elapsed < 10` fails at `13.98`. **Same harness, adjacent defect**:
  `_invoke_cli` enforces the deadline on the wait loop, then unconditionally does
  `t_out.join(timeout=5)` + `t_err.join(timeout=5)`. When the killed child leaves
  the pipe open, those joins add up to 10s *after* the deadline, so the planner's
  effective wall time is `timeout + 10s`, not `timeout`. Left untouched here to
  keep this fix minimal; tracked as the next item.
