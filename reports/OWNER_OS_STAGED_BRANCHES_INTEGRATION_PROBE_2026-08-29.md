# Staged branches — combined-merge probe (2026-08-29)

Read-only probe. No staged branch was modified; nothing pushed, deployed or
restarted. Deployed line remains `5618ce3`.

## Correction

These three were reported as "independent". **They are not.** Two of them modify
the same file:

| Branch | Head | `core/` files touched |
| --- | --- | --- |
| `fix/test-step-process-group` | `d7749a9` (code pinned at `c0b6bfa`) | `job_executor.py`, `deliver.py` |
| `feat/salvage-observability` | `80d66d5` | `job_executor.py`, `ai_planner.py` |
| `fix/windows-command-expiry-on-read` | `62df2dc` | `windows_bridge.py` |

`core/job_executor.py` is touched by two branches. That is an integration risk
worth settling before a deploy, not at one.

## Probe method

A throwaway **detached** worktree at `5618ce3`, merged all three in order. No
branch ref was moved, so this is non-destructive and leaves no trace once the
worktree is removed.

## Result: they compose cleanly

* All three auto-merge with **no conflict**; `core/job_executor.py` merged
  automatically.
* The two `job_executor` edits are in **disjoint regions with no shared state**:
  the test-runner (`_run_step` / `_kill_step_group`, ~lines 214-260) versus the
  planning stage (`salvaged_after_timeout`, ~line 576).
* Combined delta vs `5618ce3`: `ai_planner.py` +16, `deliver.py` +48/-4,
  `job_executor.py` +61/-3, `windows_bridge.py` +22.
* **Full suite on the combined tree: 2562 passed, 0 failed** (1117s). That is
  exactly the arithmetic: 2549 deployed + 5 (process-group: 1 executor + 4
  deliver) + 4 (salvage observability) + 4 (windows expiry).

## What this does and does not establish

Establishes: any subset of the three can be landed, in any order, without merge
conflict or test regression, and the combined tree is green.

Does **not** establish: that any of them should be deployed. Each still needs
merge + push + `systemctl restart ai-runtime.service` — an owner gate not given.
Nor does it re-open the withdrawn `RUNTIME_TEST_TIMEOUT` 600->1200 raise, which
stays withdrawn on the evidence that the suite measured 1171s.

## Runtime timing note

The combined suite took 1117s — again above the 600s cap the runtime enforces on
job validation. Consistent with the 742/832/1171/1117s spread already recorded:
the suite's duration is load-dependent and routinely exceeds the cap. That is the
condition `fix/test-step-process-group` exists to make harmless.

---

# Re-probe with the fourth code branch (2026-08-29, later)

`fix/windows-late-result-after-expiry` (`8e50ae1`) was added after the first
probe. It is cut from `fix/windows-command-expiry-on-read` (`62df2dc`), so
landing it lands that one too — four code branches, not five.

Re-probed the same way: throwaway **detached** worktree at `5618ce3`, merge all,
no branch ref moved.

| Branch | Head | Merge |
| --- | --- | --- |
| `fix/test-step-process-group` | `d7749a9` | OK |
| `feat/salvage-observability` | `80d66d5` | OK |
| `fix/windows-late-result-after-expiry` (carries `62df2dc`) | `8e50ae1` | OK |
| `docs/staged-integration-probe` | `f655df3` | OK |

* No conflicts.
* Combined `core/` delta vs `5618ce3`: `ai_planner.py` +16, `deliver.py` +48/-4,
  `job_executor.py` +61/-3, `windows_bridge.py` +39.
* **Full suite: 2565 passed, 0 failed** (1622s) — 2562 from the first probe plus
  the 3 late-result tests.

## Git hygiene

Every staged branch verified `pushed=0` — none is contained in any remote ref.
Nothing has leaked to `origin`. The deployed line is still `5618ce3`, local ==
remote.

## Timing, again

1622s this run. Five measurements of essentially the same suite now:
**742 / 832 / 1117 / 1171 / 1622s**. The spread is load, not test count. This
reinforces that the withdrawn `RUNTIME_TEST_TIMEOUT` 600->1200 raise was the
wrong number — 1622s would have blown a 1200s cap too. Any cap chosen from these
numbers is guesswork; scoping `ai_planner.default_test_commands()` remains the
only fix that does not degrade, and it is an owner policy decision.
