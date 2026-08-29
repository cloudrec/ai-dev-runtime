# Runtime job 87 cancelled as superseded (2026-08-29)

Owner-authorized, scoped to job 87 only. No deploy, no config, no credential, no
external call.

## The job

`dde9cd25-2fe9-403d-aa02-dad0ba86b9bf`, task_id 87, goal
**"Finish GitHub issue #10, then execute #11"**. Created 2026-07-14T17:52:17Z,
`autonomy=execute_safe`, `approval_required=1`, project `/root/ai-dev-runtime`.

It sat in `waiting_approval` for ~46 days and **never executed**: one log line
(`created`), empty `plan` / `tests` / `validation`, `artifacts=[]`, null
`heartbeat_at`. There was therefore no execution health evidence to gather — the
absence was the finding, not a measurement gap.

## Why superseded

* **Issue #10 is already fixed in-tree.** `core/ai_planner.py:19` carries
  "Root cause of the 180s/900s planner hangs (issue #10)"; commit `8ac76f2`
  ("stop `claude -p` from going agentic and hanging past any timeout") names
  issue #10 and the two jobs that exhibited it. `c4ceebd` references the issue #10
  canary task.
* **The inventory half is covered** by task 94, "[RETRY] Inventory all GitHub
  issues and reconcile portfolio", status **completed**, created
  2026-07-15T17:40:35Z — the day after job 87.

**Explicitly NOT proven: issue #11's current state.** Confirming it requires a
GitHub API read (network + credentials), outside the read-only boundary set for
this work. If #11 is still outstanding it needs a fresh job; this 46-day-old
record, with no plan and a first half already fixed, was not the vehicle for it.

## Dependency check before cancelling

* `control_plane.event` rows referencing the job id or entity: **0**.
* `os_task` rows mentioning issue #10 / #11: **0**.
* Other job rows with task_id 87: **none** (no retry sibling, no child).
* Only indirect consumer: `waiting_approval` is a standing owner-decision signal
  (`runtime_events.py:36` -> `owner_decision_required`, high, wakes;
  `runtime_watchdog.py:16` treats it as never a stall). Cancelling retires that
  signal — the sole behavioural consequence, and the intended one.

## What cancellation did

`POST /api/v1/jobs/{id}/cancel` (the production interface, not a direct db write).
Per `api/v1.py:218-225` it sets `status='cancelled'`, stamps `finished_at`, and
appends one log line. Nothing else: the job never planned, so there is no branch,
no file, no backup and no deploy artifact to unwind.

## Verified after

| Check | Result |
| --- | --- |
| job 87 | `cancelled`, `finished_at=2026-08-29T21:33:19Z` |
| job 87 logs | exactly 2 lines: `created`, `cancelled` |
| event emitted | `13507` `runtime_job_state`, **severity info, owner_action_required=0** |
| owner wake / notification | none — the info mapping in `runtime_events.py:40` |
| other `waiting_approval` jobs | 12 -> **11**; only 87 moved |
| total jobs | **106, unchanged** |

No other job was cancelled or mutated. 11 `waiting_approval` jobs remain, several
of them obvious test debris (empty task_id, goal `g`); none was touched.
