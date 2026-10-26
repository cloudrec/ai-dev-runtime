# Automatic agent typing — pathways and fail-closed policy (2026-10-27)

Branch `ai-runtime/agent-automation-allowlist` (from live `ca5a1a1`). Not deployed.
Requested by an automated instruction; live settings unchanged.

## Inventory (live code `ca5a1a1`, targeted search)

| Pathway | How it types | Today's target control |
|---|---|---|
| native supervisor (`core/native_supervisor.py`) | `ac.agent_send` | `NATIVE_SUPERVISOR_TARGETS=*` (live .env) minus deny lists; deny targets do NOT name payorch-ha-fresh (projects list has `payorch`) |
| continuation watchdog | `ac.agent_send` + **raw `send-keys Enter` / `C-u`** | `CONTINUATION_WATCHDOG_SESSIONS` (owner-os-opus-windows, gaika-server) |
| session recovery | **raw `send-keys <digit> Enter`** (summary menu) | registry of recoverable sessions |
| orchestrator, context budget (`/clear`), phase advance, agent fabric | `ac.agent_send` | their own registries |
| native goals (`/goal`), CTO bridge | `ac.agent_send` (actor set) | explicit config / `NATIVE_OWNER_OS_AGENTS`; payorch denied in code |
| API / MCP (ChatGPT) | `agent_send` / `agent_answer` with `api:` actor | HMAC auth only |
| agent creation (`agent_resume`, `tmux_launch`) | launches panes | not covered here (separate decision) |
| wake companion / CDP | types into the ChatGPT browser, not into agents | n/a |
| `core/direct_pane_control.py` | raw send-keys | library used only by tests; not imported by live code |

Defects: no choke-point hard deny for the payment agent; no allowlist for automatic
callers (`*` reaches every agent); two modules bypassed `agent_control` with raw keystrokes.

## Change

- `agent_control._deliver` (all `agent_send` / `agent_answer`) checks the RESOLVED pane:
  `payorch-ha-fresh` refused for every caller incl. API (case/space-insensitive; reads
  unaffected); in-process callers (actor not `api:`) only into `AGENT_AUTOMATION_TARGETS`
  (comma sessions/targets; empty = none; `*` is never a wildcard). `AGENT_AUTOMATION_POLICY=off`
  = previous behaviour (hard deny still on). Refusals are audited, not recorded as deliveries.
- `agent_control.send_control_keys` = the only bare-key path (Enter, C-u, C-m, Escape, one
  digit), same policy; watchdog Enter/C-u and session-recovery menu choice now use it.
- Invariant test: no live module except agent_control types into panes (`send-keys` /
  `paste-buffer`); `direct_pane_control` stays unimported by live code.

Tests: `tests/test_agent_automation_policy.py` 38; transport suites opt out explicitly
(`AGENT_AUTOMATION_POLICY=off` fixture). Full suite: 3651 passed, 9 failed = the DST park
tests already fixed by PR #22 (independent file).

## Owner gate (on deploy, behaviour changes)

Deploying this with no `AGENT_AUTOMATION_TARGETS` stops ALL automatic typing into agents
(fail closed); API/MCP sends keep working, payorch-ha-fresh refused for everyone. To keep any
automation, list exactly the sessions it may touch, e.g. in an ai-runtime drop-in:
`Environment=AGENT_AUTOMATION_TARGETS=owner-os-opus-windows,gaika-server,cp-canary`
(the owner decides the list; the hook-spawned `native_supervise_once.py` reads the runtime
`.env`, so the same key belongs there too). Rollback: `AGENT_AUTOMATION_POLICY=off` or
revert the commit.
