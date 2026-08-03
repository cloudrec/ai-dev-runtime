# Control Plane V2 / Commander watcher — current-state reconciliation (READ-ONLY)

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local, not pushed)
**Mode:** read-only/current-state reconciliation. No push/PR/publish, no live actuation/
cutover change, no canary broadening, no credentials/secrets/payments/trading/mainnet,
no destructive action, no agent created/resumed.

## 1. HEAD + recent commits

- Branch `owner-os/control-plane-v2`, **HEAD `f30eb03`**, working tree **CLEAN**.
- Recent: `f30eb03` (email trace), `983dd3c` (CTO cursor + same-chat drain), `cd5a535`
  (counter STATE vs HISTORY), `b0dc39f` (registry/gate/lease), `6f722ba` (historical vs
  active), `f18d199`/`d1d20cb` (stability + shell-footer fix).

## 2. Tests

- **Full suite: 930 passed, 0 failed** (`python -m pytest -q`, 225s). No regressions.

## 3. `runtime.failed = 15` — reconciled (historical, monotonic)

- Every current lens over `runtime_jobs.db` yields **19** failed, not 15. `failed` is a
  TERMINAL status (never un-fails) → the total only grows monotonically, so the external
  "15" is a **stale earlier snapshot** of the same series.
- **Active (last 24h) = 0** (newest failure 2026-07-28). `runtime_job_failure_report` →
  `total=19, active=0, monotonic_terminal=true, status=green`. Not actionable.

## 4. Notifier / dead-letter — separated historical vs active

- Current STATE: `dead_letter` = **17** rows; **active (last hour) = 0**.
- HISTORY (monotonic, informational): cumulative failure attempts **85**,
  `notification_dead_letter` events **17**, `notifications_red` events **8**.
- Root: server-controlled owner-push channel disabled (RED, gate **G4**). All dead-letters
  are historical bursts (02:00–06:17); no growth since. Not active failures.

## 5. Notification delivery health

- Server owner-push posture: **RED** (owner-push channel not configured — gate G4). This is
  the honest fail-closed state, not a regression.
- Same-chat delivery via `agent_notifier` (commander_events drain): **`drain_alive=true`** —
  total 477, **unacked 0**, newest ack recent. Owner same-chat path healthy.
- CTO inbox: latest event ~#57; **0 durable consumer cursors** registered (informational —
  the ChatGPT consumer reads ad-hoc, not via the persisted `cto_brief_since(ack)` contract).
- `observability_summary` overall: **status=green, all_clear=true, active_failures_total=0,
  engine_alive=true, same_chat_drain_alive=true, stale_cto_cursors=0**.

## 6. Canary flags / allowlist

- `ai-runtime.service` (PID 1235718, active): `CONTROL_PLANE_ACTUATOR_ENABLED=1`,
  `CONTINUATION_VIA_ACTUATOR=1`, `CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0`.
- Scoped to the **single** disposable canary only; any other target → `not_canary` (never
  actuated). No broadening. Legacy path intact for all non-canary agents.

## 7. 06:16 `agent_process_failed` — HISTORICAL / RESOLVED (not actionable)

- The event is commander_event **#459** `agent_process_failed` for **`cp-canary-dup:0.0`** at
  **2026-08-03T06:15:17Z** (control-plane `agent_dead` #51 @06:14:58, `duplicate_agent_detected`
  #50 @06:14:27; service log `direct lifecycle: emitted=1 ['agent_process_failed']`).
- **Cause:** `cp-canary-dup:0.0` was the disposable **second** canary pane deliberately
  created to prove duplicate detection, then intentionally retired
  (`tmux kill-session -t cp-canary-dup`). The watcher correctly reported the pane death.
- **Status:** resolved + acknowledged (`acknowledged=1`); **not recurring** — 0 new
  `agent_process_failed` since 06:16; the `cp-canary-dup` session no longer exists.
- **No impact on real agents:** primary `cp-canary:0.0` and `arbitrage2-opus:0.0` are both
  **alive (dead=0)**; registry shows `cp-canary-dup` lifecycle=`dead` (correct), no
  duplicate live agent remains.

## 8. Defects found

**None.** All counters reconcile as historical/monotonic; the 06:16 warning is an intended
duplicate-retirement, resolved and non-recurring; delivery health is green (same-chat drain
alive); canary scope correct; full suite green. No new tests/diagnostics warranted (added
only on a found defect).

## 9. Owner-gated remainder (unchanged — no autonomous action)

- **G4** owner-push channel unconfigured (dead-letters clear only by enabling it; secret-bearing).
- **6 open owner gates** need owner decisions (surfaced as backlog by `owner_gate_report`).
- **G3** push/PR/publish; multi-agent/full cutover not authorized. Durable CTO cursor needs
  the ChatGPT side to adopt the ack contract.
