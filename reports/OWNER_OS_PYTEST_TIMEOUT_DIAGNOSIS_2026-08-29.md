# Runtime job test timeout — why full-suite jobs fail, and why retrying is futile (2026-08-29)

Read-only diagnosis. No runtime behaviour, config, credential or schema change.

## The failure

Runtime jobs whose validation runs this repo's suite fail with
`error: tests failed after repair attempts`. The real cause is in the stored
`tests` blob, not the error string:

```
cmd: python3 -m pytest -q   passed: False
tail: Command '['python3', '-m', 'pytest', '-q']' timed out after 600 seconds
```

Confirmed on **7 of 40** failed/cancelled jobs, including tasks **162, 182, 193,
220, 221**.

## Mechanism

1. `ai_planner.default_test_commands()` returns `["python3 -m pytest -q"]` for any
   project with a `tests/` dir or a pytest config — i.e. the **whole suite**, for
   every job against this repo, regardless of how small the change is.
2. `job_executor._run_step()` runs it with `timeout=_TEST_TIMEOUT`.
3. `_TEST_TIMEOUT = int(os.getenv("RUNTIME_TEST_TIMEOUT", "300"))`. The code
   default is 300s and has never changed (introduced once, in `41c0e77`), **but
   the effective value is 600s**: `RUNTIME_TEST_TIMEOUT=600` is set in
   `configs/.env`, the service's `EnvironmentFile`, and is present in the live
   process environment (PID 2872356).
4. The suite no longer fits. Measured this session on the integrated tree:
   **2549 tests, 742s and 832s** across two runs. That is 24-39% *over* the 600s
   cap. The margin is negative and gets worse as tests are added.
5. `subprocess.TimeoutExpired` is caught by the broad `except Exception` in
   `_run_tests`, so the job does not crash — it reports the timeout as an ordinary
   test failure. That is why the surfaced error says "tests failed" and hides the
   real cause one level down.

**This is not a code defect.** Nothing is broken; the suite outgrew its budget.

## Why retrying is futile

A retry re-runs the identical full-suite command under the identical cap and fails
identically. Tasks 220 and 221 each show a `[RETRY]` sibling that failed the same
way. Do not re-dispatch a timed-out full-suite job unchanged.

## Options — all require an owner gate, none taken here

| Option | Change | Gate |
| --- | --- | --- |
| Raise the cap (e.g. `RUNTIME_TEST_TIMEOUT=1200`) | `configs/.env` | deployed-service config + restart |
| Scope the derived command to the touched area instead of the full suite | `core/ai_planner.default_test_commands` | code change + deploy + restart |
| Parallelise (`-n auto`) | needs `pytest-xdist` | install + deploy |

Raising the cap is the smallest and most honest: the suite genuinely needs ~850s
today, so 1200s restores headroom without weakening validation. Scoping the
command is the better long-term fix but changes what "validated" means for a job,
which is a policy decision, not an implementation one.

## Correction recorded

An earlier statement in this session that "the cap is 300s, the 600s figure is a
misattribution" was **wrong**. It came from reading only `systemctl show -p
Environment` and missing the `EnvironmentFile=/root/ai-dev-runtime/configs/.env`.
The 600s in `reports/OWNER_OS_WINDOWS_BRIDGE_AND_EXPLICIT_MODEL_ROUTING_2026-08-27.md`
was correct.

## Related, separate

Jobs **84** (`43b32aeb`) and **86** (`7cc48c98`) are a different failure:
`error: planner timed out`, both the same goal ("Global Claude Context Lifecycle
Manager"), both 2026-07-14/15, both with an EMPTY plan. Those are pre-fix
instances of the planner-salvage defect
(`reports/OWNER_OS_PLANNER_TIMEOUT_SALVAGE_2026-08-29.md`), not test-timeout
failures. Both are already satisfied; neither is to be retried.
