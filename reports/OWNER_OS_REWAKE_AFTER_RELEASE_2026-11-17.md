# Watchdog wakes that recurred after the de94158 release (2026-11-17)

Trigger: an automated instruction was received (Owner OS API). It is not owner approval.

Scope:
- DEV only. Live data was read-only: event metadata, delivery and watch rows, no pane text beyond the stored excerpts.
- No deploy, no push, no live state changed, no secret read.
- No extra agent was started. The root-cause review was done in this Opus session, so a second Opus agent would have been redundant.

## Base and head
| Repo | Branch | Base (live) | Head | Diff |
|---|---|---|---|---|
| runtime | `owneros/autonomy-loop-20261117` | de94158 | ee9f1fa | `reports/loop/rewake2_2026-11-17/runtime.diff` (3 files, +371 −7) |

## Timeline (sanitized metadata)
The wake companion restarted on de94158 at 19:59:20Z. Its watches are kept in the DB and were evaluated by the new code after the restart. Every wake below was delivered (`submitted_and_assistant_started_generating`) and acknowledged.

| Agent | stage_done (created → delivered) | Its watch | Stop event 5–8 min later (created → delivered) | Its watch |
|---|---|---|---|---|
| HU | 88277 19:50:31 → 20:00:54 | resolved `stage_done_handoff` | 88281 19:57:50 → 20:04:58 | re-woke 88309, escalated 88316 |
| WEST | 88284 19:58:32 → 20:06:51 | resolved `stage_done_handoff` | 88302 20:06:52 → 20:14:34 | re-woke 88314, escalated 88321 |
| PL | 88298 20:02:34 → 20:12:38 | resolved `stage_done_handoff` | 88303 20:08:50 → 20:16:28 | re-woke 88315, escalated 88322 |
| autonomy-loop | 88292 20:00:40 → 20:10:44 | re-woke 88312, escalated 88317 | prompt 88295 20:00:56 → 20:08:48 | re-woke 88313, escalated 88318 |

These are not delayed events:
- Deliveries lag creation by 2–10 min. That is the normal cooldown and queue lag.
- Only 88281 was created before the restart. Its re-wake (20:23Z) and escalation (20:39Z) were made by the new code, and the new code would not have resolved it either.
- The de94158 fix does work for what it covers: every stage_done watch resolved. The re-wakes come from two gaps next to it.

## Root causes
1. **A stage_done hand-over was announced a second time.**
   - The agent runs `tools/stage_done.py`, writes its last lines, and rests.
   - 300 s later agent_watch sees an unchanged pane with no finish words and emits `work_stopped_incomplete` for the same rest.
   - agent_watch never looked at the stage_done event.
   - The de94158 resolution applies only to the `task_completed` watch, so the duplicate stop watch was re-woken and escalated.
   - The final wordings ("RU should decide on it", "standing by for captures or a re-review request", "Полный набор не гонял") matched no hand-over phrase. The phrase list was the wrong layer for this.
2. **A statement was read as an owner prompt.**
   - `_OWNER_PROMPT_RE` contained the bare stem `подтверд`.
   - This pane's release report said "Подтвердится на деле, когда…". That made 88295 `agent_prompt_needs_response`. Its watch re-woke and escalated, and the pane's class `owner_prompt` kept the stage_done watch 88292 from resolving.
   - After compaction the same line was still on screen, which gave 88326.

**Stale unsubmitted prompt text:** checked, not a cause.
- Composer lines (`❯ …` and their indented wrap) are already stripped before classification. That covers an owner draft and the CLI's suggested prompt.
- Tests pin this with drafts such as "Подтвердите деплой?", "что дальше делаем?", "(y/n)" and "1. Yes", including a wrapped one.
- The watcher only reads the pane. Nothing is ever submitted.

## Fix (ee9f1fa)
- **`core/agent_watch.py`**
  - `_stage_done_covers(target, digest_since, task_ts)`: a `task_completed` from `agent_stage_done` for this pane exists, after the last task the tech lead or owner delivered (`task_ts`, a new column set by `note_task_delivered`), and at most `REARM_WORK_SECS` (300 s) before the pane's final output.
  - When that holds for a `quiescent` stop with no error line, the rest is recorded as notified and parked as a declared wait. No second event is emitted. A new task, more than 300 s of new work or a different stop re-arm it, as before.
- **`core/closed_loop_wake.py`**: `_stage_done_rest` resolves a `work_stopped_incomplete` watch about such a rest as `stage_done_handoff`, using the same evidence. This covers watches opened before the release or by a racing sweep. It never resolves when the stored excerpt carries an error.
- **Owner-prompt pattern:** only requests match ("подтверди / подтвердите / подтвердить / подтвердишь", "подтверждаете …?"). Statements such as "подтвердится", "подтвердил", "подтвердилось" no longer match.
- **HU wording:** "standing by for captures" is a declared wait.

Unchanged: a crash, an error line, a question, a blocker and a completion follow their own paths. Permissions, payment exclusions and approvals are untouched.

## Tests (synthetic panes and DBs, live timings)
**New file `tests/test_watchdog_rewake_after_release.py`, 31 tests:**
- HU and WEST stage_done rests: not announced again over 40 sweeps.
- The stage_done watch resolves.
- A stop watch opened by the old code, then a restart (new connections, same DB): resolved, with no re-wake.
- The 88292/88295 replay: no prompt and no second stop. The watch resolves.
- Controls, all still announced and re-woken:
  - the same stop without stage_done (re-woken and escalated);
  - a stage_done older than the new work;
  - a task delivered after the stage, at once or later;
  - an error after the stage;
  - a crash after the stage;
  - another agent's stage_done;
  - a `task_completed` from another emitter.
- Real confirmation requests stay owner prompts. Past and future statements do not.
- Composer drafts are never read as the agent's prompt.

**On base de94158:** 13 of the 31 fail, which reproduces the live chains.

**Mutants, all 10 killed** (scratch worktree):
- no parking in the watcher;
- no `task_ts` bound;
- no 300 s window;
- any source counts as stage_done;
- no error guard in the watcher;
- no closed-loop resolution;
- no error guard in the closed loop;
- the old stem;
- no HU phrase;
- `task_ts` not written.

**Suites:**
- Focused (new file, hand-over, dependency wait, agent watch, closed loop, wake bridge, send queue): 542 passed.
- Full suite at ee9f1fa: 5457 passed, 4 skipped, EXIT 0 (`reports/loop/rewake2_2026-11-17/results.txt`).

## Residual risks
1. A stage hand-over the tech lead ignores is not reminded, as already noted for de94158.
2. A task delivered by a path other than the API (the owner typing directly into the pane) does not set `task_ts`. If that task ends unfinished within 300 s of the previous stage_done, its stop is parked.
3. A pane that hands over and then hangs looks parked.
4. PL 88327 (21:03Z) is a new stop after new work. It is correctly not covered and is announced.

## Release, when authorised (non-payment Owner OS)
`TARGET_SHA=ee9f1fa deploy/adaptive_offer/install.sh --check`, then `--apply`. Rollback is through the backup the installer prints. The schema change is one added nullable column (`agent_watch_state.task_ts`). Old code ignores it.
