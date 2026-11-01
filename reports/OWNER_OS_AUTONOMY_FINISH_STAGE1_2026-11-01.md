# Owner OS autonomy finish — Stage 1: job #136 test failures (2026-11-01)

Branch `owneros/autonomy-finish`, worktree `/opt/owneros-autonomy`, base 9557004.
Instruction source: automated Owner OS API messages (tech lead). Not owner sign-off.
PROD: untouched. Nothing deployed, pushed, restarted. No grants changed.

## 1. Job #136 (task 326) — what actually failed

- Job record (`/root/ai-dev-runtime/runtime_jobs.db`, id 703f780d…, read-only):
  planner failed (`plan missing 'files' list`) → fallback plan → the job's ONLY change
  was `reports/runtime/fallback/PLAN-326-…md`. Suite then: 30 failed / 2594 passed.
- A Markdown file cannot break Python tests → all 30 are environmental, not regressions.
- Job worktree base resolved to `main` (see §3), i.e. tests from 0fd3353.

## 2. Root cause A — tests import the LIVE tree (fixed in this stage)

Two forms, both reproduced:
1. `sys.path.insert(0, "/root/ai-dev-runtime")` (and `/tools`, `/hooks`) at import time in
   `tools/wake_companion.py`, `tools/cdp_composer.py` (3 lazy sites), `hooks/owneros_hook.py`,
   4 other tools and 16 tests. After any of them is imported, every not-yet-imported module
   resolves from the live dirty tree. Repro: import `wake_companion` from this worktree →
   `core.wake_bridge.__file__ == /root/ai-dev-runtime/core/wake_bridge.py`.
2. Tests loading code by absolute live path: `spec_from_file_location(".../root/ai-dev-runtime/tools/cdp_composer.py")`
   in test_cdp_composer / test_wake_delivery_verification, plus test_wake_companion,
   test_closed_loop_wake (open), test_owneros_hook (HOOK subprocess), test_continuation_governor (yaml).

Proof on a `git archive main` copy (job-136 scenario), files test_core + test_cdp_composer +
test_wake_assistant_proof + test_wake_delivery_verification:
- as-is: **26–29 failed** (`claim_unavailable:TypeError` — old test vs new live composer/bridge signature)
- same transform applied: **88 passed, 0 failed**

Fix: every such path now derives from `os.path.dirname(os.path.dirname(os.path.realpath(__file__)))`.
Prod-neutral: all entrypoints run in place (systemd `owner-os-wake-companion.service`
ExecStart `/root/ai-dev-runtime/tools/wake_companion.py`; Claude hooks in
`/root/.claude/settings.json` run `/root/ai-dev-runtime/hooks/owneros_hook.py`); `readlink -f`
confirms no symlinks, so the resolved root is identical live.
Not changed (runtime data/config, not code import): `hooks/owneros_hook.py` `_TRIGGER`
and `/root/ai-dev-runtime/configs/.env`; data strings like `project_path="/root/ai-dev-runtime"`.

Regression guard `tests/test_no_live_tree_imports.py` (3 tests): static scan for live
sys.path inserts; static scan for tests loading live `.py/.yaml`; subprocess probe that
importing `wake_companion` keeps `core.wake_bridge` in this checkout. Pre-fix: fails; post-fix: passes.

## 3. Root cause B — runtime jobs build on 449-commit-stale `main` (NOT changed; tech-lead review)

`core/git_write.py:95 resolve_base_branch(project_path, explicit)` candidates, live repo:
| candidate | value | result |
|---|---|---|
| explicit | `master` (job default `base_branch DEFAULT 'master'`, `runtime_supervisor.py:97`) | no such ref → skipped |
| origin/HEAD | not a symbolic ref | none |
| current branch | `ai-runtime/220-windows-bridge` @ 9557004 (the deployed line) | matches `_WORK_BRANCH_RE ^(ai-runtime|repair)/` → skipped |
| `main` | 0fd3353 (2026-08-30) | **chosen** |

`main..HEAD` = 449 commits, `HEAD..main` = 8 (main is not an ancestor of live). No `origin/main`.
Effect: every project-28 job tests/edits code ~2 months old; combined with §2 that produced the
false failures; without §2 it would produce false greens on stale code.

Proposed design (needs tech-lead decision — policy/routing impact):
- Per-project explicit `deployed_branch` (config, audited), used after a real explicit branch and
  before origin/HEAD. Do NOT relax `_WORK_BRANCH_RE` (it prevents the OWNER-111..120 stacking bug).
- Treat the `'master'` column default as "unset", not explicit.
- Fail-closed (or loud warning artifact) when the chosen base is not an ancestor of the
  project's checked-out HEAD by > N commits.
Test cases: explicit real branch wins; configured deployed line wins over main; unconfigured
checked-out work branch still skipped; nonexistent default 'master' ≠ explicit; stale-base detection
fires on main-not-ancestor; other projects (origin/HEAD present) unchanged.

Secondary blocker: planner returns plans without `files` → fallback no-op plan (job 136 did no work).

## 4. Tests

- Baseline full suite at 9557004 in this worktree: 4211 passed, 4 skipped, 0 failed
  (green only because live HEAD == this HEAD; contamination masked).
- Focused after fix (10 touched files incl. guard): 501 passed.
- Full suite after fix: 4214 passed, 4 skipped, 0 failed (baseline 4211 + 3 guard tests).

## 5. Rollback

Branch-only commit; `git revert <sha>` or `git reset --keep 9557004`. Nothing live to roll back.

## 6. Next exact step

1. Tech lead: decide §3 design (deployed_branch config) — then implement in this worktree with the tests above.
2. Next safe item meanwhile: planner `files`-missing → fallback no-op (diagnose why the planner
   output lacked `files` for task 326; make the job fail as "planner failed" rather than run a no-op plan + full suite).
3. Independent review of this diff before any promotion to live.

## 7. Stage 2 — planner "files missing" on job #136 (fixed)

Raw planner output (jobs.plan.raw_planner_response, task 326): with `--tools ""` the model
wrote a pseudo tool call in prose (`**Tool: bash**` + `{"command": "cd /root/ai-dev-runtime && git log ..."}`).
`_extract_json` returned that first JSON object → `plan missing 'files' list` → fallback no-op plan.

Fix (`core/ai_planner.py`): collect all candidate objects (fences, then prose); the one with a
`files` list wins over earlier non-plan objects; if none and the first is tool-argument-shaped,
raise `planner emitted a tool call instead of a plan`. Fallback flow unchanged
(`fallback_plan_only` still never resolves to completed). 3 tests in tests/test_planner_fallback.py.
Full suite: 4217 passed, 4 skipped, 0 failed.

Open (tech lead): task-shaped instructions ("investigate first") push the tools-less planner to
attempt exploration; options = context-pack in prompt / explicit "no tools, output JSON only" footer /
route such tasks to an agent pane instead of the runtime planner.
