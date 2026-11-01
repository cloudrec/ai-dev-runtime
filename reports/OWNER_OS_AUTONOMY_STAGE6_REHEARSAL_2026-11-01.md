# Owner OS autonomy — Stage 6: isolated E2E rehearsal + live preflight (2026-11-01)

An automated instruction was received via the Owner OS API (tech lead). It is not owner approval.
**No live autonomous E2E is claimed.**

## 1. Verified state (no suite re-run)
- Branch `owneros/autonomy-finish`, HEAD `b968696363de…` before this report. Tree clean.
- Stage 3 focused evidence: 140 PASSED, in `reports/stage3/evidence_focused.txt`. The file records no SHA, command or exit code; this gap was already flagged.
- Stage 4 full suite at `5cfd7ab`: 4253 passed, 4 skipped, 0 failed, `exit=0`.
- Stage 5 full suite at `bf38344`: **4309 passed, 4 skipped, 0 failed, `exit=0`**, in `reports/stage5/full_suite_bf38344.txt`.

**agency-agents gap analysis: done.** All 4 source files were read; the mapping is in `OWNER_OS_AUTONOMY_STAGE3_4_HANDOFF_2026-11-01.md` §4.

Implemented because of it:
- G1 ("inconclusive evidence → FAIL", "never trust self-reported") — `eb3b30d`.
- The HITL-SLA gap was partly closed by D1's stall alert (`9fb024a`).

Already met before (no change needed): QA gate per task, ≤3 attempts, idempotent dispatch, checkpoint before send.

Proposed or rejected, not implemented:
- Hash-linked, signed audit trail — rejected, no crypto overhaul.
- SLA for owner gates — open.
- Silent truncation of feedback — low, open.
- MCP `isError` re-audit — not done.

## 2. Rehearsal: event → supervisor → ChatGPT review/decision → agent continuation → completion
- Command: `python3 -m pytest -v -p no:cacheprovider tests/test_autonomy_cycle_e2e.py` at code `bf38344`.
- Result: **7 passed, exit 0** (16 s). Evidence: `reports/stage6/rehearsal_autonomy_cycle_b968696.txt`.

Isolation, verified in code:
- `tests/conftest.py:32,36` and the test fixture point the control-plane and agent-control DBs at temp files.
- tmux runs on a private socket.
- `127.0.0.1:9222` is walled for HTTP and websocket (conftest `_LIVE_CDP_PORT`, `test_autonomy_cycle_e2e.py:75-84`).
- `TELEGRAM_*` are unset (`:161-162`).
- Nothing touched the live tree, the live DB, services or the browser.

| step | real code | mocked |
|---|---|---|
| spec → task → dispatch → agent pane | engine, task contracts, private tmux | **agent** = fixture script |
| checks, git commit | real, in a temp repo | — |
| review event → wake decision → route | `cto`/engine events, `wake_bridge`, `wake_routes` (temp DB) | — |
| supervisor delivery | `wake_companion.tick`, `cdp_composer.submit_phrase` (claim, latch, delivery proof, ledger) | **ChatGPT page** = fake CDP session |
| ChatGPT reads the wake, decides | — | **reviewer** = stub that calls `ack_review`/`submit_decision` only on verified-delivered wakes |
| decision → continuation → next task → done → watchdog quiet | engine | — |

**Not covered by this rehearsal:**
- The D1 (stall) and D2 (context-exhausted) review paths go through the same `_request_review` → `REVIEW_EVENT` path. Their tests (`test_fullcycle_stall.py`, `test_fullcycle_context.py`) drive the reviewer by hand, not through `wake_bridge` and the companion.
- A real Claude pane.
- Real ChatGPT.
- Telegram.

## 3. Real ChatGPT wake — read-only preflight. Stopped here at an owner gate.
Probes run, all read-only (`git rev-parse`/`status`/`merge-base`, `systemctl list-units`, `ss -ltn`). No connection was made to CDP or any service.

| probe | result |
|---|---|
| live tree `/root/ai-dev-runtime` HEAD | `9557004`; working tree **dirty** (38 status entries) |
| live contains branch code (`bf38344`) | **no** |
| `owner-os-wake-companion`, `owner-os-chromium`, `owner-os-fullcycle-worker`, `ai-runtime` | active (running) |
| CDP 127.0.0.1:9222 | listening (1 socket) — not contacted |
| systemd | one unit shows `loaded failed` (not identified; outside this task's scope) |

**Exact owner gate.** A real-ChatGPT E2E of Stage 3–5 behaviour needs Stage 3–5 code running in the live companion, engine and control-plane DB. Running branch code against the live companion, browser or DB from this worktree would be a live side effect, so it was not done. Required, in order:
1. Independent review: the 7-question packet in `OWNER_OS_AUTONOMY_STAGE3_5_HANDOFF_2026-11-01.md`.
2. **Owner-approved promotion:**
   - an installer;
   - a backup of the live control-plane DB (the additive `fc_task.unknown_polls` column);
   - a rollback plan;
   - companion, runtime and fullcycle-worker restarts;
   - a read-only check of live Full Cycle roots for G1b;
   - a decision on the dirty live tree.
3. **Owner-approved live canary:**
   - a sandbox Full Cycle project with a git root;
   - its own ChatGPT route binding (a route-binding change, so owner-gated);
   - one task;
   - observe event → companion delivery verified → ChatGPT `ack_review`/`submit_decision` → next task → `project_completed`, with no human prompt.

Pre-existing live evidence (on live code `9557004`, stage_done path only): events 78155, 78167, 78237, 78336, 78347, 78391 and 78416 each received a `wake` decision on route owner-os. This proves the wake **decision** leg only, not a review → continuation loop.

## 4. Next safe action
Hand the 7-question review packet to a separately approved reviewer. Nothing else can proceed without owner gates.
