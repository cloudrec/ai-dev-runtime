# Owner OS Control Plane V2 — cp-canary sustained stability

**Date:** 2026-08-03 · **Branch:** `owner-os/control-plane-v2` (local, not pushed)
**Scope:** the single disposable canary `cp-canary:0.0` only. No expansion to other agents.
No push/publication, credential, payment, trading, security-deployment, or destructive action.

## Window

Bounded sustained monitor: **12 samples over 18 minutes** (90s cadence) of `control_plane.db`
+ `agent_control.db`, comparing every metric to baseline.

## Verdict: STABLE — every sample DELTA = none

| Metric | Baseline | All 12 samples | Criterion |
|---|---|---|---|
| `cp_action` rows (canary) | 2 | **2** (no change) | no duplicate commands ✅ |
| `cp_action` verified | 2 | **2** | no re-issue / no runaway ✅ |
| `cw_step` rows (legacy) | 2 | **2** (no growth) | no unexpected legacy writes — retirement holds ✅ |
| max canary event id | 48 | **48** (no new) | no false-idle actuation / no churn ✅ |
| false_idle/action_blocked/duplicate events | 0 | **0** | no false-idle actuation ✅ |
| lease | `rollback_probe/3` | **unchanged** | stable lease/fence ownership (no thrash) ✅ |
| notification `dead_letter` | 17 | **17** (no new) | outbox stable, fail-closed, no runaway ✅ |

Post-monitor spot check: canary `idle`; `cw_health.errors = 0`; exactly **1** live
`cp-canary` pane (no duplicate); rollback drop-in present.

## Notes / honest observations

- **Idempotent quiescence.** `proactive_continue` is false (set post-green), so the watchdog
  runs each tick, sees the canary idle, and takes NO action — `cw_health` verified/blocked/
  errors all 0. The canary's input line holds a leftover typed `continue with the next safe
  canary note`; `decide` routes it as a submit but the Actuator returns `already_verified`
  (same conversation+text as a prior verified action) → **skipped, no re-delivery**. This is
  the idempotency guard working: a persistent pending never produces a duplicate command.
- **Lease record shows `rollback_probe/3`.** That is the expired lease from the earlier
  manual rollback probe, not active ownership; the quiesced watchdog has not re-acquired. It
  is cosmetic (an expired record) — no lease churn occurred during the window. A future real
  actuation would re-acquire at a higher fence.
- **Legacy retirement holds.** `cw_step` stayed at 2 (pre-fix historical) with zero growth;
  no new `cw-ok`/`cw-block`. The 2 legacy `agent_continuation_submitted` commander events for
  the canary are **pre-fix historical** (the dual-notify that fix `f126c2e` closed); none
  since.
- **Notifications RED (expected).** 17 dead-lettered owner-push notifications are the
  fail-closed-visible result of no configured `owner_push` channel (gate G4) — stable, not a
  regression. CTO inbox retains everything; same-chat via `agent_notifier` works.

## Flags (unchanged, scoped)

Systemd drop-in `/etc/systemd/system/ai-runtime.service.d/canary.conf`:
`CONTROL_PLANE_ACTUATOR_ENABLED=1`, `CONTINUATION_VIA_ACTUATOR=1`,
`CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0`. Any other agent → `not_canary` (never actuated).

## Event IDs

Discovery #44; `action_verified` #46/#48 (the 2 verified deliveries, unchanged); duplicate
#50; false_idle_corrected #37; same-chat commander_events #443, #458 (acked). No new canary
events during the stability window.

## Test counts

Full suite (last run): **905 passed**, 0 failed. Control-Plane-V2 + watcher suites include the
routing-scope + legacy-retirement (no-cw_step) tests.

## Rollback readiness — confirmed

1. `rm /etc/systemd/system/ai-runtime.service.d/canary.conf && systemctl daemon-reload &&
   systemctl restart ai-runtime.service` → actuator/routing OFF (dormant); canary back to
   legacy/observe.
2. Remove the `cp-canary` session from `config/agent_orchestrator.yaml` (+ allowed_root).
3. `tmux kill-session -t cp-canary` (disposable).
4. `git revert` phase commits; `control_plane.db` gitignored; legacy DBs backed up.

## Failures

**None.** No duplicate commands, no false-idle actuation, no lease/fence thrash, no
unexpected `cw_step` writes, no outbox runaway, watchdog errors 0. Stable for the full window.

## Blockers (unchanged, not stability-related)

G4 owner-push channel unconfigured (notifications RED); G5 same-chat inbound trigger;
G3 push/publication; multi-agent/full cutover not authorized.

---

# ADDENDUM — extended window (45 min) + delivered completion event + state fix

## Extended read-only window: 18 samples / 45 min — STABLE

Every sample DELTA = none vs baseline. Exact deltas over the window:

| Signal | Delta | Reading |
|---|---:|---|
| duplicate continuation (`cp_action` growth) | **0** | no duplicate command |
| false-idle (`false_idle_corrected`/`action_blocked`/`duplicate` events) | **0** | no false-idle regression |
| lease churn (`resource_lease` holder/fence change) | **0** | stable ownership (record `rollback_probe/3`, expired, unchanged) |
| notifier failures — NEW (`dead_letter` growth) | **0** | no notifier runaway / no new delivery failures |
| `cw_step` growth (legacy) | **0** | legacy retirement holds (frozen at 2 historical) |
| new canary events (`max_event`) | **0** | max id **48** unchanged |
| live `cp-canary` panes | **1** | no duplicate agent |
| `notification` total | 19 (17 dead_letter + 2 sent), unchanged | outbox quiescent |

Combined observed stable window (both runs): **~63 min, 30 samples, zero change.**

## Correlated completion event — DELIVERED via agent_notifier (receipt verified)

- Emitted `canary_stability_completed` (high significance) through the proven
  `agent_notifier` same-chat path: commander_event **#469**.
- **Delivery receipt: acknowledged=1** (agent_notifier acks only after delivering to the
  chat) within ~12s — same-chat message produced without a user prompt. No `delivery_failed`,
  no retry needed.
- Payload: monitor windows 18min+45min; duplicate_command_count **0**; false_idle_count
  **0**; lease_churn **0**; notifier_failures_new **0**; verified_actions **2**;
  cw_step_growth **0**; test_count **907**; blockers [multi-agent, G4, G5, G3].

## State-estimation fix (`d1d20cb`) — background-shell footer

Root cause of a false-idle on `owneros-direct-fix`: it is the pane where the Bash tool runs;
a Claude Code BACKGROUND shell (e.g. a live monitor) keeps the pane foreground command
`claude`, so the pane-command heuristic missed the running shell and, with no visible spinner
in the tail, it could read idle. `_STATE_ACTIVE_RUN_RE` now matches the footer "· N shell ·"
indicator → the agent reads active/working, never idle. Live-verified: owneros-direct-fix
footer `· 1 shell ·` → active match. Tests: shell footer active (`N shell`/`N shells`), plain
footer (`3 agents`) not active, estimator overrides idle base.

## Test count

Full suite: **907 passed**, 0 failed (was 905; +2 shell-footer tests).

## Event IDs (this addendum)

Stability completion commander_event **#469** (acked). Canary control-plane events unchanged
(#44 discovery, #46/#48 verified, #50 duplicate; max id 48). State-fix commit `d1d20cb`.

## Failures: none.
