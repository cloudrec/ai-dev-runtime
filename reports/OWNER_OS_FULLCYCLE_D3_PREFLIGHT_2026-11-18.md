# Preflight: disposable Full Cycle acceptance for reject, rework and add_tasks (D3), 2026-11-18

Trigger: an automated instruction was received (Owner OS API). It is not owner approval.

Scope:
- Read-only preflight. Nothing was launched, deleted or changed in PROD.
- No browser, private chat, secret or payment agent was touched.
- `acc-20261118` evidence is intact (directory, repo, DB rows).
- The tech lead starts the project and decides through MCP.

## Service health, read-only (10:24Z)
- Live runtime: `758ebe4`.
- Units: ai-runtime, wake-companion, fullcycle-intake and fullcycle-worker are all active since the deploy at 10:21:44Z, with 0 restarts.
- Companion errors since the deploy: 0. `loop_watch.check`: 0 problems.
- Sends: 40 `delivered` in total, 1 `indeterminate` (88803, the known one, aging out). 1 delivered since the deploy.
- Pre-send reconcile: no log line yet. It prints only when a chat has an unresolved send, and none has occurred since the deploy.

## Capacity and state
- Intake `max_active` is 2. Active and working intake projects: 0.
- `fc-*` agent sessions: 0.
- The one pending review is 137 (`gaika-published-risk-test`, blocked). It is unrelated and holds no intake slot.
- Project id `acc-d3-20261118` is free.

## How the engine supports D3 (from the live code)
- **reject.** A `review` decision needs feedback. The task moves to `VERIFICATION_FAILED` and is re-dispatched with "Reviewer rejected: …". This is bounded by `FULLCYCLE_MAX_ATTEMPTS` (3).
- **add_tasks.** This decision exists only on a `project` review. That review opens only when every task is verified and committed **and a project-level check fails**. It fires once per plan size.
  - Alternative, not tested here: `next_tasks` attached to a task approve.
- **Scope** is enforced by the gate on the real changed files (`outside_scope:` violation).
- **Checks** go through the test-runner allowlist (`gates.check_argv`).

## Spec
- Files:
  - `reports/loop/d3_preflight_2026-11-18/spec.json` is the spec to pass to `fullcycle_project_start`. It has no repo, so the project starts from an empty directory.
  - `reports/loop/d3_preflight_2026-11-18/add_tasks_report.json` holds the `next_tasks` for the add_tasks decision.
- Validated with the live `spec.validate_spec` / `validate_task`. All 6 check argvs are allowed by `gates.check_argv`.
- Dry run in scratch: `tests.test_report` exits 1 while the file is missing and 0 once it exists. `discover -s tests` exits 0.

| Part | Content |
|---|---|
| Project check 1 | `python3 -m unittest discover -s tests -v` |
| Project check 2 | `python3 -m unittest tests.test_report -v`. It fails **by design** until `report` is added. The spec goal says so openly. |
| Task `word-count` | `word_count()`, `tests/__init__.py`, `tests/test_word_count.py` |
| Task `char-stats` (after word-count) | `char_count()`, `longest_word()`, `tests/test_char_stats.py`; existing tests unchanged |
| Added task `report` (after char-stats) | `report()`, `tests/test_report.py`, `README.md` |

Each task's scope lists exact files. An initial task therefore cannot create `tests/test_report.py` without a scope violation.

## Tech lead's script and expected transitions
| Step | Tech lead action | Expected record |
|---|---|---|
| 1 | Start `acc-d3-20261118` with `spec.json` | `fc_project`, spec v1; one agent `fc-acc-d3-20261118` |
| 2 | Review R1 (word-count, attempt 1): **reject** with feedback "Acceptance exercise: also count words separated by tabs and newlines, and raise TypeError for non-str input; add tests for both." | `fc_decision_audit` decide/reject; task → `VERIFICATION_FAILED` → attempt 2 |
| 3 | R2 (word-count, attempt 2): approve if the new tests exist and pass | task `verified_done`, attempt=2, committed |
| 4 | R3 (char-stats, attempt 1): approve. Its check reruns the word-count tests (regression). | attempt=1, committed |
| 5 | Project review R4 opens: check 2 failed, `task_count=2` | `fc_review kind='project'`, `failed_checks` names `tests.test_report` |
| 6 | R4: **add_tasks** with `add_tasks_report.json` | spec v2 by `review:R4`; task `report` created |
| 7 | R5 (report, attempt 1): approve | committed; both project checks pass |
| 8 | — | `fc_project_done`, `project_completed` event; agent stops |

Expected totals:
- 4 task reviews and 1 project review;
- attempts word-count=2, char-stats=1, report=1;
- 0 `outside_scope` violations;
- 0 owner escalations;
- 0 `wake_loop_*` events.

## Evidence to collect (metadata only, no chat content)
- **Reviews:** `fc_review` id, kind, status and decision for the project, plus `fc_decision_audit` in order (reject before approve, one add_tasks).
- **Tasks:** state, attempt and committed per task; `fc_spec` versions 1 and 2.
- **Delivery:** one `wake_send_intent` row per review event. Expect state `delivered`, or `indeterminate` with no resend. No event sent twice (dedup), and the audit shows one-way moves.
- **Pre-send reconcile:** count the `presend` journal lines. Any unresolved send that gets resolved there is the first live proof of 758ebe4.
- **Project result:** the repo log and an independent unittest run as `fcagent` after done.

## Risks
- The agent may already satisfy the step-2 feedback in attempt 1. The reject is still valid as an exercise: the feedback asks for its tests explicitly. Record it as a staged reject, not a defect found.
- The agent could see project check 2 and try to create `tests/test_report.py` early. Scope blocks that as a violation, which would surface as a review or block. Record it if it happens.
- The add_tasks path through `next_tasks` on a task approve stays untested here.

## Handoff
The tech lead runs steps 1 to 7 through MCP. I observe read-only and write the acceptance report. Cleanup of both acceptance directories still needs a separate go.
