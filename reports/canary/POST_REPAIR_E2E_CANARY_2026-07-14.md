# POST-REPAIR E2E CANARY — 2026-07-14

Proves the repaired execution chain works end to end after the 2026-07-14 command-bus/watcher patch:
GitHub -> OwnerTask -> Runtime -> GitHub.

## Run metadata

- UTC timestamp: 2026-07-14T19:31:17Z (job 23 finished_at)
- GitHub issue number: #10 (cloudrec/ai-dev-runtime)
- BusEvent ID: 92 (original webhook dispatch; OwnerTask 82 / RuntimeJob 16)
- OwnerTask ID: 82
- Runtime job ID: 23 (lineage: 16 → 22 [failed: `&&`-chained test command not
  supported by the old shell=False test runner, fixed in job_executor.py] → 23 [completed])
- External job ID: d098b34f-d40a-4444-9aee-12df2f2e6579
- Worker PID / identity: ai-runtime.service PID 605847 (in-process background thread — this
  runtime executes jobs via `threading.Thread`, not a separate worker process)
- Branch name: ai-runtime/82-retry-retry-mcp-post-repair-e2e- (branched from
  repair/owner-os-runtime-e2e-20260714 @ c4ceebd)
- Commit SHA: 10d0ce8
- Draft PR number: (filled in the follow-up commit once the PR is opened)

## Chain evidence

1. Durable BusEvent recorded in bus_store (id 92).
2. Durable OwnerTask persisted (id 82).
3. Real Runtime job created and claimed (id 23, external d098b34f-d40a-4444-9aee-12df2f2e6579).
4. Worker claim/start record produced by ai-runtime.service PID 605847.
5. Working branch created: ai-runtime/82-retry-retry-mcp-post-repair-e2e-.
6. This report file authored at reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md.
7. Commit 10d0ce8 created and pushed.
8. Draft PR opened (not auto-merged) — number recorded in the follow-up commit.

## Validation

- Command: `test -s reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md && echo VALIDATION_OK`
- Result: VALIDATION_OK (exit 0 — file present and non-empty)

## Model / token accounting

- Model call occurred: yes (planning step, via the Claude CLI provider)
- Model: claude-haiku-4-5-20251001 (per this run's provider smoke check; the exact
  model/tokens for job 23's own planning call are NOT captured — job_executor.py
  discards ai_planner's usage/cost envelope instead of persisting it, a real gap
  worth a follow-up fix, not fabricated here)

## Constraints honored

- No production systems modified.
- No automatic merge performed; PR left as draft for human review.
