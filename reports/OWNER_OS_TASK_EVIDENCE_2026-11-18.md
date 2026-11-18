# Task evidence after a stage_done: the three c1f629f residuals (2026-11-18)

Trigger: automated instructions were received (Owner OS API), one to start and one to resume after an API connection loss. They are not owner approval.

Scope:
- DEV only. No deploy, no push, no live state changed, no agent created.
- No payment agent, secret, permission or access path was touched.
- No transcript content was printed or exported. The checks ran on synthetic transcripts. The one live sanity check printed a verdict and times only, and wrote to an in-memory DB.
- No owner approval was given automatically and nothing was retried blindly.

## Candidates (canonical)
| | SHA | Status |
|---|---|---|
| Live runtime | ee9f1fa | live since 2026-11-18 04:16Z |
| Previous candidate | c1f629f | kept on branch `owneros/candidate-c1f629f`; release plan in `OWNER_OS_DIRECT_TASK_REARM_2026-11-18.md` |
| **New candidate** | **0a0c456** (branch `owneros/autonomy-loop-20261117`) | supersedes c1f629f if approved |

Diff c1f629f..0a0c456: `reports/loop/task_evidence_2026-11-18/runtime.diff`, 4 files, +533 −119:
- the new `core/task_evidence.py`;
- `core/agent_watch.py`;
- 2 test files.

## Residuals: reproduced, then closed
| # | c1f629f behaviour | Reproduced by | 0a0c456 |
|---|---|---|---|
| R1 | A human prompt typed within 90 s of an automated nudge was attributed to the nudge, so the stop was parked. | `test_r1_a_human_prompt_right_after_a_nudge_is_a_task` | Attribution is by content. The prompt's sha256 (as typed, and unwrapped from a `<pasted_content>` block) is compared with the `delivery_provenance` fingerprint of each delivery to the session within 600 s. A match goes to that sender: API = task, anyone else = nudge. No match = human. |
| R2 | With no answer from `claude agents --json`, the evidence was unknown and the stop was parked silently. | `test_r2_*` (5 tests) | Identity order: the runtime list, then Claude Code's own `~/.claude/sessions/<pid>.json` (pid must match), then the identity last resolved for the same pid, if its transcript was written after the stage. The identity is stored in `agent_watch_state` (`tr_pid`, `tr_sid`); a new session id, such as after /clear, resets the checkpoint. A restarted Claude (new pid) is never matched to the old identity. |
| R3 | Only the last 2 MB were read, so a prompt followed by more output was invisible. | `test_r3_*` (4 tests) | A binary search on line timestamps finds the stage moment, then a forward read is capped at `SCAN_BUDGET_BYTES` (16 MB). A persisted checkpoint offset is never past the stage. A 24 MB history costs under 4 MB of reads; on this pane, 0.39 s. |

Live check (read-only, this pane's own transcript, an in-memory state DB): the newest prompt was correctly attributed as an API delivery. Earlier on the same transcript, all 10 automated deliveries matched by hash and all typed prompts did not.

## Verdicts and what they do
| Verdict | When | Effect |
|---|---|---|
| `task` | a submitted prompt after the stage that a human or an API caller sent | `task_ts` is recorded. The stop is announced and the closed loop works as usual (re-wake and escalation allowed). |
| `none` | transcript read to the end, no such prompt (internal nudges only, drafts, notifications, meta entries) | parked as the stage hand-over, as in ee9f1fa |
| `unknown` | no session; no transcript; more than the budget written since the stage; an **unfingerprinted (legacy) delivery** within 90 s of the prompt; an unreadable delivery store | **Announced once**, with `stage_done_evidence: unknown:<reason>` in the payload. Its watch stays the stage's rest, so it is not re-woken or escalated. Never parked silently, never guessed as a task. |

These stay as they were:
- dedup (one announcement over 40 sweeps);
- questions (`owner_prompt`);
- error stops and crashes;
- payment exclusions.

Prompt text is hashed in memory only. Neither DB contains it, and a test checks that.

## Tests
- **`tests/test_direct_task_after_stage_done.py`**, now 36 tests: on c1f629f, **15 fail**; on 0a0c456, 36 pass.
  - The 15 are all R1–R3 cases, the unknown semantics, and the payload field.
  - One residual test, the 24 MB history, also passes on c1f629f, which was bounded too.
- **Mutants, all 14 killed** (scratch worktree):
  - no hash attribution;
  - no unwrap;
  - an unfingerprinted delivery counted as human;
  - no pid file;
  - pid file for any pid;
  - no durable identity;
  - durable identity for any pid;
  - no checkpoint reset on a new session;
  - no binary search;
  - backlog counted as none;
  - checkpoint written past the stage;
  - unreadable store counted as human;
  - unknown parks;
  - unknown re-arms as a task.
- **Focused** (both stage-done files, hand-over, dependency wait, agent watch, closed loop, wake bridge, send queue, native sessions, agent_send, agent privilege): 756 passed.
- **Full suite at 0a0c456:** 5493 passed, 4 skipped, EXIT 0 (`reports/loop/task_evidence_2026-11-18/results.txt`).

## Remaining risks
1. If the session cannot be resolved (runtime list down, no pid file, no earlier identity), the result is `unknown` and the stop is announced once. That is one possible extra wake, no loop.
2. More than 16 MB written between the stage and the stop gives `unknown`, announced once.
3. If Claude Code changes how it stores a paste (other than `<pasted_content>`), automated sends stop matching by hash. Such a send then counts as human, so the stop is announced once and its watch can be re-woken. This fails toward waking, never toward silence.
4. Deliveries older than the 24 h store retention cannot be attributed. Only prompts after a recent stage_done are examined.
5. As before, a hand-over the tech lead ignores is not reminded.

## Release, when authorised (non-payment Owner OS)
`TARGET_SHA=0a0c456 deploy/adaptive_offer/install.sh --check`, then `--apply`.
- Schema: four nullable columns, added on the first sweep.
- Rollback: through the backup the installer prints.

c1f629f stays releasable on its own, under the plan in its report.

## Disposable non-payment Full Cycle acceptance: specification (not launched)
**Purpose.** One live, end-to-end proof:
- a spec from the tech-lead chat;
- a project with a sandboxed agent;
- per-task review;
- project done.

Exactly one wake per stage, and no `no_progress` or `stalled` chain.

**Exactly one agent:**
- one project `acc-<yyyymmdd>`, created by the live intake (`FULLCYCLE_INTAKE_ROOT=/opt/fc-projects`, run as `fcagent`, sandboxed);
- one agent `fc-acc-<yyyymmdd>`, started by `fullcycle:engine`;
- no other agent is created, messaged or reused.

**Spec** (no network, no secrets, no money; a fresh empty repo, never an existing tree):
- package `calc`: `add(a, b)`, `mul(a, b)`, a CLI `python -m calc add 2 3`;
- checks: `pytest -q` over the project's own tests;
- three tasks: `add`, `mul`, `cli`.

**Run:**
1. The tech-lead chat calls `fullcycle_project_start` with the spec. This needs the tech lead's or the owner's explicit go: it creates the one agent.
2. Read-only observation per task:
   - `dispatched`, `AGENT_DONE`, `VERIFYING`;
   - `review_requested`, with exactly one delivered wake;
   - `fullcycle_review_decision`;
   - `committed`.
3. One decision returns `add_tasks` (for example, a `--help` task), to prove the re-arm path.
4. The project review approves. The worker stops the agent.

**Pass criteria:**
- every stage gets exactly one delivered wake;
- 0 `wake_loop_no_progress` and 0 `wake_loop_stalled` for the agent;
- the project ends `done`, and the agent is stopped;
- loop_watch is clean throughout;
- payment panes are never offered;
- no owner action is needed.

**Abort rules:**
- any payment, permission or secret prompt;
- any wake chain;
- more than 60 minutes elapsed.

On abort: stop the agent and record the evidence.

**Cleanup** (always):
1. Confirm the agent pane is gone.
2. Archive the project's event and review ids in the report.
3. Remove `/opt/fc-projects/acc-<yyyymmdd>`.

Nothing in production is changed, so there is nothing to roll back.
