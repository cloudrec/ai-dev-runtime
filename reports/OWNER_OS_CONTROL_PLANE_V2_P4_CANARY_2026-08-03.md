# Owner OS Control Plane V2 — P4 canary (single agent, G1 approved)

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local, not pushed)

## Outcome summary

| Acceptance item | Result |
|---|---|
| State-estimation false-idle defect fixed (added to scope) | **PASS** — fixed + tested + live-verified |
| Per-agent canary scoping (single-agent safety) | **PASS** — `CONTROL_PLANE_CANARY_AGENTS`, deny-by-default |
| Same-chat delivery via agent_notifier (visible message, no user prompt) | **PASS** — canary commander_event #443 drained + acked by agent_notifier |
| Forced notification failure → visible retry/error → restore | **PASS** — 5 visible fails → dead-letter → restore green |
| Agent becomes idle → lease → verified continuation → CTO event | **GATED** — no confidently-idle, non-excluded agent available now |

Legacy actuation **remains enabled** (untouched). Actuator flags in the running service
**remain OFF**. No agent was commanded.

## State-estimation fix (`671b41b`) — the canary-scope defect

`agent_status` reported `arbitrage2-opus` idle while the pane showed `Pouncing… (8s ·
thinking)`. Fixed:
- strengthened `_STATE_ACTIVE_RUN_RE` (spinner timer `(8s ·`, `· thinking`, token counters,
  `esc to interrupt`) — whimsical gerunds (Pouncing/Noodling/Beboppin/Hyperspacing/
  Osmosing/Shimmying) caught even when the word is unknown;
- `state_estimator.estimate` fuses active markers + conversation-mtime movement + process
  CPU + idle-dwell; conflicting/unsettled → `unknown`, never idle;
- actuator false-idle GUARD refuses to deliver to a working/active agent (`target_working`)
  and emits `false_idle_corrected`.
- **Live evidence:** arbitrage2-opus base+estimate=working (CPU+conv+spinner); reported
  Pouncing string now classifies working; `false_idle_corrected` event #37. On **polyinput**
  the guard fired live — base=idle but estimate=working via CPU (`cpu_active=True`), i.e. a
  real false-idle was caught and a continuation would be suppressed.

## Canary agent selection — GATED (evidence)

The verified-continuation step needs a **confidently-idle, non-excluded** agent. None exists
right now:

| Agent | Disposition |
|---|---|
| arbitrage2-opus | **trading** (excluded) + currently working |
| email | **email sending** (excluded) |
| security | **security** (excluded) |
| ezetta-video | SSH actuator broker / sealed-core / secrets guards = **credentials/security** (excluded) |
| owneros-direct-fix | cwd `/root/ai-dev-runtime` = **self** (would collide with this work) |
| polyinput | **not confidently idle** (false-idle caught live via CPU) + owner-**parked** ("never advance") + near context-limit (937k) |

Recorded as `owner_gate` `6521774525664e49` (kind `canary_agent_selection`) + CTO event #41
(`canary_continuation_gated`). This is the safety system working: the false-idle guard +
exclusions **prevented an unsafe actuation** rather than forcing one.

**To green the continuation later:** point the canary at a prepared, confidently-idle,
non-excluded agent (e.g. a fresh throwaway Claude pane in a scratch dir), then set the flags
below scoped to it and watch for `action_verified`.

## Same-chat delivery — PASS

`agent_notifier` (`/opt/seo/backend/services/agent_notifier.py`, container `seo-backend-1`,
healthy) drains ai-runtime `commander_events` and delivers to this ChatGPT conversation
(the same path that delivered the 04:17 SIGTERM message). The canary commander_event
`#443` (`owner_os_canary_p4`) was recorded and **acknowledged=1** within one poll — agent_
notifier acks only after delivering, so the same-chat message was produced without a user
prompt. Same-chat proactive delivery is therefore **supported and proven** via agent_notifier
(no new mechanism claimed).

## Forced notification failure — PASS

Live on the control-plane notifier: with no proactive channel, `canary-forced-fail` was
attempted 5× (each a visible `failed`), then **dead-lettered** (critical event, never
silent); restoring a channel → status `green`, next drain `sent=1`. Retry + visible error +
recovery all proven.

## Exact flags / agent / event IDs

- Flags (all remain in their stated posture): `CONTROL_PLANE_ENABLED=1` (shadow),
  `CONTROL_PLANE_ACTUATOR_ENABLED` **unset/OFF** in service, `CONTINUATION_VIA_ACTUATOR`
  **unset/OFF**, `CONTROL_PLANE_CANARY_AGENTS` **unset** (deny-by-default).
- Canary agent: **none commanded** (selection gated).
- Event IDs: false_idle_corrected #37; canary_continuation_gated #41; commander_event #443
  (same-chat, acked); dead-letter batch (forced-failure).
- Gate: `6521774525664e49` (`canary_agent_selection`).

## Tests

- New/changed: state-estimator (15), per-agent canary scoping (1), + existing actuator/
  p4prep updated for the allowlist. **Full suite: 894 passed** (state fix) → all green after
  scoping (29 focused pass).

## Rollback

- No live actuation performed; nothing to roll back on the panes.
- Flags stay unset → actuator dormant; `CONTROL_PLANE_ENABLED=0` stops the shadow loop.
- `git revert` the phase commits; `control_plane.db` gitignored/droppable; legacy DBs backed
  up + checksummed.

## Whether legacy actuation remains enabled

**Yes** — legacy continuation watchdog + orchestrator actuation remain fully enabled and
untouched. The new Actuator did not command any agent (flags OFF; selection gated). No
cutover performed.

## Simulated canary (offline, deterministic) — PASS

To close the gate as far as possible without a real agent, a deterministic in-process
harness proves the FULL path with a fake pane and NO live flag change:

- `core/control_plane/canary_sim.py` — `SimulatedPane` (models idle → deliver → consume →
  working, conversation-mtime advance) + `run_canary()` which ARMS the actuator
  (`ENABLED` + single-agent allowlist) only for the duration of one in-process run and
  RESTORES the globals afterwards. The deployed service (separate process) is unaffected;
  `actuator.ENABLED` is verified `False` before and after.
- `tests/test_control_plane_canary_sim.py` — **8 tests**:
  - **full path** lease → deliver → consume → verify (all five proofs) → `action_verified`
    CTO event, `cp_action` ledger `verified`, lease held, agent SoT `working`, exactly one
    delivery;
  - **false-idle** pane (active marker) → suppressed (`target_working`, `false_idle_corrected`),
    zero delivery;
  - **exclusion** — an agent not on the canary allowlist → `not_canary`, zero delivery;
  - **restart** — stale fence rejected, re-leased fence proceeds, exactly one delivery;
  - **dedup** — a verified action is not re-issued (`already_verified`, zero delivery);
  - **retry-once** — first attempt does not consume → robust retry → verified;
  - flags OFF before/after the harness.

This is **SIMULATED PASS** — the pipeline is proven end-to-end deterministically. It is
explicitly NOT the real-agent proof.

## Simulated PASS vs still-gated real-agent proof

| Path | Status | Evidence |
|---|---|---|
| lease → deliver → consume → verify → CTO event | **SIMULATED PASS** | `test_control_plane_canary_sim.py` (8) |
| false-idle / exclusion / restart / dedup negatives | **SIMULATED PASS** | same |
| same-chat delivery via agent_notifier | **REAL PASS** | commander_event #443 acked |
| forced notification failure → retry → restore | **REAL PASS** | live notifier drain |
| state-estimation false-idle fix | **REAL PASS** | live on arbitrage2 + polyinput |
| verified continuation on a REAL live agent | **STILL GATED** | gate `6521774525664e49` — no confidently-idle non-excluded agent |

## Stop point

Per instruction: stop before multi-agent/full cutover. The full actuation pipeline is now
proven **offline (simulated PASS)**; the remaining **real-agent** continuation stays gated on
a safe agent-selection decision (`6521774525664e49`). Every other acceptance item is proven.
All actuator/canary flags remain OFF; legacy actuation untouched. Infrastructure committed,
tested, reversible.
