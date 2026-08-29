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
  — `assert elapsed < 10` fails at `13.98`. Measured, not assumed: `_invoke_cli`
  returns in **2.04s**, correctly bounded by its 2s deadline. The kill path and
  the drain joins are not implicated. The extra ~12s is spent *before* the
  subprocess ever starts, in `plan()`'s prompt file listing — `os.walk` over the
  test's `project_path`, which is `/tmp`. On this host `/tmp` holds 2927
  top-level entries, and a single `scandir` of it costs 5-8s.

  The real finding is that **`plan()` has no overall deadline**:
  `RUNTIME_PLAN_TIMEOUT` bounds only the planner subprocess, so the prompt-
  building walk that precedes it is unbounded. A project path on a slow or very
  large filesystem delays a job before the timeout clock even starts.

  Deliberately NOT fixed here. Two candidate fixes were tried and rejected on
  measurement: pruning excluded directories in place (`dirs[:] = ...`, so a large
  `.git`/`node_modules` is not descended into) and a ceiling on directories
  visited. Neither helps this case — the walk visits only **25** directories and
  the cost is enumerating one huge directory, which no dir-count cap avoids. On a
  real repo path the existing walk already costs 0.054s, so the pruning change
  was a micro-optimization, not a defect fix, and was reverted rather than
  committed as unrequested scope. A correct fix needs a wall-clock budget on the
  listing step; that is a real change to `plan()`'s contract and wants its own
  task. This failure is host-dependent and does not gate the job-86 fix.

---

## Observability gap in this fix (recorded 2026-08-29, NOT yet fixed)

**The salvage branch emits no log line and no artifact.** When `plan()` recovers a
plan the provider delivered before the deadline, the job record is
indistinguishable from an ordinary slow-but-successful plan: same status, a real
non-fallback plan, no marker. The failure path is well instrumented — the fallback
branch logs `planner failed (...) — building deterministic fallback plan` and
appends a `fallback_planning` artifact — but the success-after-timeout path is
silent.

Consequence: *"did salvage ever fire in production?"* cannot be answered from the
job records directly. It can only be inferred.

### Inference method used meanwhile (read-only)

Signature: **max planner heartbeat >= the full `RUNTIME_PLAN_TIMEOUT` (180s)**, no
`planner failed` log, and a real plan with `fallback != True`. Salvage can only
occur at or past the deadline; a plan that returns before it is a normal success.

Calibrated against all 105 historical jobs in `runtime_jobs.db`:

| threshold | historical matches |
| --- | --- |
| >= 100s | 8 — all false positives (pre-fix, salvage was impossible) |
| >= 180s | **0** |

The highest heartbeat ever recorded on a successful pre-deadline plan in this db
is **160s**, so 180s separates cleanly and a hit will be real. Jobs 88, 94, 108,
113, 118 are the slow-but-successful cases that a 100s threshold wrongly caught.

Job 86 itself classifies as `normal`, not salvage — correct: under the old code it
hard-failed with an empty plan. It is the case the fix addresses, not an instance
of the fix working.

### The rigorous fix (deliberately not done here)

One log line plus an artifact on the salvage branch — mirroring what the fallback
branch already records — would make this directly observable instead of inferred.
That is a change to `core/ai_planner.py`, so it needs another merge, deploy and
`systemctl restart ai-runtime.service`: an owner gate. Recorded here rather than
taken. Until then, treat any salvage claim as inference from heartbeat timing, not
as direct evidence.

### Live status at time of writing

Deployed and healthy (`8dcaeea` / `c4bb6ad`, service PID 2872356, `NRestarts=0`,
zero errors, 10/10 flat soak samples 16:37-16:42Z). **Salvage has not yet fired in
production** — zero runtime jobs since the 16:26Z restart, and job traffic runs
1-4/day. The `fallback_plan_only` count is unchanged at 9 through absence of
traffic, not through disproof. A read-only watcher is armed on the 180s signature.
