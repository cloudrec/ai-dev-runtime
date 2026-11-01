# Owner OS autonomy: independent security and concurrency review (Task #328), 2026-11-01

**Reviewer: Claude Opus 5.5, in a fresh context. This is not a Fable review. The Fable final audit is still PENDING.**
This review started from an automated instruction received through the Owner OS API. It is not owner approval, and it gives no PROD approval.
**Live unattended E2E: NOT PROVEN. PROD: NO-GO.**

## Scope and method
- Worktree: `/opt/owneros-autonomy`, branch `owneros/autonomy-finish`.
- HEAD is **`cbe4744`**. Running `git diff --quiet dca2287 HEAD -- core tools hooks config tests` shows the code is identical to **`dca2287`**.
  - The intake says HEAD is `2784d5a`. That value is stale: `cbe4744` is the docs-only commit that added the intake itself.
- Diff reviewed: `9557004..dca2287` over `core hooks config` (9 files, +700/−57). I also checked the tools and tests stat.
- I read the source myself. All developer claims were treated as untrusted.
- I changed **no code**. I touched nothing in `/root/ai-dev-runtime`: no tests, edits, DB, grants or pushes there. The only read outside this worktree was `~/.claude/settings.json`, to find where the hook is registered.
- Targeted repros live in the scratchpad only (`test_review_repro.py`, 2 tests). They ran in this worktree, with `tests/conftest.py` providing isolated DBs, and both passed. Each one confirms a defect.
- I did not re-run the full suite.

## Stage 10 suite claim: VERIFIED from the log
I read `reports/stage10/full_suite_dca2287.txt`:
- line 1: `sha: dca2287b908e…`
- command: `python3 -m pytest -q -p no:cacheprovider tests/`
- rootdir: `/opt/owneros-autonomy`
- `collected 4324 items`, then `4320 passed, 4 skipped, 5 warnings in 660.61s`, `exit=0`
- the git status at run time was only `?? reports/stage10/`

Caveat: the log is a plain text file written by the implementer. It is not tamper-evident. A green suite does not cover D-1 or D-2: the live `quiescent` mapping test never passes `"idle"`, and no test has a blocked result arrive after an answered stall.

## Verdicts
| Q | Verdict | Evidence |
|---|---|---|
| Q1 second envelope while active | **DEFECT** | D-1 (idle detection is inverted) and D-3 (the resend fence runs only once). I found **no** path that sends while the agent reads `working`, `shell_running` or `waiting_owner`: the fence (`engine.py:833-851`) holds, and `agent_send`'s busy refusal (`agent_control.py:2237`) is a second barrier. |
| Q2 stall alert once / on restart | **DEFECT** | Once-only holds: the row is unique per (task, attempt, kind) (`engine.py:537-546`), and the deadline comes from the persisted `fc_dispatch.ts` (`:809-812`, `:941-944`). Superseding on a late result or a crash is correct and idempotent (`:790-806`). But the shared unique slot causes **D-2**. |
| Q3 self-report as evidence | **CONFIRMED-SAFE** | When git evidence is missing, `_verify` fails closed with `changed_files_unverifiable` (`:621-625`). The claim is kept only as `claimed_changed_files`. `_git_refusal` runs on every dispatch (`:419`). `_commit_pending` is fail-closed (`:1058-1072`). Note D-10: the agent's text still reaches the tech lead without a label. |
| Q4 stuck / two at once | **DEFECT** | Two at once: none found. `_parked` needs zero `fc_dispatch` rows (`:444-451`), and a resumed parked task waits for the task in flight (`:460-465`). Stuck: **D-2** strands a task in BLOCKED forever (confirmed by repro). **D-1** together with D-7 makes stall-cancel and stall-resend hang forever. Because the engine runs one executor, a stuck task blocks the whole project. |
| Q5 rotation guard (D2b) | **DEFECT** | D-3: the blind window covers every dispatch whose send is not yet ok, not just the first one. D-4: the guard does not protect a live attempt that is waiting on a permission dialog. |
| Q6 live DB migration | **NEEDS-DECISION** (+ D-5) | The migration is additive. `superseded` is correctly excluded by `pending_reviews` (`:143`), the settled check (`:1345-1347`) and `wake_bridge._fullcycle_review_closed` (`wake_bridge.py:1729`). The ALTER is not race-safe (D-5). It needs a backup plus a one-off migration step before promotion. |
| Q7 Stage 3 changes | **CONFIRMED-SAFE** (low notes) | The hook is registered at `/root/ai-dev-runtime/hooks/owneros_hook.py`, which `readlink -f` resolves to itself. So the new `sys.path` (`hooks/owneros_hook.py:39`) is byte-equivalent in PROD, and `_RUNTIME_ENV` is unchanged. Blast radius: none at the registered path. The planner and base_ref changes are bounded: replan is off by default, the config file is empty, and strict mode is off. Behaviour change: a tool-call reply from the planner now **blocks** the job instead of using the fallback plan (tech-lead acknowledgement). D-11 is low. |

## Prioritized defect list

### HIGH
**D-1: the live adapter's idle detection is inverted** (`core/fullcycle/tmux_adapter.py:135`)
- `_QUIESCENT = ("waiting_input", "dead", "stale")`. But `agent_control.classify_state` returns **`"idle"`** for an agent that has finished its turn and is at rest (`agent_control.py:1415`). Here `"idle"` maps to `None` ("cannot tell").
- `"waiting_input"` means typed or pasted text that was never submitted is sitting in the composer (`:1395-1398`).
- Effects on a live agent:
  - (a) An agent that went silent and is now really idle (the commonest stall) is **never** "provably idle". The tech lead's resend and cancel then wait forever (`resend_fenced` / `stall_cancel_fenced`, `engine.py:850,885`). There is no second alert, and the project is blocked.
  - (b) An unsubmitted staged prompt (for example, a stuck paste of the previous envelope) reads as idle, so the next envelope is pasted on top of it.
- Repro: scratch test `test_R1_idle_agent_is_not_quiescent` (passes: `quiescent()` returns None for `"idle"`). The branch's own mapping test (`tests/test_fullcycle_stall.py:287-298`) never covers `"idle"`.
- Fix:
  - Make quiescent = `("idle", "dead", "stale")`.
  - Treat `waiting_input` as **not** quiescent (False or None).
  - Add a test that feeds the real `classify_state` outputs into the mapping.

**D-2: a late blocked or context_exhausted result after an answered stall strands the task** (`engine.py:766-772`, `:740-741`)
- Sequence:
  1. The stall alert for attempt N is decided `answer`.
  2. The fence is held, because the agent is not quiescent.
  3. The agent then writes a result with `status: "blocked"` (or `context_exhausted`).
  4. The fence sees DONE (`:840-847`). `_step_working` moves the task to BLOCKED, and `_blocked_review` calls `_request_review`.
  5. `INSERT OR IGNORE` hits the stall's own unique slot. No row and no event are created.
  6. `_step` on BLOCKED without a gate finds that a "blocked" review exists and does nothing.
- Result: BLOCKED with no gate, no pending review, the agent's question lost, and the task counted as in flight forever.
- Repro: scratch test `test_R2_late_blocked_after_answered_stall_strands_task` (passes): `state=BLOCKED, gate='', pending=[]`, `last_reason=late_result_after_stall`.
- Fix (either):
  - In `_blocked_review`, when the stall row is not pending, reopen the slot: set `status='pending'`, `applied=0`, clear `decision` and `decision_key`, write the new payload, and emit a new event.
  - Or put an attempt-scoped agent report into its own kind or slot. Add a regression test for both `blocked` and `context_exhausted`.

### MEDIUM
**D-3: the rotation guard is blind, and the fence skips on later ticks** (`engine.py:478-481`, `:833`, `:1283`)
- `_try_dispatch` writes a handle without `target` **before** the submit. If the submit is refused or fails, it stays that way for as many ticks as the retries take.
- `attempt_in_flight_for_agent` skips rows without a target, so it returns False. The context controller (`agent_context_budget.py:628`) may then `/clear` the pane, or write a handoff into the tree, while a send is pending.
- Also: once the handle's attempt is N+1, `_stall_fence_open` returns True at once (`:833`). From then on, only `agent_send`'s busy check guards the resend.
- Fix:
  - Put `getattr(adapter, "target", "")` into the pre-submit handle, or keep the previous handle until the submit is ok.
  - In the guard, treat `redispatch>0` with no target as holding (fail closed).

**D-4: the rotation guard ignores a live attempt waiting on a permission dialog** (`engine.py:1285-1289`)
- `WAITING_PERMISSION` puts the task into OWNER_DECISION (`:930-932`) while the attempt is still alive. The guard holds only for WORKING or redispatch, so the pane can be rotated.
- After the owner resumes, the engine keeps watching that same attempt (`:1110-1113`), which has been erased. The likely result is a 2-hour stall.
- Fix: hold for any non-terminal task whose handle targets the session and whose attempt is not proven over. That includes OWNER_DECISION with `agent_permission_prompt` and WORKING with a pending stall.

**D-5: the migration is not race-safe** (`engine.py:127-129`)
- The PRAGMA check and the ALTER are not atomic. Two processes connecting for the first time after promotion (engine worker, API, MCP) can both try the ALTER. The second gets `sqlite3.OperationalError: duplicate column name: unknown_polls` and that process's first call fails.
- Fix: run the check and ALTER under `BEGIN IMMEDIATE`, or catch the duplicate-column error.
- Promotion: back up `control_plane.db` and apply the migration once, by a single process, before restarting the services (owner-gated).

### LOW
- **D-6** `unknown_polls` is not reset on a new attempt (successful `_set` at `:492`). After a stall raised for an unknown status is answered and resent, attempt N+1 alerts after a single UNKNOWN poll. Fix: add `unknown_polls=0` to the success `_set`.
- **D-7** Stall-cancel and stall-resend holds have no SLA or second escalation (`:878-886`, `:850`). Combined with D-1 they hang silently. This is also the agency-agents HITL gap (see below). **NEEDS-DECISION:** the timeout for owner escalation.
- **D-8** The report event of `_blocked_review` (`:776-782`) is not linked through `fc_review.event_id`. If the review is decided before that event is delivered, the wake is not retired by `_fullcycle_review_closed`, and a stale wake reaches the tech lead.
- **D-9** The re-send after a busy refusal relies only on `agent_send._busy_for_send`. This is defense in depth only; it is covered by the D-3 fix.
- **D-10** The agent's `summary`, `question` and `handoff` reach the ChatGPT tech lead verbatim and without a label (`:706-711`, `:900-906`). This is a prompt-injection channel. **NEEDS-DECISION:** whether to mark these fields as untrusted agent text.
- **D-11** In strict mode, `git_write._staleness` fails open when `rev_sha` errors (`git_write.py:183-184`). The work branch is created from the ref *name*, not from the resolved SHA. Strict mode is off by default.

## agency-agents comparison (4 docs fetched from GitHub on 2026-11-01, read as untrusted reference; only material controls)
- **Multi-Agent Systems Architect**:
  - HITL says "Timeout behavior must be defined … SLA" and "Silent failure → human review". The owner gates and the stall holds have no SLA, which D-1, D-2 and D-7 show in practice. **Gap.**
  - "Idempotency requirement": met. Dispatch is keyed by `fullcycle:{task}:{attempt}`, and decision keys are latched.
  - "Never silently truncate required context": met (`clean_handoff` reports the truncation).
- **Agents Orchestrator**: the QA gate, the bounded retries (`max_attempts`) and "inconclusive → fail" (Q3) are met.
- **MCP Builder**: refusals are typed (`review_superseded`, `review_already_decided`), but nobody has checked how the MCP layer maps them to `isError`. **Gap, not re-audited.**
- **Agentic Identity & Trust**:
  - Fail-closed authorization: met for git evidence, dispatch refusals and the rotation guard's error path.
  - Not met: `submit_decision.by` is free text, and there is no signed or append-only decision trail. This is pre-existing and was rejected as out of scope. **NEEDS-DECISION.**

## Decision
**NOT CLEAN.** Fix D-1 and D-2 (HIGH) before any canary. Fix D-3, D-4 and D-5 before promotion.
Promotion stays owner-gated, and needs a DB backup plus a one-off migration step. Live unattended E2E is still NOT PROVEN. The Fable final audit is pending. The reviewer stops here and waits for tech-lead decisions.
