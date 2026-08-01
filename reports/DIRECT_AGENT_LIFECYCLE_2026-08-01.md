# Owner OS — reliable tmux direct-agent lifecycle notifications (task 156)

**Date:** 2026-08-01 · **Branch:** `ai-runtime/156-owner-os-reliable-tmux-agent-com`
**Commit:** `105649a` — feat(commander): direct-agent lifecycle events for non-plan tmux agents (dead-path completion/interruption)

Runtime job 68 for this task had failed CLOSED as `fallback_plan_only` (commit
`bcb502e` contained only a Markdown plan, no implementation). This is the real
implementation, tested and live-deployed.

## Root cause of the confirmed defect

`/opt/ezetta-video` finished all six masters and updated its reports, yet no
completion event was ever created (agent_notifier healthy, `delivery_failed=0`).

The Commander already emits lifecycle events from two paths in
`agent_orchestrator.refresh_and_resolve`:

* the inline `agent_watcher.transition_event` block (lines ~720-737) — emits
  completion / owner-decision / waiting / stall / recovery for an agent on any
  observed **alive** state transition, and
* the dead-agent block (lines ~754-783) — but it only fires for orchestrator
  **plan** sessions that have an assigned unfinished task.

Both have a structural blind spot for a **direct** agent (a Claude pane not in
`config/agent_orchestrator.yaml` — only `seo-audit`/`job`/`polyinput`/`safeguard`
are configured; ezetta-video, email, security, … are direct):

1. The sweep `continue`s on any pane that is not `(is_agent and alive)` **before**
   it computes a transition, so a direct pane that dies/exits is never passed to
   `transition_event`; and the dead-block skips it (no plan task).
2. `transition_event` returns `None` when there is no prior state, so an agent
   already finished at first observation never emits.

Net: a direct agent that **finishes and exits between polls**, or is **killed**,
produces no event at all. That is the ezetta miss.

## Fix (additive, no double-notify)

New pure module **`core/direct_agent_lifecycle.py`** owns exactly that blind spot
for direct sessions, and nothing the inline path already covers:

| Observed (direct agent) | Module decision | Emits |
|---|---|---|
| ALIVE, any state | record observation only (inline owns live transitions) | — |
| ALIVE idle + fresh report | record `completion_emitted` (so a later clean exit isn't mislabelled) | — |
| first observation (baseline) | silent — an existing idle session is never retro-notified | — |
| DEAD/vanished, last seen idle, fresh report, not killed mid-run | **completion** (finish-then-exit) | `agent_completed` |
| DEAD/vanished with in-flight work / no evidence / mid-run SIGKILL markers | **interruption**, never completion; newest reports in payload | `agent_process_failed` |
| DEAD after a completion already recorded | ignored (benign exit) | — |

Guarantees:

* **No parallel notifier / history** — every event routes through the existing
  durable `agent_control.record_commander_event`, drained + acked by Owner OS via
  `GET /api/v1/agents/commander/events`.
* **Never labels a death as completion** — a fresh report cannot turn a mid-run
  SIGKILL (active-exec markers in the last tail) or a vanish-while-active into a
  completion.
* **Debounce** — a live child command (`shell_running`) or active-exec markers
  keep an idle pane out of completion.
* **Dedup across polls / monitor restart / resumed conversations** — a persisted
  observation store (`direct_agent_lifecycle` table) + per-conversation
  fingerprint dedup keys (`dlc:completed:…` / `dlc:interrupted:…`); a resumed
  Claude conversation (new `conversation_id`) re-baselines.
* **Payload** — target, cwd, event time, sanitized one-line summary, newest report
  paths, `owner_action_required`.
* **Metrics** — `agents_observed`, `completion_candidate`, `completions_emitted`,
  `dead_candidate`, `interruptions_emitted`, `false_idle_debounced`,
  `insufficient_evidence_suppressed`, `duplicate_suppressed`, `delivered`,
  `emit_error`, … exposed read-only at
  `GET /api/v1/agents/direct-lifecycle/metrics`.

Wired additively into `agent_orchestrator.run_loop` (best-effort — a sweep failure
is caught and never breaks the orchestrator).

## Modified source (commit `105649a`, 4 files, +764)

| File | Change |
|---|---|
| `core/direct_agent_lifecycle.py` | **new** — pure decision (`decide`, `completion_evidence`, `_fresh_report`), persistent obs store + metrics (`get_obs`/`save_obs`/`bump_metric`/`metrics`), `sweep` wiring |
| `core/agent_orchestrator.py` | `run_loop` calls `direct_agent_lifecycle.sweep(agent_list())` each tick (guarded, best-effort) |
| `api/v1.py` | new read-only `GET /agents/direct-lifecycle/metrics` |
| `tests/test_direct_agent_lifecycle.py` | **new** — 25 tests |

## Tests

* Focused: `tests/test_direct_agent_lifecycle.py` — **25 passed**. Covers evidence
  gating, baseline silence, alive-recorded-not-emitted, finish-then-exit
  completion, working→dead & vanished interruption, SIGKILL/mid-run stays
  interruption even with a report, dead-after-completion ignored, resumed
  conversation, missing cwd, delivery failure counted-not-raised, sweep
  skip-configured, vanished dedup across polls, metrics.
* Relevant suites: `test_agent_watcher` + `test_agent_orchestrator` + `test_phase13`
  + lifecycle = **100 passed** (inline path unchanged).
* Full suite (`python -m pytest -q`): **798 passed** (773 prior + 25 new).

## Deployment evidence (live)

* Deployed by restarting **`ai-runtime.service`** only (the systemd daemon that
  runs the orchestrator loop). No other Owner OS service touched.
* Post-restart `MainPID 3535252`, `active`; `GET /health` → `{"status":"ok"}`;
  log: `agent supervisor started` + `agent orchestrator started (interval 45s)`.
* First live sweep on the new code (read-only DB inspection):
  * 4 direct agents **baselined silently**: `email:0.0` (idle), `security:0.0`
    (idle), `ezetta-video:0.0` (working), `owneros-direct-fix:0.0` (working).
  * **0** `dlc:*` commander events emitted → **no false notifications** for
    existing alive agents.
  * **ezetta-video recorded as `working` (a fresh session) — NOT retro-notified.**
  * metrics: `agents_observed=5, false_idle_debounced=2, noop=2` (all silent).
  * **delivery failures = 0** (all-time); **0** commander events created since
    restart.
  * new metrics route registered: `/api/v1/agents/direct-lifecycle/metrics` → 401
    (auth required), vs 404 for a nonexistent route.
* Controlled **synthetic** lifecycle transition with a **non-external captured
  sink** + isolated temp DB (a fake `synthsink:0.0` target — no real tmux agent
  or user session touched): alive→silent; pane vanish with fresh report while
  active → single `agent_process_failed` (interruption, `owner_action_required`,
  never completion); repeated poll → deduped (no second event).

## Rollback

* Code: `git revert 105649a` (or `git checkout <prev> -- core/direct_agent_lifecycle.py core/agent_orchestrator.py api/v1.py tests/test_direct_agent_lifecycle.py`) then `systemctl restart ai-runtime.service`.
* Kill switch without a redeploy: set `DIRECT_AGENT_LIFECYCLE_ENABLED=0` in
  `/root/ai-dev-runtime/configs/.env` and restart — `sweep` no-ops, the inline
  path is unchanged.
* The `direct_agent_lifecycle` / `direct_agent_lifecycle_metrics` tables are
  additive and can be dropped safely; no existing table/behaviour was altered.

## Known limitations

* A direct agent that finishes and **exits while still showing active-exec markers
  in its last captured tail** (e.g. it exited faster than one 45s poll after the
  spinner) is classified as an **interruption**, not a completion — the safe,
  criteria-compliant reading (death is never a completion). The newest reports are
  carried in the interruption payload so the owner can verify.
* A vanish **while last seen active** (working) is an interruption even with a
  fresh report (ambiguous between finish and kill) — again the conservative
  choice, with reports in the payload.
* Completion evidence relies on `agent_report` scanning the project's allowlisted
  report subdirectories; a project that writes its status artifact outside those
  subdirs will not produce completion evidence (falls back to interruption on
  exit, still notified).
* No historical backfill — existing sessions are baselined silently; only
  transitions observed after deploy are notified.
