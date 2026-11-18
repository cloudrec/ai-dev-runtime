# Disposable Full Cycle acceptance `acc-d3`: reject, rework, add_tasks (2026-11-18)

Trigger and scope:
- An automated instruction was received (Owner OS API). It is not owner approval.
- The tech lead created the project (`api:client/chatgpt-tech-lead`, 10:28:02Z) and made every decision through MCP.
- I observed read-only: DB metadata and the journal. No chat or transcript content was read.
- Runtime: live `758ebe4`.
- The actual spec was read from the DB, not assumed from the preflight. Its files are `text_stats.py`, `tests/test_word_count.py`, `tests/test_char_stats.py` and `tests/test_report.py`. Project check 2 is `python3 -m unittest tests.test_report`, failing by design until add_tasks.

## Verdict: PASS. D3 is proven live.
| Part | Result |
|---|---|
| reject → rework | R140 (word-count, attempt 1) was **rejected** at 10:30:39, a planned acceptance exercise and not a defect. Attempt 2 was dispatched at 10:30:59 and **approved** as R141 at 10:32:20. |
| regression | R142 (char-stats, attempt 1) was approved. Its check re-ran the word-count tests. |
| add_tasks | Project review R143 opened when check 2 failed. The **add_tasks** decision at 10:36:23 created spec v2 by `review:143` and task `report`. |
| final | R144 (report, attempt 1) was approved at 10:40:47. `fc_project_done` holds event 89028. Both project checks pass: 20 tests, and `tests.test_report` OK. |
| one agent | Only `fc-acc-d3`. `agent_alive` is False after done. Intake working: 0. |
| gates | 0 blocked, dirty or permission reviews. 0 scope violations. 0 owner escalations. |

Attempts: word-count 2, char-stats 1, report 1. That gives 4 task reviews and 1 project review, matching the preflight.

Repo after done: `829339d` baseline → `c79c86f` word-count (attempt 2) → `844e082` char-stats → `0b76d9c` report. The tree was clean.

## Transport (wake per review event)
| Event | Review | Outcome |
|---|---|---|
| 88988 | R140 | Skipped (`actionable_cooldown_active`), then expired unsent as `fullcycle_review_already_decided`. The tech lead was already in a turn and polled. |
| 88996 | R141 | Same as 88988. |
| 89004 | R142 | Send intent → **delivered** at the first attempt (`submitted_and_assistant_started_generating`). The watch resolved as `progress_observed`. |
| 89008 | R143 | **delivered** at the first attempt. Watch `delivery_verified=1`. |
| 89020 | R144 | **delivered** at the first attempt. Watch `delivery_verified=1`. |
| 89028 | project done | Decided wake, not yet sent at the time of the report. Informational. |

- Sends: 3 of 3 delivered at the first attempt.
- 0 `indeterminate`.
- 0 duplicates: one `wake_send` row per event, and one-way audit `→intent, intent→delivered`.
- 0 `wake_loop_*` events.
- 0 companion errors.
- The pre-send reconcile of 758ebe4 was not triggered: there were no unresolved sends, so it is still unproven live.

## Cleanup (owner's direct word "удаляй оба учебных проекта")
- `/opt/fc-projects/acc-20261118` was removed at 10:39Z.
- `/opt/fc-projects/acc-d3` was removed at 10:41Z, after done.
- Evidence was saved before each removal: git log, file list and a unittest run. See `reports/loop/acceptance_cleanup_2026-11-18/`.
- DB rows (projects, specs, reviews, decision audit, wake records) are kept.
- No fcagent tmux session remained.

## Remaining
- Pre-send reconcile (758ebe4) is unproven live until an `indeterminate` send occurs.
- The add_tasks route through `next_tasks` on a task approve is still untested live. The project-review route is proven.
- D2, post-send CDP timeouts: none in this run, so the cause is still unmeasured.
