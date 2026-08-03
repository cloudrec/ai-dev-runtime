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
