# First real GAIKA Full Cycle task (2026-11-18)

- **Repo source.** New allowed repo `gaika-live` → `/opt/gaika`, the live checkout at 1181e47d. It was added on the owner's direct word "разреши гайку с живой версии" at 11:21Z.
  - A project copy takes only the committed HEAD. The live directory and var/ are not touched.
  - Rollback: restore `/root/ai-dev-runtime/backups/fullcycle-repos-20261118T112107Z.json` to `/etc/ai-runtime/fullcycle-repos.json`, or run `repos.remove('gaika-live')`.
  - The old `gaika` entry (`/opt/gaika-extension`, main ed100521) is 2188 commits behind live. Projects should not use it.
- **Defect, reproduced in a scratch copy of 1181e47d.**
  - `tests/project-doc-counts.test.mjs` throws `ENOTDIR` on a plain file in var/catalog.
  - The live var/catalog holds `refresh-queue.json.post-reconcile-*` files.
  - Without var/, the file passes: 19 pass, 3 skip.
- **For the tech lead.** Start project `gaika-catalog-walk` with `fullcycle_project_start(project_id="gaika-catalog-walk", repo="gaika-live", spec=<spec.json>)`, then review it through MCP. One agent, no deploy.

## `intake_directory_exists` (investigated read-only; an automated instruction was received)

**Cause: a second start call landed during the first call's repo copy.** It was neither an orphan nor a foreign directory.
- `/opt/fc-projects/gaika-catalog-walk` was born at 12:48:41.18Z, and `.git` at 12:48:41.26Z.
- The `fc_project` row was written at 12:49:02.56Z, so the copy window was about 21 s:
  - 83 MB tree, 30 MB `.git`;
  - fetch of 1181e47d, then the chown to fcagent.
- `intake.start` checks `os.path.lexists(path)` before copying, but holds no lock, and the project row is written only after the copy finishes. A second call inside those 21 s therefore sees a directory with no project and is refused with `intake_directory_exists`, while the first call completes.
- No API log line records the calls, so the exact second-call time is inferred: the tech lead's `project_get` showed the project as absent, which places that check before 12:49:02.

**Result: the project is healthy.**
- `gaika-catalog-walk`: source `gaika-live` at base `1181e47d60f8…` (full SHA in `fc_intake_source`).
- Branch `fullcycle/gaika-catalog-walk`, owner `fcagent`, spec v1 by `api:client/chatgpt-tech-lead`.
- The tech lead's own spec: task `catalog-walk`, scope `tests/`, check `node --test tests/project-doc-counts.test.mjs`.
- Task `catalog-walk` started working at 12:50:00Z, attempt 1.
- No recovery is needed. Nothing was deleted or changed.

**Proposed DEV fix (not done).** Either option would turn a retry into a clear answer instead of a confusing refusal:
- make the start idempotent while a copy is in flight: a per-project lock, or a pending marker written before the copy, so a retry gets `intake_in_progress`; or
- return the existing project once the row lands.

## Live evidence: periodic reconcile (distinct from pre-send)
| Event | Route | Trail (`wake_send_intent_audit`) |
|---|---|---|
| 89067 | hostsecure | intent → not_sent (11:58:56, `cdp_error:WebSocketTimeoutException`) → retry intent (12:05:14) → indeterminate (12:06:09, same error) → **delivered** (12:08:49, `reconciled_assistant_answered`) |
| 89068 | mess | the same path; **delivered** at 12:10:58 |

- Both late receipts came from the **periodic** reconcile, journal format `wake send reconcile: event N -> delivered`.
- The **pre-send** reconcile line is `(before event …)` and has still not appeared, so it is still unexercised live.
- D2 recurred: 4 post-send `WebSocketTimeoutException` between 11:58 and 12:08Z. Without the 2883d2b fix, both events would have stayed undelivered.
