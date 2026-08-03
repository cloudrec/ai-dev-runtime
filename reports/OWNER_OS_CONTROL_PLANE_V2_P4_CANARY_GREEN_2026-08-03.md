# Owner OS Control Plane V2 — P4 one-agent canary GREEN

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local, not pushed)
**Canary agent:** `cp-canary:0.0` — a **disposable, non-production** Claude agent created
solely for acceptance. Project `/root/cp-canary-v2`: doc-only, **no credentials, network,
publication, payments, trading, or destructive scope** (enforced by its `CLAUDE.md` + the
Actuator policy gate). No production/trading/email/payment/security/customer agent reused.

## Result: one-agent P4 cutover GREEN (scoped to the canary only)

| Acceptance item | Evidence |
|---|---|
| Zero-config discovery | `new_agent_discovered` event **#44** → registry `cp-canary:0.0` = **managed** (no manual registration) |
| Lease acquisition | `resource_lease agent:cp-canary:0.0` held by `continuation_watchdog`, fences **1** then **2** |
| Safe continuation delivered | `Continue with the next safe canary note` via the Actuator |
| Verified delivery | `cp_action` **×2 verified** (submitted+pane_changed+prompt_consumed+conversation_modified+state_transitioned); `action_verified` events **#46, #48** |
| Bounded safe work / no external effects | 2 dated lines in `reports/CANARY_LOG.md`, no overwrite, **only that file changed** |
| False-idle protection | estimator+guard (unit tests + live `false_idle_corrected` **#37** on polyinput); canary never actuated while working |
| CTO inbox event | `action_verified` #46/#48 in the durable event log |
| agent_notifier same-chat | commander_event **#458** (`owner_os_p4_canary_green`) **acknowledged=1** by agent_notifier (seo-backend-1) → visible message in the chat without a user prompt; also #443 earlier |
| Forced notification failure/retry | forced RED → **dead-lettered** (visible, 17 dead-letter events) → restore → `green` + sent |
| Rollback | actuator disabled → `actuate` returns `actuator_disabled` (no-op); drop-in removable |
| Duplicate prevention | 2nd pane same cwd → `duplicate_agent_detected` **#50** (`no conflicting command issued`), **0** actuations of the duplicate; `find_live_agent_for_dir` reuses the live one; duplicate retired |

## Legacy actuation retired for the canary ONLY

- Routing is scoped per-agent (`_route_via_actuator` = `ROUTE_VIA_ACTUATOR` AND target in
  `actuator.CANARY_AGENTS`). Only `cp-canary:0.0` routes through the Actuator; every other
  agent keeps the proven legacy inline path.
- Fix `e728db8`→(this): the routing branch now fully owns the record and `continue`s, so the
  canary writes **no `cw_step`** and emits **no `cw-ok`** — legacy bookkeeping/notification
  is retired for it (no dual records / double notify). Live proof: after the fix,
  `cp-canary` `cw_step` stayed at 2 (pre-fix historical) with **no new legacy rows**, while
  `cp_action` is the canary's ledger. Test: routed canary writes `cp_action`, zero `cw_step`.
- **No multi-agent cutover** — stopped here per instruction.

## Exact flags

Reversible systemd drop-in `/etc/systemd/system/ai-runtime.service.d/canary.conf`:
```
CONTROL_PLANE_ACTUATOR_ENABLED=1
CONTINUATION_VIA_ACTUATOR=1
CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0
```
`CONTROL_PLANE_ENABLED` default-on (shadow). All actuation is scoped to `cp-canary:0.0` by
the canary allowlist; any other agent → `not_canary` (never actuated). Canary
`proactive_continue` set **false** post-green to quiesce the disposable agent (no perpetual
token use); it stays managed + actuator-routed. Set true to resume.

## Event IDs / commits / tests

- Events: discovery #44; action_verified #46/#48; duplicate #50; false_idle_corrected #37;
  same-chat commander_events #443 + #458 (acked).
- Commits (local, no push): `e728db8` (per-agent routing scope), `<this>` (full legacy
  retirement for routed canary + test), config (canary session + drop-in reference), this
  report.
- Tests: `test_control_plane_p4prep.py` (+2: routing scoped, legacy-retirement no-cw_step),
  full suite green (see run).

## Rollback (immediate)

1. `rm /etc/systemd/system/ai-runtime.service.d/canary.conf && systemctl daemon-reload &&
   systemctl restart ai-runtime.service` → actuator/routing fully OFF (dormant).
2. Remove the `cp-canary` session from `config/agent_orchestrator.yaml` (+ allowed_root).
3. `tmux kill-session -t cp-canary` (disposable agent) — optional.
4. `git revert` the phase commits; `control_plane.db` gitignored. Legacy DBs backed up.

## Remaining blockers (not canary-blocking)

- **Multi-agent / full cutover** — NOT authorized; stopped here.
- **G4** — owner-push (Telegram) channel unconfigured ⇒ notifications RED, owner-action
  events dead-letter (visible). Secret-bearing → owner-gated.
- **G5** — same-chat proactive wake beyond `agent_notifier` (no supported inbound trigger).
- **G3** — push/PR/publication owner-gated (branch unpushed).

## Verdict

One-agent P4 cutover is **GREEN** on a disposable canary: zero-config discovery → lease →
verified safe continuation → CTO event → same-chat delivery, with false-idle protection,
forced-failure visibility, rollback, and duplicate prevention all proven, and legacy
actuation retired for the canary alone. No production agent touched; flags scoped to one
disposable agent; fully reversible. Stopped before multi-agent cutover.
