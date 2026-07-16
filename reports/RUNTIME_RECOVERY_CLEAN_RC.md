# Clean Release Candidate — minimal recovery branch

**Date:** 2026-07-16
**Clean branch:** `repair/runtime-recovery-clean-20260716-142818`
**Base:** `main` @ `e5ba180` — the clean branch starts here, with no old chain behind it
**Tests:** 218 passed (full suite, and again in a clean git worktree)
**Status:** CLEAN_RC_READY_FOR_REVIEW

The first candidate merged 36 commits of mostly unrelated history. This branch
carries the same fix in **6 commits / 32 files**, built from `main` alone.

|  | Clean RC | First RC (archive) |
| --- | --- | --- |
| Commits ahead of `main` | **6** | 37 |
| Files changed | **32** | 78 |
| Insertions / deletions | **6131 / 116** | 10609 / 117 |
| Fallback-plan commits | **0** | 13 |
| `PLAN-*.md` files | **0** | 19 |
| Tests | 218 passed | 268 passed |

The full diff is saved at `reports/RUNTIME_RECOVERY_CLEAN_RC.diff` (6720 lines).
It captures the six code commits; the report and the diff artifact themselves are
added by the final commit and so are not inside it.

## 1. The archive is preserved untouched

`repair/runtime-recovery-20260716-115609` (tip `70c2a7c`) and its candidate
`rc-ab818cd253f4` are **unchanged**. Nothing was rebased, amended, force-pushed
or deleted. That branch remains the complete record of the investigation,
including the root-cause report `reports/RUNTIME_RECOVERY_FINAL.md` and every
piece of evidence. This clean branch is an alternative delivery of the same fix,
not a replacement for the archive.

## 2. How the minimal dependency tree was derived

`main` does not contain `core/notify_format.py`, `ai_planner.build_fallback_plan`
or `job_store.touch_heartbeat`, all of which the recovery work builds on. Rather
than cherry-picking commits that carry plans and reports alongside their code,
each required file was taken at its final content and re-committed by meaning.
The import graph was then checked to prove nothing outside this tree is needed.

Three modules turned out to have **zero internal imports** —
`prospect_audit_batch`, `agent_context_recovery`, `notify_format` — so they carry
across cleanly on their own. `notify_dedupe`, `notify_dispatch`, `bus_store` and
`prospect_audit_policy` are referenced by nothing that this fix needs, which is
why they could be dropped rather than ported.

## 3. Commits

| # | Commit | Meaning |
| --- | --- | --- |
| 1 | `e0d0535` | prerequisite runtime fixes (planner timeout + fallback, bounded backups, heartbeats, base-branch resolution, test-db isolation) |
| 2 | `c7c5666` | job kinds and explicit outcomes |
| 3 | `5861da4` | branch and workspace isolation |
| 4 | `5bb8e76` | Release Controller (OWNER-111) |
| 5 | `70c9534` | Telegram outcome notifications |
| 6 | `89c54d2` | recovered valid work (OWNER-113 fix, OWNER-118) |
| 7 | this commit | final report and diff artifact |

Each commit's tests land with the feature they cover, so no commit ships code
whose tests only arrive later. Commit 1 stands at 90 passed, commit 2 at 129,
and the branch at 218.

Where a single file served two commits it was split by hand rather than
duplicated: `job_executor.py` reaches commit 2 with kind routing and outcomes but
**without** the workspace-hygiene sweep, which arrives in commit 3 alongside the
branch-isolation rule it belongs with. Two tests were likewise held back from
commit 2 (`test_failed_code_job_leaves_no_untracked_debris` → commit 3,
`test_a_fallback_plan_job_cannot_feed_a_release` → commit 4).

## 4. What the branch delivers

All ten required capabilities, unchanged in behaviour from the archive branch:

* **job kinds** — `code_change`, `operational`, `content_production`,
  `deployment`, `data_handoff`, `context_restore`; only `code_change` is gated on
  the repository suite;
* **explicit outcomes** — `implemented`, `fallback_plan_only`,
  `operational_complete`, `content_complete`, `deployment_prepared`,
  `data_handoff_complete`, `context_restored`, `failed`;
* **fallback_plan_only** — never an implementation, never releasable, refused by
  the Release Controller;
* **branch isolation** — a job never bases on the previous job's work branch;
* **workspace hygiene** — a failed job removes only the files it created;
* **task-specific validation** — artifacts must exist and be non-empty; repo-suite
  commands are stripped from non-code jobs and recorded as dropped;
* **Telegram outcome notifications** — outcome-led, with reason and next step;
* **Release Controller** — create → approve → release, SHA-pinned approval, main
  backup, duplicate-merge guard, post-merge retest, single-unit restart, health
  check, automatic rollback, persistent state, CLI;
* **Prospect Audit batch fix** — one bad row no longer sinks the batch;
* **Agent Context Recovery** — preserved unmodified.

## 5. What was deliberately excluded

**13 fallback-plan commits** (`5e3ec9e`, `cd77236`, `9683d78`, `eda9d9a`,
`a008db4`, `62308cc`, `f2341ed`, `a5ec65f`, `1eb01d0`, `0e9f4d9`, `88a8023`,
`034a68b`, `044ae0f`) — each consists only of a Markdown plan for work that was
never implemented.

**46 files**, verified absent from the diff:

| Category | Excluded |
| --- | --- |
| Fallback plans | all 19 `reports/runtime/fallback/PLAN-*.md` |
| Unrelated portfolio reports | `reports/GITHUB_PORTFOLIO_BACKLOG_2026-07-15.{md,json}` |
| Old canary reports | `reports/SUPERVISOR_LIVE_CANARY_2026-07-15.{md,json}`, `reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md` |
| Old audit reports | `reports/OWNER_OS_RUNTIME_LIVE_AUDIT_2026-07-15.{md,json}` |
| Old deployment reports | `reports/runtime/SAFEGUARD_CAMPAIGN_ACTIVATION_2026-07-16.{md,json}`, `reports/runtime/TELEGRAM_COMPLETION_NOTIFY_DEPLOY_2026-07-16.md` |
| Separate features not needed by this fix | `core/notify_dedupe.py`, `core/notify_dispatch.py`, `core/bus_store.py`, `core/prospect_audit_policy.py`, `config/prospect_audit_policy.yaml`, `docs/PROSPECT_AUDIT_DB_POLICY.md` |
| Unrelated API / deployment surface | `api/main.py`, `api/v1.py`, `deploy/*` (5 files) |
| Tests of excluded features | `tests/test_notify_dedupe.py` (6), `tests/test_prospect_audit_policy.py` (30), `tests/test_smoke.py` (14) |
| Archive-branch documents | `reports/RUNTIME_RECOVERY_FINAL.md`, `RUNTIME_RECOVERY_TASK.md` |

The 50-test gap between the two branches is **exactly** those three excluded test
files: 6 + 30 + 14 = 50, and 268 − 50 = 218. No test was lost or weakened.

`reports/runtime/AGENT_CONTEXT_RECOVERY_RUNBOOK.md` is kept: it is the operating
documentation for `core/agent_context_recovery.py`, which this branch ships.

## 6. Verification

| Check | Result |
| --- | --- |
| Starts from `main` `e5ba180` | ✅ `git merge-base --is-ancestor e5ba180 HEAD`, first parent is `e5ba180` |
| Focused tests (each feature) | ✅ all green |
| Full `pytest -q` | ✅ 218 passed |
| Full `pytest -q` in a clean worktree | ✅ 218 passed — self-contained from `main` alone |
| `git diff --check` | ✅ clean on all code. The only hits are inside `reports/RUNTIME_RECOVERY_CLEAN_RC.diff` itself, which reproduces original context lines verbatim (trailing spaces included) because it is a captured diff — excluding that artifact, the check is silent |
| No `PLAN-*` / unrelated reports in diff | ✅ audited by pattern, all absent |
| No fallback-plan commits | ✅ 0 |
| No reference to excluded modules | ✅ grep across `core`, `api`, `cli`, `tests` |
| Internal imports resolve from this tree | ✅ AST scan, none missing |
| Business projects untouched | ✅ no `acap` / `mess` / `email` / `JobHunter` path in diff |
| Production DB unmodified | ✅ md5 `93a117e4ddbac5904e31213882be7061`, unchanged since before the recovery |
| Service not restarted | ✅ `ai-runtime.service` still on its original start time; restart happens only inside `release`, after approval |
| `main` untouched | ✅ still `e5ba180` |

## 7. Release candidate

A second candidate is created for this clean branch. The first candidate
(`rc-ab818cd253f4`, the 36-commit archive) is **kept, not deleted** — it stays in
`created` state and must not be approved.

Find the clean candidate:

```bash
cd /root/ai-dev-runtime && ./venv/bin/python -m cli.release list
```

It is `created`, **not approved, not merged, not deployed**, and refuses to
release without an explicit approval that pins its exact head SHA.

## 8. Activation and rollback

**Activation** (approve, then merge → full retest → restart only
`ai-runtime.service` → health check, with automatic rollback on any failure):

```bash
cd /root/ai-dev-runtime && BR=repair/runtime-recovery-clean-20260716-142818 && RC=$(./venv/bin/python -m cli.release list | ./venv/bin/python -c "import json,sys;print([r['id'] for r in json.load(sys.stdin) if r['state']=='created' and r['branch']=='$BR'][0])") && ./venv/bin/python -m cli.release approve --id "$RC" --approver "$(whoami)" --sha "$(git rev-parse $BR)" && ./venv/bin/python -m cli.release release --id "$RC"
```

**Rollback** (restores `main` to its pre-merge backup branch and restarts the
service):

```bash
cd /root/ai-dev-runtime && RC=$(./venv/bin/python -m cli.release list | ./venv/bin/python -c "import json,sys;print([r['id'] for r in json.load(sys.stdin) if r['state']=='released'][0])") && ./venv/bin/python -m cli.release rollback --id "$RC"
```

**Full manual rollback** (repository restored from the pre-recovery backup):

```bash
systemctl stop ai-runtime.service && rm -rf /root/ai-dev-runtime && cp -a /root/ai-runtime-recovery-backup-20260716T115417Z/repo /root/ai-dev-runtime && systemctl start ai-runtime.service
```

## 9. Jobs retryable after release

Unchanged from the archive report: OWNER-113 → `operational_complete`, 114 →
`content_complete`, 115 → `deployment_prepared`, 116 → `operational_complete`,
117 → `data_handoff_complete`, 118 → `context_restored`. Close OWNER-111 (the
Release Controller is implemented here) and OWNER-120 (superseded). Queue them
deliberately; none are re-run automatically.

**CLEAN_RC_READY_FOR_REVIEW**
