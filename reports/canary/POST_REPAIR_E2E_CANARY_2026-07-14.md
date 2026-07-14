# POST-REPAIR E2E CANARY — 2026-07-14

Branch: `repair/owner-os-runtime-e2e-20260714`
UTC timestamp of this report: 2026-07-14T18:08:27Z
GitHub issue: cloudrec/ai-dev-runtime#10
OwnerTask: 82 (unchanged — no new OwnerTask created)
Original BusEvent: 85 (unchanged — no new BusEvent created)

## 1. Failed jobs (confirmed, documented, left untouched)

| Runtime job (external UUID) | SQLite row | task_id | autonomy_level | error | duration |
| --- | --- | --- | --- | --- | --- |
| `293bb4b0-955e-44de-87f4-3c6bfa11c5b7` | 12 | 82 | execute_full | `planner timed out` | 180s (matches old `RUNTIME_PLAN_TIMEOUT=180`) |
| `eae2d77a-d994-4ae4-b5b3-264596364dd2` | 14 | 82 | execute_safe | `planner timed out` | 900s (matches an interim, ultimately unsuccessful, `RUNTIME_PLAN_TIMEOUT=900` bump) |

Job 14's per-job log shows 45 heartbeats at a steady ~20s cadence for the full
900s, then a single timeout error — the subprocess never produced any output,
it was not merely slow. Raising the timeout a second time would only have
produced the same result more slowly; the timeout was never the bug.

Orphan `fb1ddfd2-670b-44c6-9f1c-373fff9774ab` (waiting_approval, task 82) was
left untouched. SQLite rows 15–18 (tasks 83/84/85/86, waiting_approval) were
left untouched — not approved, modified, cancelled, executed, or repurposed.

## 2. Root cause (confirmed by direct reproduction)

`core/ai_planner.py` invoked the host `claude` CLI as `claude -p` with:
- the **default toolset** (Bash/Read/Write/Task/etc. all enabled), and
- the **operator's live `$HOME/.claude` session settings** (same `$HOME=/root`
  as the interactive session used to run this repair — output-style overrides,
  permission-mode overrides, MCP config all inherited).

Reproduced directly (bounded, no production file changes) by calling
`ai_planner.plan()` with the exact goal/instructions/project_path stored for
job 14 and a 55s timeout: the subprocess hung for the full bound with zero
output, identically to the real failures. Bisecting confirmed the *repo file
listing* and *project_path* were not the trigger — a trivial goal against the
same project_path returned in seconds. The real OwnerTask-82 instructions text
(describing a multi-step "produce a durable BusEvent … a commit … a push … a
draft PR … a GitHub comment" outcome) is task-shaped enough that, with a full
toolset available and no explicit prohibition on acting, the model attempted
real agentic work (tool calls) rather than emitting a single JSON plan — and
never returned within any timeout, however large.

Confirmed by elimination: passing `--tools ""` alone (disabling all tool
access) made the identical prompt/instructions return valid output in ~14–31s.
Adding `--setting-sources "" --strict-mcp-config` additionally eliminated
observed pollution from the operator's own session state (one raw stdout
capture during diagnosis showed the subprocess hallucinating a fake "Bash
command executed" transcript and stray caveman-mode-styled prose — both gone
once settings/tool inheritance were cut off).

Two contributing factors, one primary fix:
1. **Primary**: no `--tools ""` — the planner is supposed to be a stateless
   text→JSON call and must never have tool access at all.
2. **Contributing**: no settings/MCP isolation — the subprocess silently
   inherited operator session state via shared `$HOME`.

## 3. Fix (`core/ai_planner.py`)

- `claude -p --tools "" --setting-sources "" --strict-mcp-config --output-format json`
  — no tool access, no inherited settings/MCP, structured JSON envelope
  (bonus: the envelope carries real `duration_ms`/`total_cost_usd`/`usage`/
  `modelUsage`, used for accounting below).
- `RUNTIME_PLAN_TIMEOUT` reverted to its original `180` — the timeout was
  never the bug; raising it a second time was rejected as a "fix" per the
  operator's explicit instruction.
- Timeout/heartbeat loop now uses `proc.wait()` + `proc.poll()` instead of
  repeated `communicate(timeout=...)`, with prompt delivery and stdout/stderr
  draining each on their own thread (`_feed_stdin`, `_drain`) — avoids a
  pipe-buffer deadlock for large prompts and lets stdout/stderr be capped
  independently (`RUNTIME_PLAN_MAX_OUTPUT_BYTES`, default 4MB) without
  blocking either stream.
- Process-group kill (`_kill_process_group`) now runs on **every** exit path
  (success, failure, and timeout), not only timeout — and uses `proc.pid`
  directly as the pgid (valid because `start_new_session=True` makes the
  child its own session/group leader) instead of `os.getpgid(proc.pid)`,
  which raises `ProcessLookupError` once the child has already been reaped
  and silently skipped grandchild cleanup.
- `_classify_failure()` turns a failed/erroring invocation into one of a
  small set of non-secret-leaking classes (`provider_auth_required`,
  `provider_limit_exceeded`, `provider_setup_required`,
  `provider_interactive_prompt_detected`, `provider_permission_denied`,
  or a truncated raw fallback) using the JSON envelope's
  `api_error_status`/`permission_denials`/`subtype` first, then stderr/stdout
  pattern matching.

## 4. Provider CLI — inspected, not assumed

`claude --help` was read in full before any flag was used. Confirmed from the
CLI itself (not memory/external docs): `-p/--print` skips the workspace trust
dialog in non-interactive mode; `--tools ""` disables all tools; `--setting-sources`
takes `user,project,local` (empty = none); `--strict-mcp-config` ignores all
MCP config outside `--mcp-config`; `--output-format json` returns one envelope
with `result`, `is_error`, `subtype`, `api_error_status`, `permission_denials`,
`usage`, `modelUsage`, `total_cost_usd`.

## 5. Controlled provider smoke test (real, executed)

Command actually run (see `/root/repair_backup_20260714/evidence/smoke_test_final.py`):

```
/root/.local/bin/claude -p --tools "" --setting-sources "" --strict-mcp-config --output-format json
```
cwd=`/root/ai-dev-runtime`, env = ai-runtime.service's effective env (`PATH`,
`HOME=/root`, + `configs/.env`), stdin = prompt piped then closed (equivalent
DEVNULL+positional-arg variant was also verified separately), hard timeout
60s, no permission-bypass flags, no production file changes. Prompt requested
only `{"ok":true}`.

**Result:** exit=0, elapsed=4.45s, `result` field = `{"ok":true}` exactly,
`duration_ms=1327`, `total_cost_usd=$0.0015675`, `usage.output_tokens=9`,
model billed: `claude-opus-4-8[1m]`. Full output in
`/root/repair_backup_20260714/evidence/smoke_test_final_output.txt`.

## 6. Tests added (`tests/test_phase13.py`)

All exercise `core/ai_planner.plan()` against a fake CLI stub (no network, no
real provider calls):

- success path via `--output-format json` envelope
- timeout kills the whole process group, including a SIGTERM-ignoring child
  that itself forked a grandchild (heartbeats fire ≥2 times)
- hanging parent with no children still times out and leaves nothing running
- parent exits immediately but leaves a detached child running — child is
  still reaped (process-group cleanup independent of the timeout path)
- malformed (non-JSON) response
- empty stdout on clean exit
- immediate non-zero exit with stderr → classified `claude cli error`
- simulated `api_error_status: 429` envelope → classified `provider_limit_exceeded`
- interactive-looking stderr (`Press Enter to continue...`) → classified
  `provider_interactive_prompt_detected`
- oversized output (2MB against a 1000-byte cap) does not hang and fails
  cleanly instead of exhausting memory
- unit tests for `_drain()` capping and `_classify_failure()` pattern matching

## 7. Test results

- `venv/bin/python3 -m pytest -q` → **41 passed** (24 `test_core.py` + 17
  `test_phase13.py`, up from the prior 31 total because this repair added new
  planner regression tests; no prior test was removed or weakened).
- `ai-runtime.service` restarted to load the fix; `GET /health` → `200
  {"status":"ok", ...}` immediately after restart.

## 8. Backups (taken before any change; no credentials inspected/printed)

- `/root/repair_backup_20260714/ai-dev-runtime.tar.gz` (full working tree,
  excluding `venv/` and `.git/`)
- `/root/repair_backup_20260714/runtime_jobs.db.bak` (sha256/md5-verified
  identical to the live DB at backup time)
- `/root/repair_backup_20260714/ai-runtime.service.bak` (unit file)
- `/root/repair_backup_20260714/ai-runtime.service.systemctl-show.txt`
  (effective `systemctl show` config: `User`, `WorkingDirectory`,
  `Environment=`, `EnvironmentFiles=`, `ExecStart`)

## 9. Replacement Runtime job for OwnerTask 82 — BLOCKED, not created

**No replacement job was created.** This was a deliberate stop, not an
oversight.

The orchestration layer that owns OwnerTask 82 (outside this repository) only
redispatches a Runtime job for an existing task when that task has *never*
had a runtime job recorded against it. OwnerTask 82 does not qualify: its
most recent attempt (job 14 / `eae2d77a…`) already has an id recorded, even
though that job later failed. With an id already on file, the orchestrator's
normal "task already dispatched" handling returns the old, failed job's IDs
rather than attempting a new dispatch — there is currently no
retry-after-failure path for this case, and re-running the fallback watcher
alone does not reach one either (it only re-observes GitHub issue state; it
does not itself decide to redispatch).

Creating the job by calling this repo's own `POST /api/v1/jobs` directly with
`task_id=82` would work mechanically, but would bypass that orchestration
layer entirely — the same pattern that, per the operator's own note, is
suspected to have produced the orphaned, unrelated jobs at SQLite rows 15–18
(tasks 83–86). The operator's instructions explicitly required going through
"the normal repaired Owner OS / watcher path," so this run stopped short of
creating the replacement job rather than taking that shortcut.

**This is a gap in the external orchestrator, not in `ai-dev-runtime`.**
Fixing it is out of scope for this branch (`No unrelated service changes`)
and was not attempted, and no internal details of that service are recorded
here since it is a separate, non-public codebase. Two ways forward, for the
operator to choose:

1. Explicitly authorize a direct `POST /api/v1/jobs` call for `task_id=82`,
   `autonomy_level=execute_safe`, using OwnerTask 82's existing title/
   instructions — acknowledging it bypasses the orchestrator's own dispatch
   bookkeeping for this one call, **or**
2. Patch the orchestrator's existing-task retry condition (in its own repo)
   to also retry when the previously-recorded runtime job is in a terminal
   failure state, then re-run its fallback watcher once through the now-
   genuinely-normal path.

Everything downstream of job creation — durable BusEvent, OwnerTask linkage,
Runtime internal job ID, external UUID, approval, worker claim, planner
completion, validation/tests, duplicate-delivery idempotency, watcher state
ordering — is **not yet exercised** for a replacement job, pending that
decision. The now-fixed planner itself is proven end-to-end via the smoke
test and the full unit-test suite (§5–§7), independent of how the job is
created.

## 10. Model/token accounting

Only the smoke test in §5 made a real provider call during this repair (plus
earlier diagnostic reproductions against the real OwnerTask-82 instructions,
run for root-cause analysis, also bounded and non-mutating). No Runtime job
was created or executed, so no job-level plan call occurred. Smoke-test cost:
$0.0015675, 9 output tokens billed against `claude-opus-4-8[1m]` (cache-read
input tokens: 2665, from repeated identical-prefix diagnostic calls in this
same session).
