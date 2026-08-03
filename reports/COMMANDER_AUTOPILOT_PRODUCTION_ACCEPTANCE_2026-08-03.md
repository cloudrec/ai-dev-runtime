# COMMANDER AUTOPILOT — PRODUCTION ACCEPTANCE

**2026-08-03.** Internal-autonomy Commander autopilot for the critical projects. Built strictly
inside `/root/ai-dev-runtime`. No agent created; no destructive / live-external / payment /
trading / promotion / credential / publication / scope-expansion action. Loop DORMANT (owner
gate). Local commits only.

Independent of ChatGPT: the entire mechanism is server-side and does NOT depend on any
same-chat/notification channel — internal project autonomy works even when the owner cannot be
messaged.

## PASS / FAIL

| Dimension | Result | Evidence |
|---|---|---|
| **auto-discovery** | **PASS** | persistent registry `config/commander_autopilot.yaml` — 5 projects (owneros-direct-fix, cp-canary, payment, arbitrage2-opus, mess-qa-automation), each with `end_state` + documented safe `next_step`. Live: all 5 evaluated each tick. |
| **auto-next-step** | **PASS (canary live-capable; 4 owner-gated)** | idle/waiting + unfinished + `autonomous_safe` → deliver exact next step via the lease-gated Actuator → confirm `working`, no dup (`test_deliver_to_canary_actuates_and_verifies`, `test_tick_pokes_canary_and_gates_others`). Live read-only: payment + mess detected as poke candidates; their live actuation is owner-gated. |
| **false-idle** | **PASS** | `working`/`shell_running`/background-subagent/active-exec-in-tail-tip → `skip_progressing` (never poked). Active markers checked only in the current status (tail tip), not scrollback. Actuator adds its own false-idle guard before any send. |
| **background-agent** | **PASS** | a running Fable/subagent (`has_background_subagent`) is treated as WORKING → skipped (`test_background_subagent_counts_as_working`). |
| **restart persistence** | **PASS** | delivery carries the Actuator lease + monotonic fence; a verified `cp_action` is never re-issued across a restart (`test_restart_persistence_and_dedupe_no_reissue`). Live: ai-runtime restarted, loops alive, `restart_safe=True`, `consistent=True`. `autopilot_run` ledger is durable. |
| **dedupe** | **PASS** | same (target, conversation, step) → Actuator `already_verified`; `ctrl.sends==0`, no second poke. |
| **safety boundaries** | **PASS** | payment / trade / traffic-or-DB promotion / credential / publication / push / deploy / destructive next-step → not `autonomous_safe` → `skip_unsafe`; `deliver_next_step` hard-blocks an unsafe step; non-canary target → `not_canary` (owner-gated). Deny-by-default (`test_unsafe_next_step_is_not_poked` ×6, `test_deliver_blocks_unsafe_step`). |

## Live E2E (payment / arbitrage2 / MESS), read-only

Evaluated on **current real states** (no actuation — evaluate-only; actuation for these agents is
owner-gated):

| Agent | State | Decision | Live actuation |
|---|---|---|---|
| owneros-direct-fix:0.0 | working | skip_progressing | owner-gated |
| cp-canary:0.0 | working | skip_progressing | enabled (in CANARY_AGENTS) |
| payment:0.0 | idle (3 open tasks) | **poke candidate** | owner-gated — payment execution untouched |
| arbitrage2-opus:0.0 | working | skip_progressing | owner-gated |
| mess-qa-automation:0.0 | idle (4 open tasks) | **poke candidate** | owner-gated |

The autopilot correctly **detected the unfinished idle work** on payment and mess and identified
the exact SAFE next step it would deliver, while withholding actuation (they are not in
`CANARY_AGENTS`). cp-canary and arbitrage2 were genuinely working → skipped. A real
kick→working→receipt is proven deterministically for the canary path
(`test_deliver_to_canary_actuates_and_verifies`: `acted=true, verified=true`); it was not forced
live because cp-canary was working at eval time and the other four are owner-gated.

## Owner gate (STOP POINT — not crossed)

Live auto-actuation of **payment / arbitrage2-opus / mess-qa-automation / owneros-direct-fix** is
a **scope expansion** → owner gate. To enable (owner decision required):
1. add the target to `CONTROL_PLANE_CANARY_AGENTS` (systemd drop-in),
2. set `live_actuation: true` for it in `config/commander_autopilot.yaml`,
3. set `COMMANDER_AUTOPILOT_ENABLED=1`, then restart ai-runtime.

Until then the loop is **dormant** (`COMMANDER_AUTOPILOT_ENABLED` unset — verified live:
`commander autopilot disabled (owner gate)`), and actuation is confined to cp-canary. Payment
execution and its tool-permission (ssh IdentityFile) dialogs are NOT touched.

## Watchdogs

- **agent death** → `watchdog_dead`, recorded, NEVER creates a duplicate.
- **stuck shell** → `watchdog_stuck_shell` when `shell_running` with no proven progress > 30m.
- **false completion** → `watchdog_false_completion` when `completed` is claimed with open tasks
  (a single-report claim is not accepted as the end-state).

## Limitations

- Loop dormant; multi-agent live actuation is owner-gated (above). No scope expansion performed.
- `next_step` is a static documented safe step per project (deterministic + safe); dynamic
  synthesis from the live task list is a future enhancement.
- Background-subagent detection is heuristic (regex on the tail); the primary progress signal is
  the agent_control state classification.
- The auto-allow set here is the strict `autonomous_safe` class (denies push/deploy/publish/ssh).
  The broader owner list (git commit/push to private remotes, staged builds) is intentionally NOT
  auto-allowed yet — it is a separate owner decision.

## Files

- `core/commander_autopilot.py` — registry / evaluate / tick / deliver / watchdogs / dormant loop.
- `config/commander_autopilot.yaml` — persistent registry (5 projects + end_state + safe next_step).
- `api/main.py` — dormant startup hook.
- `tests/test_commander_autopilot.py` — 22 tests.

## Status

Full suite **1045 passed, 0 failed**. Redeployed ai-runtime; loops alive, `restart_safe`,
`consistent`; autopilot dormant. Deterministic acceptance across all 7 dimensions green; live
multi-agent actuation stopped at the owner gate.
