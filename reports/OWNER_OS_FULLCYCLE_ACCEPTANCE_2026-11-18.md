# Disposable Full Cycle acceptance `acc-20261118` (2026-11-18)

Trigger:
- The owner typed "запускай проверку на учебном проекте".
- Afterwards, an automated instruction was received (Owner OS API) asking for this report. It is not owner approval.

Scope:
- Runtime 0a0c456, live since 06:56:54Z.
- Observation was read-only: DB metadata and the journal. No chat or transcript content was read.
- No deletion, no PROD change, no new agent of mine, no push, no payment agent contacted.
- The project directory is **kept** as evidence. Cleanup waits for a separate go.

## Verdict
| Part | Result |
|---|---|
| **Project** | **PASS**. Done 07:08:54Z. Tasks `arithmetic` and `division-docs`: each attempt 1, committed, verified. Reviews 138 and 139 were acked and approved via MCP. Repo `572bf17`, `19e00f1` on `5f0fa54`, clean. 17 unittest tests pass, re-run independently as `fcagent`. |
| **One agent** | **PASS**. Only `fc-acc-20261118`; `intake.agent_alive` is False after done. The intake shows no other new project. |
| **Wake chains** | **PASS**. 0 `wake_loop_*` events since the release. No acceptance watch was re-woken or escalated (see Watches). |
| **Notification transport** | **DEFECTS**. 2 of 3 sends recorded `cdp_error` on post-send verification. One is a probable duplicate (D1). |
| `add_tasks` re-arm path | **Not exercised**. The tech lead wrote its own 2-task spec and approved both tasks. |

The project's success is proven by the engine's own records: decisions, commits, verification and the done row. That proof is independent of whether the wakes were delivered. No delivery below is inferred from a decision.

## Timeline (UTC; metadata only)
| Time | What |
|---|---|
| 06:58:07 | 88629: my stage_done (release and go/no-go). Wake skipped: `actionable_cooldown_active`. |
| 06:59:57 | Project created by an `api:` client (the tech lead). This was before any wake of mine was submitted. The metadata does not show what prompted it. |
| 07:00:50 | 88643 `task_completed` (arithmetic). Review 138 opened. Wake decided. |
| 07:01:38 / 07:01:43 / 07:02:20 | 88633 (my acceptance request): claimed, submitted, **verified** (`submitted_and_assistant_started_generating`). |
| 07:02:10 / 07:02:11 | Review 138: ack, then decide approve. |
| 07:02:40 | 88652 `task_completed` (division-docs). Review 139 opened. |
| 07:02:41 | 88643 **expired unsent**, reason `fullcycle_review_already_decided`. |
| 07:07:47 / 07:07:59 | 88652 claimed, then submitted (latched: composer cleared). |
| 07:08:28 | 88652 `cdp_error:WebSocketTimeoutException` at `post_send_verify`. |
| 07:08:32 / 07:08:34 | Review 139: ack, then decide approve. |
| 07:08:43 | 88652 delivery recorded `delivered=0 cdp_error` after a 20 s post-hoc check. The log says "submitted but unverified … latched". |
| 07:08:54 | 88672 project done. `fc_project_done` written. |
| 07:14:52 / 07:15:15 | 88629 attempt 1: claimed. `cdp_error` at `post_send_verify`, **after** dispatch but **before** the composer-cleared latch. |
| 07:15:34 | An `api:` agent_send to this pane (the automated instruction). |
| 07:15:35 | 88629 attempt 1 recorded `delivered=0`, **not latched**. |
| 07:21:30 / 07:21:45 / 07:22:00 | 88629 attempt 2: submitted (latched), `cdp_error` again, `delivered=0`. |
| 07:23:54 / 07:24:31 / 07:24:52 | 88672: claimed, submitted, **verified**, acknowledged. |

## Telemetry anomalies, explained
1. **88643: "missing receipt".** Correct behaviour, not a defect.
   - The review was decided at 07:02:11, while the tech lead was already in a turn (88633).
   - The bridge expired the wake as `fullcycle_review_already_decided` before sending. Nothing was sent, so there is nothing to confirm.
2. **88652: `cdp_error` despite a timely decision.** This is a known class.
   - The phrase was submitted: the composer cleared and the event was latched.
   - The browser then stopped answering CDP, and the 20 s post-hoc check saw no assistant start.
   - The record honestly says "submitted, delivery unproven".
   - The decision at 07:08:34 is consistent with delivery, but it is **not used as proof**. The tech lead may have polled reviews on its own.
3. **88672.** Now **verified** at 07:24:52. The 15-minute wait was ordinary per-chat cooldown and queueing behind 88629.

## Remaining defects
- **D1: a possible duplicate after a CDP timeout between the click and the latch** (`tools/cdp_composer.py`, `_attempt`).
  - `dispatched=True` is set before the click. The latch is set only once the composer is *seen* cleared.
  - If the socket dies in that gap and the 20 s post-hoc check cannot reach the page either, the event is not latched and is offered again.
  - 88629 hit exactly this gap. Attempt 1 timed out after dispatch, and an API action from the tech lead's chat followed 19 s later (circumstantial). Attempt 2 then re-sent at 07:21:30.
  - The chat was not read, so the duplicate is **probable, not proven**. The module's own docstring records the same class (event 74760, 2026-10).
  - Proposed fix (DEV, not done):
    - after `dispatched` with an unreachable page, latch as "submitted, unproven" instead of retrying;
    - rely on the closed-loop watch to re-wake if no progress follows.
  - Trade-off: a lost first send becomes a later watch re-wake instead of an immediate duplicate.
- **D2: frequent post-send CDP timeouts.**
  - 3 of 5 send attempts in the window hit `WebSocketTimeoutException` at `post_send_verify` (88652, 88629 ×2).
  - The browser was not reported degraded beforehand.
  - This makes delivery receipts unreliable even when sends land. Its cause (renderer busy while ChatGPT starts generating) is not yet measured.
- **D3: the `add_tasks` re-arm path is still unproven live.** It is covered by tests only.

Not reproduced in DEV, by design: the instruction allows a DEV investigation only if reproducible. D1 needs a CDP stub that times out between click and clear. That is a test to write with the fix, not a live experiment.

## Watches
Final state at 07:26:49Z (`wake_loop_watch`):

| Event | Re-woken | Escalated | Resolved |
|---|---|---|---|
| 88629 | 0 | 0 | `runtime_reports_agent_working`, 07:26:43 |
| 88633 | 0 | 0 | `runtime_reports_agent_working`, 07:04:41 |
| 88652 | 0 | 0 | `fullcycle_project_settled`, 07:10:59 |
| 88672 | 0 | 0 | `fullcycle_project_settled`, 07:26:43 |

There is no watch for 88643, because it was never sent.

Since the release:
- `wake_send` rows for the project's events: 2 (88652, 88672);
- `wake_loop_no_progress` / `wake_loop_stalled` events: 0;
- `loop_watch.check`: 0 problems.

## Can monitoring stop?
Yes:
- every acceptance watch is resolved;
- the agent is stopped;
- the project is done;
- no pending review or send remains for it.

D1 and D2 are transport defects for a follow-up in DEV. They need no live watch.

## Cleanup (pending a go; nothing deleted)
- The agent is already stopped by the worker.
- Evidence to keep before removal:
  - this report;
  - review ids 138 and 139;
  - events 88629, 88633, 88643, 88652, 88672;
  - the repo log above.
- Removed 2026-11-18 10:39Z on the owner's word; evidence in `reports/loop/acceptance_cleanup_2026-11-18/acc-20261118.txt`.
