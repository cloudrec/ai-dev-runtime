# Owner OS Control Plane V2 — continuation state (context-preservation)

**As of 2026-08-03.** Compact handoff so work continues across sessions. Branch
`owner-os/control-plane-v2` (local, NOT pushed). Standing constraints: read-only/internal
observability only; NO agent creation, actuation broadening beyond `cp-canary:0.0`, cutover,
push/publish, destructive, live, payment, trading, credential, secret, or external action.
Stop at any owner gate.

## Where things are

- **Phases done:** P0 SoT, P1 discovery/CTO/delivery (shadow), P2 lease-gated Actuator, P3
  notifier outbox, P4-prep routing, P4 one-agent canary GREEN (`cp-canary:0.0` only), plus
  extensive read-only observability diagnostics.
- **Live service:** `ai-runtime.service`. Shadow engine + supervisor + orchestrator +
  continuation watchdog run. Actuator armed ONLY for `cp-canary:0.0` via systemd drop-in
  `/etc/systemd/system/ai-runtime.service.d/canary.conf`
  (`CONTROL_PLANE_ACTUATOR_ENABLED=1`, `CONTINUATION_VIA_ACTUATOR=1`,
  `CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0`). `cp-canary` `proactive_continue: false`
  (quiesced). Legacy retired for the canary only; intact for all others.

## Diagnostics inventory (`core/control_plane/diagnostics.py`, all read-only)

`notification_failure_report`, `notification_history_report` (STATE vs monotonic HISTORY),
`runtime_job_failure_report` (monotonic terminal), `registry_health_report`
(engine liveness via `updated_at`), `owner_gate_report` (aging), `lease_report`,
`cto_cursor_report` (lag/stale), `commander_delivery_report` (same-chat drain),
`loop_liveness_report` (5 loops incl. supervisor heartbeat), `actuation_scope_report`
(canary-confinement breach), `consistency_report` (cursor/notification-state/fence invariants),
`restart_consistency_report` (durable in-flight state a restart could strand: orphaned/stale
notifications, abandoned in-flight actions, cursor-ahead, supervisor-heartbeat freshness),
`observability_summary` (aggregate, incl. `red_reasons` + `restart_safe`). Endpoint
`GET /api/v1/control-plane/observability`. Tests: `tests/test_control_plane_diagnostics.py`
(55). Supervisor heartbeat in `core/agent_supervisor.py::heartbeat()` → `supervisor_heartbeat`
table.

## Current live health (last check)

`observability_summary`: green when the recent owner-push dead-letter ages out; can be RED
for ~1h after a restart because discovering an observe_only agent (e.g. `payment:0.0`) emits
an owner-action event that dead-letters (owner-push disabled = **G4**). This is correct/honest,
owner-gated. Loops all alive; drain alive; actuation confined to canary; all consistency
invariants hold; no live duplicate agents.

## Same-chat pinger (owner's #1 priority)

Producer `core/control_plane/event_pipeline.py::publish_significant_event` — full owner-notify
contract (correlated CTO event id, agent, project, factual summary, delivery attempt + retry
once, receipt only on proven proactive send, dedupe, durable CTO inbox + legacy
`commander_events` mirror) for `completed`/`waiting_owner`/`failure`/`dead`/`blocker`.
False-idle invariant enforced (live shell/tool run never idle). Emit-only, no actuation.
**Stage 1 wired (code):** `core/control_plane/pinger_shadow.py::shadow_tick` in
`engine.tick_once`, scope-confined to `CONTROL_PLANE_PINGER_SHADOW_AGENTS`←
`CONTROL_PLANE_CANARY_AGENTS` (cp-canary only); transition-based, no re-emit
(durable `pinger_shadow_state`, restart-safe), best-effort. NOT deployed — daemon keeps prior
code until owner restarts; no live flag changed. Tests: `tests/test_event_pipeline.py` (13) +
`tests/test_pinger_shadow.py` (10). **Blocked on G5** (no
`CONTROL_PLANE_SAMECHAT_WAKE_URL` inbound trigger → no new turn in this chat) and **G4** (no
telegram creds) → `notifications_status`=RED. Live floor works: legacy commander drain green
(499/0 unacked) carries real payment/arb2 events. Full status + staged cutover:
`reports/SAME_CHAT_PINNER_STATUS.md`. Did NOT inject synthetic live events into owner chat.

## Open owner gates (do NOT auto-resolve — provenance invariant)

7 open: `classify_scope` for observe_only agents (email/security/ezetta/owneros/payment),
`unverified_owner_decision` (arbitrage2 stop-selling), `canary_agent_selection`. Plus:
**G3** push/PR/publish · **G4** owner-push (Telegram) channel unconfigured (secret-bearing) ·
**G5** same-chat inbound trigger · multi-agent/full cutover NOT authorized ·
**G-PAY-1/2/3** payment agent is ALIVE (task premise was false) — do not signal-9 or hand it a
publish task without owner reconfirmation.

## Candidate NEXT read-only gaps (not yet built)

- Owner-gate SLA (unanswered > N hours → escalate flag).
- Event-log growth/retention (unbounded `event` table size + rate).
- Discovery churn (lifecycle-flip rate per agent).
- Notification receipt latency for `sent` (n/a while owner-push RED).
- (done) `red_reasons` aggregation; (done) restart-consistency report.

## Recent commits (HEAD-ward)

restart consistency (`restart_consistency_report` + false-idle-after-restart) · `7176679`
`red_reasons` + context-state · `872c9e3` consistency invariants · `b579d2c` actuation scope ·
`df6711a`/`3bd853b` supervisor heartbeat · `75cc28c` loop liveness · earlier: cursor/drain,
counter STATE-vs-HISTORY, registry/gate/lease, historical-vs-active. Full suite last:
**963 passed**.
