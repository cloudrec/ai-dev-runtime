# Owner OS integration — three planner/delivery fixes (2026-08-29)

## What was integrated

Branch `integration/owner-os-2026-08-29`, cut from `b30ebf8`
(`ai-runtime/220-windows-bridge`). Three branches merged with `--no-ff`, each
branch's history preserved:

| branch | head | change |
|---|---|---|
| `fix/planner-timeout-harness` | `9ef1bed` | production fix + tests |
| `fix/delivery-attribution-test` | `27f1e70` | test-only |
| `fix/phase13-planner-timeout-test` | `a31353b` | test-only |

**No conflicts.** The three branches touch disjoint file sets, so all three
merges were clean and no resolution judgement was exercised:

```
fix/planner-timeout-harness      core/ai_planner.py, tests/test_planner_fallback.py, reports/…
fix/delivery-attribution-test    tests/test_delivery_attribution.py, reports/…
fix/phase13-planner-timeout-test tests/test_phase13.py
```

Exactly one production file changed across the whole integration:
`core/ai_planner.py`. Everything else is tests and reports.

## Verification

- **Full suite: 2544 passed, 0 failed** (12m22s), against 2542 passed / 2 failed
  on the `b30ebf8` baseline. The +2 are the new planner-salvage regression tests;
  both prior failures are resolved.
- **Windows/bridge checks: 193 passed** (2m18s) — `test_windows_bridge`,
  `test_windows_client`, `test_windows_e2e`, `test_windows_fabric`,
  `test_runtime_bridge`, `test_wake_bridge`. Run separately from the full suite,
  not concurrently: commit `74c00cd` records these modules racing a shared job db.
  `test_windows_fabric` and `test_wake_bridge` are the two bridge modules that
  actually touch the changed surfaces (`ai_planner`, `_deliver`/`agent_send`).

## Residual risks

1. **The salvage path is only proven against a stubbed CLI.** Every test injects a
   fake `claude` binary via `RUNTIME_CLAUDE_BIN`. No test exercises a real
   provider writing a real envelope and then really lingering; that shape is
   inferred from job 86's logs, not observed live. The fallback path is unchanged
   for anything that does not both parse and validate, so the blast radius is
   bounded, but first live confirmation should be watched.
2. **`plan()` still has no overall deadline** — `RUNTIME_PLAN_TIMEOUT` bounds only
   the subprocess, not the prompt-building walk that precedes it. Open owner
   decision; the phase13 fix only removed the pathological `/tmp` input from the
   test, it did not close the gap.
3. **Salvage changes an outcome distribution.** Jobs that previously ended
   `fallback_plan_only` on a lingering planner will now end as real
   implementations. That is the intent, but it means `fallback_plan_only` rates
   and any alerting keyed to them will shift after this ships.
4. **Not validated against `main`.** The integration is based on `b30ebf8`, the
   tip of `ai-runtime/220-windows-bridge`, which carries 20+ unmerged commits.
   Behaviour after a future merge to `main` is unverified.

## State

- Not deployed, not merged to `main`, not pushed. Local commits only.
- Main working tree untouched at `b30ebf8` with its 29 dirty/untracked entries.
- Rollback: `git worktree remove .claude/worktrees/integration --force` and
  `git branch -D integration/owner-os-2026-08-29`. The three source branches are
  independent and survive that.
