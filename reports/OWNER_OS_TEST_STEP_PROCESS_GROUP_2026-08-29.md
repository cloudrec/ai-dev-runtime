# Test-step process-group leak — staged, not deployed (2026-08-29)

Branch `fix/test-step-process-group`. **Code is staged at
`c0b6bfa0dfe39074ff0927cfe70b11dd219ab783` and pinned there** — this report is a
docs-only commit on top, so the code tree is byte-identical to `c0b6bfa`. Nothing
pushed, merged, deployed or restarted. Deployed line remains `5618ce3`.

## The defect

`subprocess.run(timeout=...)` kills **only the direct child**. Every process the
step had spawned survives. Reproduced directly this session: a deliberately
spawned grandchild was still alive after the parent was killed on timeout.

Two live call sites carried it, both running `python3 -m pytest -q` (the FULL
suite) under the same `RUNTIME_TEST_TIMEOUT`:

| Module | Entry point | Code default |
| --- | --- | --- |
| `core/job_executor._run_step` | runtime job validation | 300 |
| `core/deliver._run_tests` | `POST /api/v1/deliver` (PHASE 17 merge->test->push) | 180 |

Effective cap is **600s** (`configs/.env` line 61, live in PID 2872356). The suite
measured **742s, 832s and 1171s** this session — it exceeds the cap routinely, so
this is not a rare path. Seven of 40 failed jobs carry
`Command '['python3', '-m', 'pytest', '-q']' timed out after 600 seconds`
(tasks 162, 182, 193, 220, 221). Each of those could have orphaned processes that
nothing later reaps — and this repo's own suite spawns long-lived CLI stubs,
including a SIGTERM-ignoring grandchild in `_invoke_cli`'s tests.

## The fix

Both sites now run the step via `Popen(..., start_new_session=True)` so it leads
its own process group, and on timeout kill the group SIGTERM-then-SIGKILL (5s wait
each) before **re-raising the original `TimeoutExpired` unchanged**. Same
discipline `ai_planner._kill_process_group` already used.

Deliberately unchanged: the timeout value, the commands run, pass/fail semantics,
and the recorded error text. `_run_tests` stores `str(e)`, and
`str(TimeoutExpired)` is only `"Command '[...]' timed out after N seconds"` —
`e.output` is `None` even today (verified), so re-raising loses no diagnostic
detail. `deliver` keeps `cmd.split()` rather than `shlex.split()`: switching would
change which commands parse, a separate behaviour change, not part of a leak fix.

* `ce135ad` — `core/job_executor.py` +45/-3, `tests/test_job_executor.py` +41.
* `c0b6bfa` — `core/deliver.py`, plus `tests/test_deliver_test_runner.py`, the
  first coverage this module has had.

## Verification

* Full suite on this branch: **2550 passed, 0 failed** (1170.99s).
* Delivery + executor gate: **110 passed**. Deliver module alone: 4 passed.
* Mutation, both sites: restoring the `subprocess.run` implementation leaves the
  grandchild alive and fails the reap test. Production files restored clean after
  each mutation.
* `deliver` tests also pin that a timeout still surfaces as an ordinary failed
  result rather than escaping into the delivery flow.

## Rollback

Nothing to roll back — never deployed. If it later is: existing tag
`rollback/pre-planner-salvage-20260829T162442Z` -> `b30ebf8` plus
`backups/predeploy_planner_salvage_20260829T162442Z/`. Both changes are two
functions in two files; **no schema, config, credential or policy change**, so
restoring the two files is sufficient. Restore files rather than `reset --hard`,
which would discard the 29 unrelated dirty `reports/*`.

## Owner gate

Landing this needs merge + push + `systemctl restart ai-runtime.service` (6
workers). Not authorized; not done.

**The `RUNTIME_TEST_TIMEOUT` 600->1200 raise is withdrawn** — the suite measured
1171s, leaving 2.5% headroom, so that number is not defensible. See
`OWNER_OS_TEST_TIMEOUT_DEPLOY_PROPOSAL_2026-08-29.md`. The durable fix is scoping
`ai_planner.default_test_commands()` so a job does not run 2550 tests to validate
one module — that changes what "validated" means per job and is an owner policy
decision, explicitly not taken here.
