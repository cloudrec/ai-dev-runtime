# Proposal awaiting owner decision — test-step process group + pytest cap (2026-08-29)

Nothing here is applied. No config change, no restart, no push, no merge, no
deploy. This records exactly what would happen if authorized.

## Change 1 — code: `ce135ad`

Branch `fix/test-step-process-group`, worktree `.claude/worktrees/test-step-killgroup`,
based on deployed `5618ce3`. `core/job_executor.py` +45/−3, `tests/test_job_executor.py` +41.

`_run_step` moves from `subprocess.run(timeout=)` to
`Popen(..., start_new_session=True)` + `communicate(timeout=_TEST_TIMEOUT)`. On
timeout it calls a new `_kill_step_group()` (SIGTERM, then SIGKILL, each with a 5s
wait), drains with a bounded second `communicate(timeout=10)`, then **re-raises the
original `TimeoutExpired` unchanged**.

**Why.** `subprocess.run(timeout=)` kills only the direct child. A `pytest` killed
at the cap left everything its tests had spawned running on the server. Proven
empirically in this session: a deliberately spawned grandchild survived the
timeout. Real exposure — this repo's own suite starts long-lived CLI stubs
(`sleep 30` fake providers; `_invoke_cli`'s tests spawn a SIGTERM-ignoring
grandchild), and 7 of 40 failed jobs hit the cap on the full suite.

**Behaviour deliberately preserved:** the timeout value, validation policy, schema,
credentials, and the recorded error text. `_run_tests` stores `str(e)`, and
`str(TimeoutExpired)` is only
`"Command '[...]' timed out after N seconds"` — `e.output` is `None` even today
(verified), so no diagnostic detail is lost by re-raising.

Mirrors the kill-group discipline `ai_planner._kill_process_group` already uses.

## Change 2 — config: the cap

`configs/.env` line 61, the service's `EnvironmentFile`:

```
RUNTIME_TEST_TIMEOUT=600      ->      RUNTIME_TEST_TIMEOUT=1200
```

Live value in PID 2872356 is currently `600` (confirmed from `/proc/.../environ`).
The code default in `job_executor.py` is `300` and is overridden by this file.

**Why 1200.** The suite is 2549 tests and measured **742s** and **832s** twice this
session — 24–39% over 600s. 1200s restores headroom without weakening validation.

## Verification already done

* Focused gate: **102 passed** — `job_executor`, `planner_fallback`, `phase13`,
  `fallback_truthfulness`, `job_kinds`, `job_workspace`.
* Process leak reproduced directly before the fix; gone after.
* Mutation: restoring the old `subprocess.run` implementation leaves the
  grandchild alive and fails `test_timed_out_step_reaps_its_grandchildren`.
* Field evidence for the cap: 7 of 40 failed jobs carry
  `Command '['python3', '-m', 'pytest', '-q']' timed out after 600 seconds`
  (tasks 162, 182, 193, 220, 221).

**Not done: no full-suite run on this branch.** Only the focused gate. A full run
should precede or accompany any deploy.

## Rollback

Existing point: tag `rollback/pre-planner-salvage-20260829T162442Z` -> `b30ebf8`,
plus `backups/predeploy_planner_salvage_20260829T162442Z/` (4 db copies +
`ROLLBACK.md`). A fresh pre-deploy point would be cut first.

Code rollback is one file; config rollback is one line back to `600`. **No schema
change either way**, so rollback needs no migration. Restore the single file
rather than `git reset --hard` — the latter would discard the 29 unrelated dirty
`reports/*` files.

## Restart impact

`ai-runtime.service` only. `WorkingDirectory=/root/ai-dev-runtime`, so the checkout
IS the deploy and the restart is what activates it. `configs/.env` is read at
process start, so **the cap change needs the same restart** — it is not hot.
Restart interrupts 6 workers; there are currently **0 in-flight jobs**.

## Remaining risk

1. **The cap raise is mitigation, not a cure.** The suite keeps growing and will
   re-cross 1200s. The durable fix is scoping
   `ai_planner.default_test_commands()` instead of always returning
   `python3 -m pytest -q`, which changes what "validated" means per job — a
   policy decision, not an implementation one. Not proposed here.
2. **No full-suite validation on this branch yet** (see above).
3. `_kill_step_group` returns early on `PermissionError`, skipping SIGKILL. Only
   reachable if the group is not ours; acceptable, and it fails safe (no kill
   rather than a wrong kill).
4. The bounded post-kill `communicate(timeout=10)` can add up to 10s to a timing-out
   step if a killed grandchild still holds the pipe. Bounded, and only on the
   already-failing path.
5. Batching is recommended: the cap raise makes the timeout rarer, `ce135ad` makes
   it harmless when it still fires. Neither substitutes for the other, and
   shipping them separately costs two restarts of a live control loop.

## CORRECTION 2 — 1200s is NOT a safe cap (measured 2026-08-29, post-draft)

The full suite was finally run on this branch. Result: **2550 passed in 1170.99s
(19m30s)** — 29 seconds under the 1200s I recommended.

Three measurements of the same suite this session:

| Run | Tests | Duration |
| --- | --- | --- |
| integration branch | 2549 | 742s |
| integration branch | 2549 | 832s |
| `fix/test-step-process-group` | 2550 | **1171s** |

The suite is not ~850s; it is **742-1171s depending on machine load** (the slow run
competed with the live service and other agents). Duration varies by 58% for
essentially the same test count, so a cap must be sized against the worst
observed case under load, not the median.

**1200s gives 2.5% headroom over the worst run and would re-time-out almost
immediately.** The earlier "1200s restores headroom" claim was based on the two
fast runs only and is withdrawn.

Revised options, in order of preference:

1. **Fix the cause, not the symptom — scope the derived test command.**
   `ai_planner.default_test_commands()` returns the whole suite for every job
   however small the change. A job touching one module does not need 2550 tests.
   This is the only option that does not degrade as the suite grows. It changes
   what "validated" means per job, so it is an owner policy decision.
2. **If a cap raise is still wanted, 1200 is too low.** `2400` gives ~105%
   headroom over the worst observed run. Note this doubles how long a genuinely
   hung step occupies a worker before being killed — and, per CORRECTION 1, it
   applies to `core/deliver.py`'s live merge-gate too.
3. Do nothing to the cap and land `ce135ad` alone. Timeouts keep happening at the
   current rate, but they stop leaking processes. This is the smallest, safest
   step and it strictly improves on today.

**Recommendation changed to (3) now, (1) next**, rather than the original batched
cap raise. `ce135ad` is independently correct and load-insensitive; the cap number
is not something this evidence can pin down confidently.

## CORRECTION — the cap change has a second consumer (found 2026-08-29, post-draft)

The section above describes the cap as "one line, one value". The *edit* is, but
the **blast radius is two subsystems, not one**. `RUNTIME_TEST_TIMEOUT` is read by
two independent modules:

| Module | Line | Code default | Purpose |
| --- | --- | --- | --- |
| `core/job_executor.py` | 29 | `300` | runtime-job validation (fixed by `ce135ad`) |
| `core/deliver.py` | 16 | **`180`** | PHASE 17 merge -> test -> push gate |

`core/deliver.py` is **live**, not dead code: `api/v1.py:277` exposes
`POST /api/v1/deliver`, which calls `deliver_mod.deliver(...)`.

Three consequences the owner should weigh before authorizing:

1. **Raising the cap to 1200 silently also raises the delivery gate's timeout.**
   That is probably desirable — but it is a second behaviour change, not a
   side-effect-free config tweak, and it was not stated in the original proposal.
2. **`deliver._run_tests` has the SAME process-group leak** that `ce135ad` fixes.
   `ce135ad` touches only `job_executor`, so after that deploy the leak still
   exists on the delivery path. Its default test command is `python3 -m pytest -q`
   — the full suite — so it is exposed to exactly the same timeout.
3. **The two modules disagree on the code default** (300 vs 180) for the same
   env var. Only matters when the env is unset, but it means "the default cap"
   is ambiguous depending on which subsystem you mean.

Additionally `deliver._run_tests` splits its command with `cmd.split()` rather
than `shlex.split()`, so a quoted argument would tokenize differently than the
same command run through `job_executor`. Cosmetic today (the default command has
no quoting), noted so it is not rediscovered as a bug later.

**Not fixed here.** Extending `ce135ad` to `core/deliver.py` would enlarge the
pending deploy, so it is recorded rather than taken. If the batched deploy is
authorized, the honest options are (a) land `ce135ad` as-is and accept that the
delivery path keeps the leak, or (b) authorize a follow-up commit that applies
the same kill-group fix to `deliver._run_tests` so both consumers of the raised
cap are safe. (b) is recommended — a raised cap means a timing-out delivery now
leaks processes for twice as long.

## Unaffected

Telegram `notifications_red` / dead-letter stays credential-gated and untouched.
The planner-salvage change is already live (`8dcaeea`) and unrelated to this.
