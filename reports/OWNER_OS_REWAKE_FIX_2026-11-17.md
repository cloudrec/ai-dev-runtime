# Repeated stop wakes for finished agents, and secrets in the access log (2026-11-17)

Trigger: an automated instruction was received (Owner OS API). It is not owner approval. Nothing was deployed, nothing pushed, no agent created, and browser access is unchanged.

## Base and head
| Repo | Branch | Base | Head | Diff |
|---|---|---|---|---|
| runtime | `owneros/autonomy-loop-20261117` | 0966fcb (live code 56f5ad4) | de94158 | `reports/loop/rewake_2026-11-17/runtime.diff` |
| seo | `owneros/log-redact-20261117` | 97cb001 (live) | 39603fb | `reports/loop/rewake_2026-11-17/seo.diff` |

## What was wrong (live data, read-only)
In the last 2 days, `wake_loop_no_progress` was raised 11 times after a `work_stopped_incomplete` and 2 times after a `task_completed`. The final messages fall into four groups:

| Group | Events | Correct behaviour | Before |
|---|---|---|---|
| External dependency wait | WEST 87144, 87495; HU 87157, 87893 | announce once | already fixed by #338 and 8052c40 (these predate it) |
| Completed hand-over to the owner or tech lead | 88186 → 88198 (this pane: "скажи мне «дал»"), ai-terminal 87669 "Решения от вас", fruit 87835 "Остаётся от вас" | announce once | re-woken every 15 min, then escalated |
| Formal stage hand-over (`task_completed` from `agent_stage_done`) | 88001, 87778 | no re-wake while the pane is at rest | re-woken, then escalated |
| Genuine failure or unfinished work | serverwarden 87158 (API error) | keep waking | unchanged, and still woken |

Not covered, and left as they were:
- RO 88151 ("haven't seen a reply" from RU) and bitwhite 88018 (a bare document path). Neither wording states a hand-over, so both keep waking.
- fruit 87892 ("Compacted while idle").

## Fix
**runtime `core/agent_watch.py`.** A new `declares_handoff(tail)` matches an explicit hand-over of the next move:
- Russian: "решение / действие / ответ / остаётся / нужно / требуется **от вас / тебя / владельца / техлида**", "за тобой / вами остаётся", "скажи мне";
- English: "decision / action / input / go-ahead from you / the owner", "over to you", "your call / decision / move".

It applies only when all of these hold:
- the agent's final message, at rest (`quiescent`);
- no error line;
- no negation ("от вас не требуется");
- no "продолжаю / continuing".

`declares_dependency_wait` returns True for it, so it reuses the #338 lifecycle:
- announced once;
- re-armed by a delivered tech-lead task (`note_task_delivered`), more than 300 s of work, a different stop, or the 4 h TTL;
- the closed loop resolves its watch as `agent_dependency_wait`.

Owner gates worded as a blocker or a prompt ("жду подтверждения", "waiting for owner approval") are classified before this and keep their reminders.

**runtime `core/closed_loop_wake.py`.** A watch whose event is `task_completed` from `agent_stage_done` resolves as `stage_done_handoff` while the pane is `quiescent` or `idle`. A working, crashed or prompting pane keeps its watch, and a new stage is a new event with a new watch.

**seo `backend/services/log_redaction.py` and `main.py`.** A filter on `uvicorn.access`, `gunicorn.access`, `uvicorn.error` and `gunicorn.error` is installed when the app is imported. It rewrites:
- `/api/mcp/c/<x>`, `/api/owner/fc-release/<x>` and `/api/owner/fc-page/<x>` to `<redacted>`;
- query values of token / access_token / key / secret / password / ptok / api_key.

The filter never drops a record and never raises.

## Tests (synthetic only)
- **runtime `tests/test_stop_handoff_rewake.py`, 26 tests:**
  - real hand-over wordings, and 10 near misses: negation, carrying on, owner gates, log text, last bullet, an error;
  - announced once over 40 sweeps;
  - unfinished work re-announced;
  - a failure after a hand-over announced;
  - a new task re-announced;
  - the closed loop: no re-wake or escalation for 88186, 87669 and 87835 over 4 SLO windows;
  - unfinished work re-woken and escalated;
  - a real failure never suppressed (`agent_process_failed` too);
  - a `stage_done` hand-over resolved, another emitter's `task_completed` kept, a crashed `stage_done` pane kept.
- **runtime mutants, all killed (on a scratch copy):**
  - hand-over not parked: 7 failed;
  - no `stage_done` resolution: 1;
  - no negation guard: 1;
  - no carrying-on guard: 2;
  - `stage_done` from any source: 1;
  - errors parked: 5;
  - a crashed pane resolved: 1.
- **seo `backend/tests/test_log_redaction.py`, 12 tests, dummy tokens only.** Mutants, all killed:
  - filter not installed by main: 1 failed;
  - page key kept: 5;
  - args untouched: 3;
  - case-sensitive query: 1.
- **seo end-to-end through a real gunicorn `UvicornWorker` with a dummy token** (container without network):
  - with the filter, the log line is `"POST /api/mcp/c/<redacted> HTTP/1.1" 200` and the dummy appears 0 times;
  - control without the filter: 1 time.

Results:
- runtime focused (new tests, #338, closed loop, agent watcher, wake bridge, send queue): 331 passed.
- runtime full suite at de94158: 5426 passed, 4 skipped, EXIT 0.
- seo, on a disposable PostgreSQL on an internal network:
  - focused (MCP, owner, auth, security, log redaction): 229 passed; the only skip is DNS;
  - full: 48 failed / 1082 passed, against 48 / 1070 for the base 97cb001 run the same way; the failure lists are identical, none in MCP, owner or logging.
- Log: `reports/loop/rewake_2026-11-17/results.txt`.

## Residual risks
1. A stage hand-over the reviewer silently ignores is not reminded. The first wake is verified: submitted, and the assistant started generating. After that, only the next stop, task or 4 h TTL brings it back.
2. The hand-over phrases are a list. A new wording falls back to the old behaviour: woken, never silenced. That is the safe direction.
3. A pane that hands over and then hangs for real looks the same as a parked one until the 4 h TTL.
4. Redaction covers the three known secret routes and common query names. A new secret-in-path route must be added to `_SECRET_PATH`.
5. Earlier container logs still hold old lines. The current backend container was recreated at 15:01Z. Its lines are left as they are; nothing was read or printed.

## Release, when authorised (non-payment Owner OS, through the installers)
- **runtime:** `TARGET_SHA=de94158 deploy/adaptive_offer/install.sh --check`, then `--apply`.
- **seo:** fast-forward the deployed branch 97cb001 → 39603fb, with `--check` and rollback the same way as `deploy/seo_undismiss`.
