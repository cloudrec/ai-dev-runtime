# OWNER OS — AUTONOMY PHASE 3: CONTINUATION GOVERNOR (LIVE)

**Status: QUEUED — not started.** Persisted 2026-08-05 before any work, so context
compaction cannot lose it. Phase 3 begins ONLY if the Phase 2 6-hour checkpoint is clean
enough to continue; Phase 2 evidence lives in `OWNER_OS_AUTONOMY_PHASE2_CURRENT.md` and is
kept separate from this file.

## Goal

Eliminate the exact MESS stop-and-wait failure pattern: an agent that is alive but parked,
or holding a queued-but-unsubmitted prompt, while approved work sits waiting in a durable
project queue.

## Requirements (owner)

1. **Detect**, for allowlisted non-Payment agents only: live-but-idle, `waiting_input`,
   `waiting_owner`, and queued-but-unsubmitted tmux input — including the `Pasted text`
   marker, `Press up to edit queued messages`, and plainly visible text in the input line.
2. **Read a durable per-project execution queue + current pointer.** MESS source:
   `/opt/mess/design/v1/REDESIGN_EXECUTION_QUEUE.md` plus `/opt/mess/reports/PROJECT_STATE.md`.
   **Never invent work beyond the persisted queue/spec.**
3. **Act exactly once.** A visibly queued but unsubmitted approved task → submit once and
   verify pane/conversation modification. A completed stage with a grounded next queue item
   → send it once. No duplicate prompts, panes, agents, or cwd collisions.
4. **Handle `/clear` / compaction:** verify the new conversation starts in the same
   tmux/cwd, then send the exact durable resume instruction and confirm the agent rereads
   state before working.
5. **Missing design payload → genuine owner blocker** naming the exact missing fields.
   Never fabricate design, never spin.
6. **Boundaries unchanged:** Payment excluded. Arbitrage2 paper-only. No real orders,
   payments, credentials, publishing, destructive operations. **Fable prohibited.**
7. **Tests:** queued-input detection, one-copy submit, stale queue pointer,
   completion→next transition, clear/resume, duplicate prevention, missing-payload blocker,
   deliberate hold, crash-loop quarantine.
8. **Live acceptance on MESS without disrupting its current V6 work:** observe at least one
   NATURAL stage completion and prove the governor advances it or correctly blocks it, with
   no human ping. **Do not manufacture a risky failure.**
9. **Deploy only after backup + full suite.** Opus owns architecture, deploy and final
   review. Verdict `OWNER_OS_AUTONOMY_PHASE3 = PASS / PARTIAL / BLOCKED`, evidence only.

## Preconditions

- Phase 2 6h checkpoint file `reports/phase2_soak_6h_checkpoint.json` exists and is clean
  enough to continue. **Not yet met at the time of writing.**
- Phase 2 soak recorder and evaluator must remain untouched — no restart, no disturbance.

## Progress log

| when | item | status |
|---|---|---|
| 2026-08-05 | assignment persisted | done |
| — | phase 2 checkpoint gate | pending |
