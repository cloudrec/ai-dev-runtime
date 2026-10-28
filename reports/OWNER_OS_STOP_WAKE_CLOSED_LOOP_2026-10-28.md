# STOP → wake → ChatGPT → next task without owner pokes — 2026-10-28

Requested by an automated instruction (ChatGPT via Owner OS); nothing here is owner
approval. Local branch only: no restart, deploy, config, secret, grant or push.

Branch `ai-runtime/stop-wake-closed-loop` (worktree `/root/ai-dev-runtime-stop-wake`),
base `cc38238` (= live `/root/ai-dev-runtime` HEAD; wake companion restarted 02:57 CET on it).

## What was observed (live DBs, read-only, last 24 h)

GAIKA is driven by ChatGPT through active grant `gba3c9487e88c` (`api:bearer/mcp-token-3`,
owner_cli, expires 2026-11-03). Stop → next ChatGPT `agent_send` latency for
`gaika-country-ru`: mostly 14–800 s, but the stops at **02:25:50, 03:26:28, 04:45:25 UTC**
waited **3597 s, 4725 s and 35+ min (ongoing)**. `owner_intervention` was recorded at 02:25
and 03:21, so the owner had to poke the chat by hand.

`agent_turn_stopped` being routine is **not** the defect. The Stop hook fires every turn
(421/day), so waking on it would be a storm. The native supervisor tried to continue on
those turns 304 times and was refused every time
(`not_delivered:automation_target_not_allowlisted`), which is correct under
`AGENT_AUTOMATION_POLICY=enforce`. After that, the quiescence rule (`agent_watch`, 300 s)
emits `work_stopped_incomplete`. That event is actionable, has trigger class `completion`,
and wakes the agent's project route. That part works: there were 41 such wakes in the day.

The losses happen downstream of that wake:

1. **A send that may have reached the chat but was never verified is dead for 3 h.** When
   the page takes the phrase, `cdp_composer` latches `wake_submitted`. That latch correctly
   forbids a second send of the event. If the user turn is then never observed
   (`user_turn_not_observed_after_send`), the companion does not acknowledge the event and
   the closed-loop watchdog does not watch it. Nothing follows up until
   `record_abandoned_wakes` retires it after `MAX_WAKE_AGE_SECS`. There were 26 such
   actionable wakes in 7 days. Example: event 73945 (gaika-ru `work_stopped_incomplete`,
   04:52:57, unverified send at 04:54:31) is the 04:45 stall.
2. **The watchdog suppressed every GAIKA watch.** `closed_loop_wake._resolution_reason`
   resolved a watch as `pane_awaiting_owner` whenever any `owner_gate` was open for the
   agent. `discovery` opens a `classify_scope` gate ("unknown-scope agent at …") for every
   agent outside the static scope map, and nobody answers those gates (45 are open). Every
   watch for such an agent was therefore resolved within about 1 s and never re-woken.
   Over 7 days that hit 19 of 34 watches, including every gaika-country-ru watch.
   `owner_status.DIAGNOSTIC_GATE_KINDS` already classifies these gate kinds as diagnostic,
   not as owner decisions.

## Change (2 files + tests)

- `core/closed_loop_wake.py`: `pane_awaiting_owner` now counts only **decision** gates
  (`kind NOT IN owner_status.DIAGNOSTIC_GATE_KINDS`). `register_delivery(verified=False)`
  is added, with a new column `wake_loop_watch.delivery_verified` (migrated, default 1).
- `tools/wake_companion.py`: when an actionable wake fails and `wb.was_submitted(event)`
  is true, the companion now registers the watch with `verified=False` and still does not
  acknowledge. `_latched` fails closed, so any doubt keeps the old behaviour.

Safety properties, unchanged or tested:

- **Idempotence.** The original event is never sent twice, because of the latch.
  `register_delivery` is `INSERT OR IGNORE`.
- **Bounded follow-up.** A re-wake is a new event (`wake_loop_no_progress`, dedup key
  `wakeloop:<id>:no_progress`), followed by at most one escalation. After that the watch
  is terminal.
- **No self-feeding.** The watchdog never watches its own re-wake events.
- **No storm.** An older watch resolves `progress_observed` as soon as any newer event
  exists for the agent, so at most one watch per agent is effectively open. Within an SLO
  window, repeated scans emit nothing.
- **No new authority.** No pane text is forwarded. The phrase still carries only the
  event id, a closed trigger class, the route key and a sanitized agent reference. The
  change touches no grant, send policy or creation policy, and gives agents no ability to
  lead themselves: only ChatGPT acts, under its existing grant.
- **Real owner decisions still suppress re-wakes**, e.g. `owner_approval_required`.

Replay over the last 7 days of live data: none of the 19 `pane_awaiting_owner` resolutions
would still be suppressed. The 26 unverified sends would each have been watched, and each
would have produced at most one re-wake and one escalation.

Known cost: re-wakes and escalations are `owner_action_required` / high severity, as they
were before. A stall that stays stalled now also queues an owner Telegram push, which
already happens for verified wakes. Eight of the 26 unverified wakes were about
`payorch-ha-fresh`, which stays hard-denied for sends. Its wakes already reach ChatGPT
today, and this change adds no actuation path to it.

## Tests

`tests/test_stop_wake_closed_loop.py` (11) runs the real `cto.emit` → `wake_bridge`
(decision, route, pending) → worktree `tools/wake_companion.tick` →
`closed_loop_wake.slo_scan` chain. Only the browser is faked. It covers:

- **Two-turn chain.** A stop wake is taken but unverified, is watched, and gets exactly one
  re-wake in gaika's own chat with `trigger=loop_watchdog`. The re-wake is verified, each
  event is submitted once, and agent progress then resolves the watch with no escalation.
- **Still stalled.** Exactly one re-wake and one escalation, then quiet.
- **Gate kinds.** A decision gate still suppresses; all four diagnostic gate kinds do not.
- **Unchanged paths.** A refused send (no latch) is not watched and is retried as before. A
  non-actionable unverified wake is not watched.
- **Helpers.** `register_delivery` idempotence; `_latched` fails closed.

Three tests in `tests/test_closed_loop_wake.py` used `classify_scope` as their example
"owner gate". They now use `owner_approval_required`, so they keep testing what they claim.

Sabotage checks:

- Removing the gate-kind filter fails 6 tests.
- Removing the companion's unverified watch fails 2 tests.

Full suite: **3996 passed / 0 failed / 4 skipped** (base `cc38238`: 3985 passed / 0 failed / 4 skipped; +11 new). `tests/test_closed_loop_wake.py` + new file: 106/106.

## Not changed (findings for follow-up)

- The native supervisor makes 300+ continuation attempts a day that the send policy
  refuses (event path 74, idle sweep 230). The refusals are harmless but repeat every
  300 s for each target. A pre-check against `agent_control.automation_decision`, with a
  terminal skip, would remove them. That touches the live supervisor loop, so it is left
  for a separate change.
- Of the companion's verification failures, `user_turn_not_observed_after_send` happened
  27 times in a day. It may be a selector timing issue after the 2026-10 ChatGPT UI change
  (`cc38238`). This change bounds the damage but does not fix the root cause.
- 45 `classify_scope` gates have been open since September. Answering or retiring them is
  an owner or ops decision.

## Live proof needs an owner-gated deploy

The chain is reproduced in tests on real modules. Proving it live needs the wake companion
running this code, which means a restart of `owner-os-wake-companion.service`. That is
owner-gated. The runtime API (`ai-runtime.service`) does not need a restart: the slo_scan
and the companion both run inside the companion process.

Suggested owner sentence:
"I approve fast-forwarding /root/ai-dev-runtime to ai-runtime/stop-wake-closed-loop <commit> and restarting only owner-os-wake-companion.service, with rollback to cc38238 on failure."

- **Rollback:** `git -C /root/ai-dev-runtime reset --keep cc38238 && systemctl restart owner-os-wake-companion.service`.
  The added column is additive and harmless if left in place.
- **Acceptance after deploy:**
  - the next actionable `user_turn_not_observed_after_send` has a `wake_loop_watch` row
    with `delivery_verified=0`;
  - after 900 s without progress there is one `wake_loop_no_progress` delivered to the
    route chat;
  - no `pane_awaiting_owner` resolutions for agents that have only `classify_scope` gates.
