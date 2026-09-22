# Events 57057 and 57111 — two wake loops from the same morning, 2026-09-22

Automated instructions were received via the Owner OS API (not owner sign-off) asking
for both to be traced as Owner OS control-plane issues. Both are downstream of the
04:20:47Z tmux-server loss (see `OWNER_OS_EVENT_57007_TMUX_SERVER_IN_DAEMON_CGROUP_2026-09-22.md`)
and both are control-plane defects, fixed locally with regression tests. Capacity/ACAP,
`/opt/capacity`, `/opt` backup data/timers: untouched, read-only.

## 57057 — `wake_loop_stalled` for a pane that does not exist

Chain: 57008 `agent_dead capacity-blockchain:0.0` (high, **owner_action_required=0**,
"conversation_id preserved for recovery") → delivered as a wake at 04:23:02Z (`agent_dead`
is a LIFECYCLE type, therefore "actionable", therefore SLO-tracked) → 57042
`wake_loop_no_progress` (high, oar=1) at +15 min → 57057 `wake_loop_stalled` (critical,
oar=1) at +30 min. Identical chain on 2026-09-17 after the boot: 54490→54557→54582.

Why: the watch measures "progress" on the target pane. `_resolution_reason` had a
"target is gone" resolution only for `session:` targets and a "crash watches keep
waking" doctrine for a *crashed pane still on screen*. A pane-shaped target with no pane
at all, on a project outside `config/managed_sessions.yaml` (cp-canary,
mess-qa-automation, arbitrage2-opus — the only targets any automation revives), can
never progress: the only thing that ends the state is the owner, who was told by the
delivered 57008 itself.

Spam bound: per watch exactly two emissions (`escalated=1` closes it), so not infinite —
but guaranteed, and each one wakes the `owner-os` route with a "continue on
capacity-blockchain" task that Owner OS may not perform. Stale state: none to retire —
the `agent` row (`lifecycle_state='dead'`, conversation preserved) *is* the recovery
handle and discovery flips it to `recovered` on return; route `owner-os` is live.

Fix `b90bac1`: `_dead_pane_awaiting_owner` — positive evidence only (watched wake is
`agent_dead`/`agent_process_failed`; `agent.lifecycle_state == 'dead'`; target not in
the recovery registry) → resolution `dead_pane_awaiting_owner_resume`, silent, before
the SLO logic. Fails closed. Self-limiting via discovery's `recovered` flip. Registry
targets keep escalating unchanged (control test). Live read-only check against
`control_plane.db`: 57008/57009/57015 (still dead) resolve; 57010 (recovered) does not.

## 57111 — a finished, parked agent poked as `quiescent`

main105-backup-repair's pane (06:49–07:55 local): "Завершено и верифицировано
(последний успех 22 Sep 03:06, следующий прогон 23 Sep 02:00). Припаркован." then
"continue with the next safe step" → "Припаркован." ×3. Events 57045/57061/57111
`work_stopped_incomplete` (`quiescent`, `at_rest_unchanged_for_3xxs`), each with a new
digest because each poke+reply changed the pane bottom. Each was a LIFECYCLE wake →
route `owner-os` → poke → reply → 300 s at rest → next event. Token loop, ~hourly.

Root cause is the pane reader, three faults compounding (`core/agent_watch.py`):

1. `_UI_LINE_RE` alternative `[◯●◉]\s.*` (added for subagent-widget rows, event 4255)
   also stripped **every `●` line — the CLI's marker for the agent's own prose**.
   "● Завершено и верифицировано…" and "● Припаркован." were never evidence.
2. The composer echo wraps ("❯ continue with the next safe" / "  step"); only the `❯`
   line was chrome, so "step" survived as the agent's "final line". The event excerpt
   `step step ⧉ backup-incident` is exactly that.
3. "parked"/"припаркован" was not finish vocabulary, so even the surviving text proved
   nothing → `quiescent` (structural stop without stated finish).

Fix `adb62a3`: widget rows are `◯`/`◉`, or `●` carrying the `· ↓ NNk` counter; tool-call
headers (`● Bash(…)`) stay chrome; the indented continuation of a `❯` line goes with it
(ends at a blank or unindented line; `❯ 1. Yes` menus untouched); a stated park is a
finish, still outranked by crash text or open work below it. Live pane now classifies
`completed / stated_finish_at_rest`, which `closed_loop_wake` already resolves as
`agent_parked_completed`, and an identical repeat "Припаркован." keeps the same digest
(dedup). 11 regression tests incl. controls (quiet without a stated finish stays
`quiescent`; a `● Bash(… complete …)` header is not a finish).

## Test results

`tests/test_agent_watch*.py tests/test_owneros_hook.py tests/test_native_supervisor*.py
tests/test_closed_loop*.py`: 431 passed before the new files; `test_closed_loop_wake.py`
91 passed (86 + 5); `test_agent_watch_parked_agent.py` 11 passed. Full suite: see below.

## What is live now vs. after restart

* `hooks/owneros_hook.py` runs from the checkout per invocation — already live.
* `agent_watch` scan and `closed_loop_wake.slo_scan` run inside `ai-runtime.service`;
  both fixes take effect only after the daemon restarts. Until then the main105 loop
  continues at its ~hourly cadence and any new death outside the registry still
  produces its two escalations.

## Remaining gates (privileged — not taken)

1. Re-scope live tmux server 3739055 (`busctl … StartTransientUnit … PIDs au 1 3739055`).
2. `systemctl restart ai-runtime.service` — only after 1; deploys 611dbc4, adb62a3, b90bac1.
3. Push (local commits since a4604be: 611dbc4, 1656e17, b377093, 5dfc776, adb62a3, b90bac1, +this report).
