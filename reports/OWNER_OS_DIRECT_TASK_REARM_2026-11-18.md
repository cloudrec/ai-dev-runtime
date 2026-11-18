# A task typed into the pane after stage_done (2026-11-18)

Trigger: an automated instruction was received (Owner OS API). It is not owner approval.

Scope:
- DEV only. No deploy, no push, no live task submitted, no agent created.
- No payment agent, secret, permission or access path was touched.
- No permission prompt was answered or bypassed.

## Canonical state
| | SHA |
|---|---|
| Live runtime | ee9f1fa (since 2026-11-18 04:16Z) |
| Branch `owneros/autonomy-loop-20261117`, code | c1f629f |
| Diff | `git diff ee9f1fa c1f629f -- core tests`, saved as `reports/loop/direct_task_2026-11-18/runtime.diff` |

Diff size: 2 files, `core/agent_watch.py` and the new `tests/test_direct_task_after_stage_done.py`.

## The blind spot (ee9f1fa residual 2)
1. The agent runs `stage_done` and stops.
2. Within the 5-minute quiescence window, the owner types a new task straight into tmux.
3. The agent stops it unfinished.

The stop came within `REARM_WORK_SECS` of the stage_done, and only API deliveries set `task_ts`. So ee9f1fa parked it as the old hand-over, and the unfinished task was never announced.

## Fix: evidence of a submitted prompt
**Source:** the session's own transcript.
- Claude Code writes a `user` entry with `origin.kind == "human"` only when a prompt is submitted.
- These never count: an unsent draft in the composer, the suggested prompt, a recalled ghost, tool results, task notifications, compaction summaries, `isMeta` entries and local command output.

**Session:**
- It is resolved from the pane's Claude pid through `native_sessions`, the runtime's own list.
- It is never "the newest file in the directory", so two agents sharing a cwd are not mixed.

**Attribution:**
- Automated sends type into the same composer. A prompt with an `agent_control` delivery to that session within `DELIVERY_MATCH_SECS` (90 s) is attributed to the delivery.
- An API delivery re-arms, as `note_task_delivered` already does.
- An internal nudge does not re-arm, so the #338 suppression stands.

**Persistence:**
- The prompt time is stored as `agent_watch_state.task_ts`, which only ever moves forward.
- The closed loop, later sweeps and a restarted worker all read the same value.
- Prompt text is never stored or compared; only its time is read.

**When the evidence is unknown** (no session, unreadable transcript), it counts as no evidence, and the ee9f1fa behaviour is kept.

**Cost:** the transcript is read only when a quiescent stop is about to be parked as a stage hand-over. Reads are bounded to 2 MB.

## Tests
**New file `tests/test_direct_task_after_stage_done.py`, 20 tests** (synthetic transcripts, panes and DBs; nothing is typed). Behaviour on ee9f1fa vs c1f629f:

| Case | ee9f1fa | c1f629f |
|---|---|---|
| Task typed in tmux 30 s after stage_done, stopped 90 s later | parked (the old failure) | announced |
| The same, swept 40 more times | — | announced once (dedup) |
| A nudge more than 90 s before the prompt | masked | announced |
| Evidence after a restart (fresh connection) | none | `task_ts` persisted; the stop watch is re-woken |
| No prompt; unsent draft; suggested prompt | parked | parked |
| Task notification (also with "human" in another field), meta, compaction summary, command output, tool result | parked | parked |
| Internal nudge within 90 s | parked | parked |
| Prompt before the stage_done (no stale-task resurrection) | parked | parked |
| Another session in the same cwd; unknown session | parked | parked |
| API task (`note_task_delivered`) | announced once | announced once |
| A question after the new task | owner prompt | owner prompt |
| An error stop | announced | announced |
| Prompt text in either DB | — | absent |

On ee9f1fa, 4 of the then 19 tests failed, exactly the rows that change.

**Mutants, all 10 killed** (scratch worktree):
- no direct check;
- no persistence;
- any origin counts;
- meta counts;
- tool result counts;
- no "after stage_done" bound;
- newest file in the cwd;
- nudges count;
- no 90 s window;
- unknown session falls back to the newest file. My mutation script applied this one wrongly, so it was run by hand and killed.

**Live read-only sanity check**, on this pane's own transcript, times only: two submitted prompts in 6 h, at 04:16 (typed in the pane) and 04:20 (API delivery). Task notifications were correctly not counted.

**Suites:**
- Focused (both new files, hand-over, dependency wait, agent watch, closed loop, wake bridge, send queue, native sessions, agent_send): 604 passed.
- Full suite at c1f629f: 5477 passed, 4 skipped, EXIT 0 (`reports/loop/direct_task_2026-11-18/results.txt`).

## Residual risks
1. A prompt typed by a person within 90 s of an internal nudge is attributed to the nudge and stays parked. That is the old behaviour, only in that narrow window.
2. If `claude agents --json` cannot answer (timeout, a down binary), the evidence is unknown, and the old behaviour applies.
3. More than 2 MB of transcript written after the prompt hides it. A stop within about 5 minutes of a stage_done makes that unlikely.
4. A queued prompt (typed during an active turn) is written when Claude takes it, so its time can be later than the typing. It still comes after the stage_done.

## Release, when authorised (non-payment Owner OS)
`TARGET_SHA=c1f629f deploy/adaptive_offer/install.sh --check`, then `--apply`. There is no schema change: `task_ts` already exists live.

## Proposal: disposable non-payment Full Cycle acceptance (not started; no new agents yet)
**Goal.** Prove the whole loop on the live system with a throwaway project:
- spec in ChatGPT;
- project and sandboxed agent;
- stage hand-overs;
- review;
- done.

The wakes must be one per stage, with no `no_progress` or `stalled` chain.

**Set-up** (existing pieces only):
- The Full Cycle intake (`core/fullcycle/intake.py`), which is live: a fresh git repo under `FULLCYCLE_INTAKE_ROOT`, and the agent `fc-<id>` as `fcagent`, sandboxed, started by `fullcycle:engine`.
- A toy spec with no network, no secrets and no money. For example, "a `calc` package: `add`, `mul`, a CLI, with pytest checks", the same shape as the isolated toy runs of 2026-11-17 (`reports/loop/e2e_2026-11-17`).

**Steps:**
1. The tech-lead chat starts the project through its MCP Full Cycle tools with that spec. This creates one sandboxed agent, so it needs the tech lead's or the owner's go.
2. Observe, read-only, for each task:
   - `dispatched`, then `AGENT_DONE`, then `VERIFYING`;
   - `review_requested`, with exactly one wake;
   - the reviewer's decision;
   - `committed`.
3. Let one review return `add_tasks`, to prove the re-arm path.
4. The project review approves. The worker stops `fc-<id>`.

**Pass criteria:**
- every stage produces exactly one delivered wake;
- `wake_loop_no_progress` and `wake_loop_stalled` are 0 for `fc-<id>`;
- the project ends `done`, and the agent is stopped;
- loop_watch is clean throughout;
- payment panes are never offered.

**Fail handling.** Nothing to roll back in production: the project directory and the agent are disposable. Stop the agent, remove `<root>/<id>` and record the evidence.

**Out of scope.** Typing into the pane (no live task submission), any payment agent, and any permission change.
