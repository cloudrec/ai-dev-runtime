# Event 11073 — false agent_waiting_input during active test run + live fork (gaika-server, 2026-08-29)

## The false wake

Owner OS emitted `agent_waiting_input` (`idle → waiting_input`) for
`gaika-server:0.0` while, per live evidence, the pane was actively running
the full test suite 5x after a safety-relevant tab/context lifecycle fix,
with one `general-purpose` review fork still active. Inventory already
reported `state=working` at the time.

Event 11073's own stored evidence:

```
  Ran 1 shell command
  ● Still waiting.

✻ Waiting for 2 background
  agents to finish
  ✔ Update installed · Re…
───
❯ continue waiting, check b…
───
  [CAVEMAN]
  ⏵⏵ auto mode on      · …

  ● main
  ◯ general-purpose 3m 9s ·
  ◯ general-purpose 3m 5s ·
```

## Root cause — not a code defect, a deploy gap

Tested the exact stored evidence against the code on disk **before** making
any change: `_STATE_ACTIVE_RUN_RE.search(...)` already returns `True` and
`classify_state(...)` already returns `"working"`. The wrapped
`"background\n  agents"` pattern was fixed for event 11050 (commit
`becf0b6`) and correctly covers this shape too.

So the wake fired from a process running **stale** code. Traced the call
chain: `waiting_transitions.observe()` — the source of `agent_waiting_input`
— is only called from `core/agent_orchestrator.py`, whose `run_loop()` is
started at FastAPI startup inside **`ai-runtime.service`**, a completely
separate systemd unit from `owner-os-wake-companion.service`.

Compared `ai-runtime.service`'s `ActiveEnterTimestamp` against this
session's four `agent_control.py` fix commits:

```
ai-runtime.service ActiveEnterTimestamp: 2026-08-28 16:25:31 CEST

20b363d  2026-08-28 16:24:51 CEST  dim recall-ghost fix           (BEFORE restart — covered)
a992b3a  2026-08-28 21:12:51 CEST  (thinking)/recently_active fix (AFTER — NOT covered)
d7b8bfa  2026-08-28 22:45:07 CEST  recently_active-vs-idle fix    (AFTER — NOT covered)
becf0b6  2026-08-29 00:32:00 CEST  wrapped "background agents"    (AFTER — NOT covered)
```

Every prior fix's deploy step this session restarted
`owner-os-wake-companion.service` (correct for `agent_watch.py`/
`stall_doctor.py`, the paths it actually runs), but never
`ai-runtime.service` — so `agent_orchestrator`/`waiting_transitions` kept
running the pre-`a992b3a` classifier for over 8 hours across three
already-shipped fixes, including the exact one (`becf0b6`) that resolves
this event's evidence.

## Fix

Two parts — close the immediate gap, then make the gap class self-diagnosing:

1. **Restarted `ai-runtime.service`** (required — it is the actual stale
   process). Confirmed clean via `systemctl status` and the health endpoint
   (`{"status":"ok",...}`). No gaika-server pane capture was performed at any
   point in this investigation or the restart — reproduction used only the
   event's own stored payload; the restart is a FastAPI daemon restart with
   no interaction with tmux panes.

2. **Generalized the existing worker-skew mechanism** (`core/wake_bridge.py`
   already had `register_worker()`/`worker_skew()`, built for exactly this
   bug class in the wake companion — see its own header comment). It judged
   every registered worker against a single hardcoded file set
   (`wake_bridge.py`, `wake_routes.py`), so it could not have caught this:
   `agent_orchestrator` watches different files.
   - `_WORKER_WATCHED_FILES`: now a per-worker dict — `"wake_companion"` keeps
     its original set; `"agent_orchestrator"` watches `agent_control.py`,
     `agent_orchestrator.py`, `control_plane/waiting_transitions.py`.
   - `_module_mtime(worker)` takes the worker name; `worker_skew()` looks up
     each row's own file set instead of one global mtime.
   - `agent_orchestrator.run_loop()` now calls
     `wake_bridge.register_worker("agent_orchestrator")` once per tick, so
     `pipeline_health()`'s existing `worker_running_stale_code` alarm now
     covers this pipeline, not only the companion.

## Tests

7 new regressions:
- `tests/test_agent_control.py` — `test_event_11073_active_test_run_with_live_child_is_working`,
  `test_event_11073_agent_list_end_to_end_is_working_not_pending` (end-to-end,
  with `_pane_shell_running`/`_pane_pending_input`/`conversation_recently_active`
  monkeypatched to raise-if-called).
- `tests/test_wake_pipeline_health.py` — `test_a_worker_is_judged_only_against_its_own_watched_files`,
  `test_agent_orchestrator_watched_files_include_agent_control` (per-worker
  isolation of the skew mechanism); 5 pre-existing skew tests updated for the
  new `_module_mtime(worker)` signature (monkeypatch lambdas widened to accept
  the arg — no behavioral change).
- `tests/test_agent_orchestrator.py` — `test_run_loop_registers_the_orchestrator_worker_each_tick`
  (drives `run_loop()` for one iteration via a sleep-raises-to-stop harness,
  asserts `wake_bridge.register_worker("agent_orchestrator")` fires).

```
103/103  test_agent_control.py
573/573  broader wake/orchestrator/stall-doctor gate (test_agent_control,
         test_access_recovery, test_agent_orchestrator, test_context_budget,
         test_stall_doctor, test_agent_watch, test_agent_queued_prompt,
         test_queued_input_delivery_failure, test_queued_input_stall_incidents,
         test_wake_companion, test_closed_loop_wake, test_continuation_governor,
         test_owner_os_adversarial, test_wake_bridge, test_wake_pipeline_health)
2540/2541 full suite (1 pre-existing, unrelated failure —
         test_delivery_attribution.py, confirmed via git-stash earlier this
         session to fail identically without any of this session's changes)
```

## Commit & deploy

Commit `d5c44b2` — *fix(deploy-skew): agent-orchestrator worker-skew watches
agent_control.py* — `core/wake_bridge.py`, `core/agent_orchestrator.py`,
`tests/test_wake_pipeline_health.py`, `tests/test_agent_orchestrator.py`,
`tests/test_agent_control.py`. Local at time of writing; push status recorded
separately once authorized.

`ai-runtime.service` restarted `2026-08-29T00:38:37 CEST` — required, to
close the actual root cause. Health confirmed via
`GET /api/v1/health → {"status":"ok","provider_available":true}`. No other
service touched; `owner-os-wake-companion.service` was already current.

Preserved throughout: wake target/route bindings, `waiting_transitions`
retirement semantics, managed-auto scope, Telegram/config/secrets, the 29
unrelated pre-existing dirty/untracked report files — all untouched. No
scheduling/polling created. gaika-server's live pane was never captured or
otherwise touched at any point.

## Status: closed

No remaining implementation, test, or deploy step for this incident. The
underlying "fix shipped but the wrong process restarted" failure mode is now
self-diagnosing via `pipeline_health()`'s `worker_running_stale_code` alarm
for the `agent_orchestrator` worker, not only the wake companion.
