# Owner OS Runtime Live Audit — 2026-07-15

- **Generated (UTC):** 2026-07-15T13:07:02Z
- **Scope:** read-only audit of Owner OS (traffic_os / `seo-backend`) + `ai-dev-runtime` + the manual Claude/tmux workflow.
- **Mode:** no fixes, no restarts, no job approve/retry/cancel/reconcile/create, no DB writes, no merges. Secrets never printed (only env-key presence and host:port shown).
- **Overall readiness:** ~**60%** (semi-autonomous; end-to-end still needs manual steps).

> Numbering note: "Runtime job N" in the owner's language = the **Owner OS `runtime_jobs.id`** (Postgres), which differs from the runtime service's SQLite ROWID and from the external UUID. This report states all three where relevant. Confirmed mapping: Owner OS `runtime_jobs.id` **17→task 83, 18→84, 19→85, 20→86, 25→task 92**.

---

## 1. ai-dev-runtime repository

| item | value |
| --- | --- |
| current branch | `repair/owner-os-runtime-e2e-20260714` |
| HEAD | `ebe818d` docs(canary): record planner fallback fix + replacement job for OwnerTask 92 |
| git status | **clean** (no uncommitted changes) |
| remote | `git@github-ai-dev-runtime:cloudrec/ai-dev-runtime.git` (SSH alias) |
| recent repair commits | `ebe818d` docs, `64696a6` planner fallback fix, `61cb1b6` repair-loop commit fix, `ce09611` recover_interrupted race, `9311c94` base-branch fix, `8ac76f2` planner `--tools ""` fix |
| open draft PRs | **#13** (repair: base-branch/race/commit fixes, draft, →main), **#12** (PHASE-45 canary, draft), **#14** (OwnerTask 92 fallback, *ready*, →repair branch) |
| repair reports | `reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md` (incl. §11 replacement run) |
| workspace safe? | **Yes.** Tree clean, HEAD on the repair lineage, backup exists at `.ai-runtime-backups/repair_planner_fallback_20260715T113350Z/` (tree + DB, sha256-verified). |

## 2. Runtime service (`ai-runtime.service`)

| aspect | finding | verdict |
| --- | --- | --- |
| service status | `active (running)`, enabled | WORKING |
| PID / uptime / restarts | MainPID 2312717 (uvicorn), up since 2026-07-15 13:40:49 CEST (~1h20m at audit), **NRestarts=0** | WORKING |
| health endpoint | `GET /health` → `{"status":"ok", ... tasks_total:0}` | WORKING |
| worker heartbeat | heartbeat thread pulses job rows during execution (verified live on job `0093273a`) | WORKING |
| queue processing | jobs execute in background threads on approve/queue; completed job `0093273a` end-to-end | WORKING |
| recovery after restart | `recover_interrupted()` marks jobs interrupted-during-edit → `waiting_approval` (rows 24/26/30/32/34 show exactly this) | WORKING |
| branch resolution | `resolve_base_branch` never hardcodes master; resolved `repair/...` for the replacement job | WORKING |
| planner execution | non-agentic `claude -p --tools "" --setting-sources "" --strict-mcp-config --output-format json` | WORKING |
| planner timeout | timeout kills whole process group; **no new `planner timed out` since `8ac76f2`** | WORKING (fixed) |
| malformed / non-JSON response | previously fatal (job 25); now `_extract_json` handles fenced/prose JSON, classifies malformed vs no-JSON | WORKING (fixed) |
| fallback planning | on any PlannerError (except provider_not_configured) → deterministic local plan, marked in job metadata, no retry loop. **Exercised live** on `0093273a` | WORKING (fixed) |
| coding-agent launch | **No independent coding agent exists.** The planner emits file content directly; the pipeline applies it. The fallback writes only a safe planning-record doc (no fabricated code). | PARTIAL |
| test execution | `_run_tests` runs argv (no shell=True), supports `&&` chains; ran full pytest (91 passed) on `0093273a` | WORKING |
| commit / push | commit + `push -u origin` work (job `0093273a`: commit `8d6bf53`, pushed). But not every job pushes (job 24/task 88 committed `d0400fb`, `pushed=False`). | PARTIAL |
| draft PR creation | **Runtime pipeline never opens PRs** (no `gh`/PR code in `job_executor`/`git_write`). PR #14 was opened **manually**. | BROKEN |

## 3. Owner OS integration (traffic_os / `seo-backend-1`)

| aspect | finding | verdict |
| --- | --- | --- |
| Owner OS health | container `seo-backend-1` Up 5h (healthy), Postgres `seo-postgres-1` healthy | WORKING |
| runtime URL/config | `RUNTIME_URL=http://host.docker.internal:8199` (correct), `RUNTIME_TOKEN` set, `RUNTIME_POLL_ENABLED=true`, interval 10s | WORKING |
| job polling | `services/runtime_poller.poll_once` — advisory-locked, polls jobs with `external_job_id` set and status in ACTIVE; applies plan/diff/commit/branch/tests/error | WORKING (scoped) |
| reconciliation | startup `_reconcile_stranded_jobs`; manual `POST /jobs/{id}/reconcile` (`runtime_reconcile.reconcile_with_evidence`); one manual reconcile recorded 07:54 | WORKING (manual) |
| retry synchronization | `services/runtime_retry.retry_runtime_job` creates a NEW tracked row (retry lineage) — but only via `POST /jobs/{id}/retry` or MCP. **Out-of-band runtime jobs are invisible.** | PARTIAL |
| event ingestion | `bus_events` populated (GitHub `issues`/`issue_comment`, latest 2026-07-15 12:26) via webhook/`command_bus`; `owner_events` emitted by poller on terminal transitions | PARTIAL |
| task→runtime dispatch | machinery exists (`ai_cto._create_and_submit`, `autopilot`, `dev_manager`, `command_bus`, `ai_bridge`) but tasks 82–92 are `[MCP]`-prefixed = created/dispatched **manually** via the ChatGPT/MCP bridge. The systemd `ai-task-watcher.service` is **dead** (last run failed HTTP 500, 2026-07-14). | PARTIAL |
| approval flow | `requires_approval` + `routes_approvals`; job 25 was approved 11:23 before running | WORKING |
| notification flow | `_notif_loop` turns `owner_events` → `notifications` (rule "Warnings & critical → local", enabled) | WORKING (local only) |
| Telegram delivery | `TELEGRAM_*` env **unset** in `seo-backend`; `notifications.channel_kind=telegram` recorded once as **`not_configured`**; `email`/`webhook` also `not_configured`. Only `local` delivers (`status=sent`). | BROKEN |
| status detection (queued/planning/running/waiting_input/blocked/failed/completed) | poller maps runtime→Owner OS: ACTIVE set includes queued/planning/…/testing/committing/pushing/running/waiting_approval; TERMINAL = completed/failed/cancelled/rolled_back/blocked. **`waiting_input` has NO representation** (no runtime status, no Owner OS mapping). | PARTIAL |

**Status-detection detail:** Owner OS *can* detect queued, planning, running (via the intermediate ACTIVE states), blocked, failed, completed — **for jobs it created**. It **cannot** detect `waiting_input` (no such status anywhere), and it cannot detect anything about jobs created out-of-band (no `runtime_jobs` row).

## 4. Current jobs and tasks

| ref | title | status | external_job_id | branch | commit | tests | error | authoritative? | duplicate? | owner action? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RJ 17 / task 83 | (planner-timeout batch) | failed | c12411f3 | — | — | — | planner timed out | authoritative | — | none (historical) |
| RJ 18 / task 84 | " | failed | 43b32aeb | — | — | — | planner timed out | authoritative | — | none |
| RJ 19 / task 85 | " | failed | b9b17b46 | — | — | — | planner timed out | authoritative | — | none |
| RJ 20 / task 86 | " | failed | 7cc48c98 | — | — | — | planner timed out | authoritative | — | none |
| RJ 21 / task 87 | " | waiting_approval | dde9cd25 | — | — | — | — | authoritative | — | approve-or-cancel decision |
| RJ 22 / task 82 | retry | superseded | f8ae91e1 | repair/owner-os-runtime-e2e-20260714 | — | — | tests failed after repair | authoritative | superseded by RJ23 | none |
| RJ 23 / task 82 | retry | completed | d098b34f | ai-runtime/82-retry-retry-… | 10d0ce8 | ok | — | authoritative | — | none |
| RJ 24 / task 88 | [MCP] Fix planner timeout & resume #11 | completed | f64f64de | ai-runtime/88-…-61cb1b6 | d0400fb | ok | — | authoritative | — | **push + PR** (commit exists, `pushed=False`); task 88 = `review` |
| RJ 25 / task 92 | [MCP] Implement planner timeout fallback only | **failed** | 0a079853 | — | — | — | model did not return JSON | **STALE** | ✅ replacement `0093273a` (runtime-side **completed**, commit `8d6bf53`, pushed) not tracked by Owner OS | **reconcile/retry via Owner OS** to record the successful replacement; task 92 currently `blocked` |
| OwnerTask 88 | — | `review` | (RJ24) | — | d0400fb | ok | — | authoritative | — | push branch + open PR |
| OwnerTask 91 | [MCP] Event-driven Runtime supervisor + owner notification | `backlog` | — | — | — | — | — | authoritative | — | **NO runtime job exists**; not started |
| OwnerTask 92 | [MCP] Implement planner timeout fallback only | `blocked` | RJ25 (failed) | — | — | — | model did not return JSON | **STALE** (fix delivered out-of-band) | replacement job `0093273a` | reconcile to `review`/`done` |

## 5. Manual Claude / tmux workflow

- **tmux sessions (10):** `3dthings`, `acap`, `bitvise`, `email`, `job`, `mess`, `runtime-planner-fix`, `runtime-repair-ACTIVE-DONT-TOUCH`, `seo`, `tmux-cleanup`. Multiple detached/attached `claude` processes running.
- **Is Claude output captured?** Partially — each Claude CLI persists a transcript under `~/.claude/projects/*/*.jsonl` (session logs), and tmux keeps scrollback. **Owner OS does not read either.**
- **Can Owner OS see Claude questions?** **No.** No bridge from tmux/Claude transcripts into Owner OS.
- **Can Claude report `waiting_input`?** **No.** There is no `waiting_input` status in the runtime or Owner OS, and no channel from a manual Claude session to Owner OS.
- **Does Claude completion reach Owner OS automatically?** **No.** Only runtime-service jobs that Owner OS itself created are polled. Manual Claude work is invisible.
- **Can any component auto-answer safe Claude questions?** **No** such component found.
- **Can ChatGPT receive immediate events from the server?** **No.** ChatGPT integrates via the MCP bridge (`services/mcp_server.py`, `services/ai_bridge.py`) which is **pull-based** — ChatGPT queries Owner OS; the server pushes nothing to ChatGPT.

**Blind spots:** manual Claude/tmux sessions are entirely outside Owner OS observability; no `waiting_input` concept end-to-end; out-of-band runtime jobs untracked; no server→ChatGPT and no server→owner push path.

## 6. Notifications and automation

- **Mechanisms:** `_notif_loop` consumes `owner_events` (pointer in `notification_state`, last processed id 2164 @ 13:03) → `notifications`, per `notification_rules`. One enabled rule: **"Warnings & critical → local"**.
- **Records:** 24 notifications; `local`=21 `sent`; `telegram`/`email`/`webhook` each 1 × **`not_configured`**. Latest `runtime.job.failed` notification id 24 @ 11:27 (from job 25 failing).
- **Telegram:** not configured in Owner OS (`TELEGRAM_*` unset). A separate host process `python -m app.telegram.runner` and `jh-telegram-bot`/`beautybot-telegram-bot` containers exist but are **unrelated** to Owner OS runtime notifications.
- **Scheduler/polling:** Owner OS runs 10 asyncio loops at startup (runtime-poll, scheduler, media, studio, publish, pipeline, calendar, notif, budget, health). All **polling-based**.
- **Push vs poll:** **Polling** throughout. No push channel currently delivers to the owner.
- **Can a completion event wake the owner immediately?** **No.** Best case today: a runtime completion → poller (≤10s) → owner_event → notification **local** only (in-app; owner must look). No external push.

## 7. End-to-end workflow matrix

| stage | verdict | evidence / exact failure point |
| --- | --- | --- |
| GitHub issue/task | PARTIAL | `bus_events` ingest `issues`/`issue_comment` (latest 12:26 today) via webhook/`command_bus`; but `ai-task-watcher.service` is **dead** (HTTP 500, 2026-07-14) |
| OwnerTask creation | WORKING | `owner_tasks` 88/91/92 created (via MCP bridge) |
| approval | WORKING | `requires_approval` + approvals; job 25 approved 11:23 |
| Runtime job | WORKING | `runtime_jobs` rows created & linked; job `0093273a` ran fully |
| planner | WORKING | non-agentic planner; replacement job passed planning |
| coding agent | PARTIAL | no distinct coding agent — planner emits files; fallback writes only a plan doc |
| tests | WORKING | full pytest 91 passed on `0093273a` |
| commit | WORKING | commit `8d6bf53` |
| push | PARTIAL | `0093273a` pushed; job 24/task 88 committed `d0400fb` but `pushed=False` |
| draft PR | BROKEN | runtime opens no PRs; PR #14 opened manually |
| Owner OS synchronization | PARTIAL | poller works for Owner-OS-created jobs, but replacement `0093273a` is out-of-band → Owner OS shows RJ25 **failed** / task 92 **blocked** (stale) |
| automatic next task | BROKEN | no auto task-chaining; dispatch manual via MCP; task 91 sits in `backlog` with no job |
| Telegram notification | BROKEN | telegram `not_configured`; env unset; only `local` notifications |
| ChatGPT visibility | PARTIAL | MCP pull only; no server→ChatGPT push |

Counts: **WORKING 5, PARTIAL 6, BROKEN 3, UNVERIFIED 0** (of 14 pipeline stages).

## 8. Conclusion

- **Overall readiness:** ~**60%**. Core runtime execution is solid; the autonomy loop around it (dispatch, PR, sync, notify) is manual or broken.
- **Works reliably now:** runtime service health/recovery/heartbeat; planner (non-agentic) + timeout handling + **fallback**; test execution; commit; base-branch resolution; Owner OS poller for tracked jobs; approval flow; in-app (`local`) notifications.
- **Works only manually:** GitHub→task dispatch (MCP), draft-PR creation, push for some jobs, reconcile/retry, and every step of the manual Claude/tmux workflow.
- **Completely broken/absent:** automated draft-PR creation in the runtime; external push notifications (Telegram/email/webhook not_configured); automatic next-task advancement; `ai-task-watcher.service` (dead); any `waiting_input` concept; any server→owner or server→ChatGPT push.
- **Why jobs 17–20 failed:** planner **timed out** — the pre-`8ac76f2` planner invoked `claude -p` with the full toolset and inherited `$HOME/.claude` session state; on task-shaped instructions the model went agentic and never emitted JSON, burning `RUNTIME_PLAN_TIMEOUT`. (Owner OS RJ 17–20 = tasks 83–86.)
- **Why job 25 failed:** after the timeout fix, the planner returned **prose instead of JSON** → `model did not return JSON`; `job_executor` converted that `PlannerError` straight into a failed job (no fallback existed yet).
- **Did the coding agent start for job 25?** **No.** RJ25 (`0a079853`) died at the planning stage — `branch=None`, `commit=None`, no file ops; its log ends at "planning failed". Nothing past planning ran.
- **Did the previous planner repair fix the timeout?** **Yes for timeout specifically** (no `planner timed out` after `8ac76f2`), **but it did not make the planner reliable** — job 25 then failed a *different* way (non-JSON). The new fallback (`64696a6`) closes that gap and is proven live (job `0093273a`).
- **Does OwnerTask 91 have any Runtime job?** **No.** Task 91 is `backlog` with zero `runtime_jobs` rows.
- **Is the system currently autonomous?** **No.** It is semi-manual: dispatch via MCP, PRs by hand, notifications in-app only, out-of-band jobs untracked, and the successful task-92 replacement required manual creation and is not yet reflected in Owner OS.
- **Smallest safe sequence of fixes for real autonomy:**
  1. **Sync task 92** through the supported Owner OS retry/reconcile path (`runtime_retry`/`POST /jobs/{id}/reconcile` or MCP) so the completed replacement `0093273a` (commit `8d6bf53`) is recorded — no DB write.
  2. **Automate draft-PR creation** in the runtime pipeline (after push) and push consistently (fix job-24-style `pushed=False`).
  3. **Configure one external push channel** (Telegram or webhook) so completion/blocked events reach the owner immediately.
  4. **Revive/replace `ai-task-watcher`** (or wire webhook→dispatch) for automatic GitHub→task→job flow.
  5. **Add a `waiting_input` status + a Claude/tmux↔Owner OS bridge** so manual Claude questions and completions are observable.
- **Single next recommended action:** Officially reconcile/retry **OwnerTask 92** through Owner OS so the successful replacement job is tracked and task 92 leaves `blocked` — via the supported endpoint/MCP, **never** a direct DB write.
- **Components that must NOT be touched:** `ai-runtime.service` (no restart), `seo-postgres-1` / `traffic_os` DB rows (no writes), `owner_tasks`/`runtime_jobs` records, Runtime jobs 17–21, the completed replacement job `0093273a`, tmux sessions `runtime-repair-ACTIVE-DONT-TOUCH` and `runtime-planner-fix` and the other live `claude` sessions.

---

*Read-only audit. No service was restarted; no job was approved, retried, cancelled, reconciled, or created; no database record was modified; nothing was merged. Unverified items are marked as such. Owner OS report ingestion (§ publish step) requires owner authentication not available to this read-only audit and was intentionally not performed via any DB write.*
