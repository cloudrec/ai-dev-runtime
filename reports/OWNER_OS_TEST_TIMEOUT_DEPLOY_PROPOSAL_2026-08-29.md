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

## Unaffected

Telegram `notifications_red` / dead-letter stays credential-gated and untouched.
The planner-salvage change is already live (`8dcaeea`) and unrelated to this.
