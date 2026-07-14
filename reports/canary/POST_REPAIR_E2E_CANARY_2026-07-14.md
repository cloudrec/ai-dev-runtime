# POST-REPAIR E2E CANARY — 2026-07-14

Proves the repaired execution chain works end to end after the 2026-07-14 command-bus/watcher patch:
GitHub -> OwnerTask -> Runtime -> GitHub.

## Run metadata

- UTC timestamp: ${RUNTIME_UTC_TIMESTAMP}
- GitHub issue number: ${GITHUB_ISSUE_NUMBER}
- BusEvent ID: ${BUS_EVENT_ID}
- OwnerTask ID: ${OWNER_TASK_ID}
- Runtime job ID: ${RUNTIME_JOB_ID}
- External job ID (if present): ${EXTERNAL_JOB_ID}
- Worker PID / identity: ${WORKER_IDENTITY}
- Branch name: ${BRANCH_NAME}
- Commit SHA: ${COMMIT_SHA}
- Draft PR number: ${DRAFT_PR_NUMBER}

## Chain evidence

1. Durable BusEvent recorded in bus_store (id ${BUS_EVENT_ID}).
2. Durable OwnerTask persisted (id ${OWNER_TASK_ID}).
3. Real Runtime job created and claimed (id ${RUNTIME_JOB_ID}, external ${EXTERNAL_JOB_ID}).
4. Worker claim/start record produced by ${WORKER_IDENTITY}.
5. Working branch created: ${BRANCH_NAME}.
6. This report file authored at reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md.
7. Commit ${COMMIT_SHA} created and pushed.
8. Draft PR #${DRAFT_PR_NUMBER} opened (not auto-merged).

## Validation

- Command: `test -s reports/canary/POST_REPAIR_E2E_CANARY_2026-07-14.md`
- Result: ${VALIDATION_RESULT}

## Model / token accounting

- Model call occurred: ${MODEL_CALL_OCCURRED}
- Model: ${MODEL_NAME}
- Prompt tokens: ${PROMPT_TOKENS}
- Completion tokens: ${COMPLETION_TOKENS}
- Total tokens: ${TOTAL_TOKENS}

## Constraints honored

- No production systems modified.
- No automatic merge performed; PR left as draft for human review.
