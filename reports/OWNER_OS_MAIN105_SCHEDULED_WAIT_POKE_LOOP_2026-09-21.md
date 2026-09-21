# main105-backup-repair:0.0 — 45 `work_stopped_incomplete` in a day on a deliberate wait

**Provenance:** an automated instruction was received (Owner OS API channel). Nothing here
is owner sign-off. The backup agent, its shell and its pane were not touched; every read
was read-only (`tmux capture-pane`, `mode=ro` SQLite).

## What the pane shows

`✻ Brewed for 2s · done 10:53 PM · 1 shell still running`, footer `⏵⏵ auto mode on · 1
shell`, and the agent's own last words: `scheduled_wait до 22 Sep 02:00.` — in reply to a
`❯ continue with the next safe step` line that it did not type. One live background shell
(the watcher), foreground turn finished, inventory state `idle`.

## The loop (events 56782, 56785, 56791, 56820, 56842, 56853 and 39 more)

| actor | count, 2026-09-20T22Z → 21T21Z | what it did |
|---|---|---|
| native_supervisor **event path** | 127 skips `intentional_external_wait` | read `background_tasks` from each turn-stop hook payload, correctly refused to poke |
| native_supervisor **idle sweep** | **117 `continued_same_agent_idle_sweep`** + 622 `cap_reached_but_recent_intentional_external_wait` | poked "continue with the next safe step" whenever below the hourly cap |
| agent_watch | **45 `work_stopped_incomplete`** (class `quiescent`, `at_rest_unchanged_for_~300s`) | one per new pane digest |

Mechanism: the idle sweep consulted the intentional-wait evidence **only once the cap
(`MAX_CONSECUTIVE=6`/h) was reached**, and then declined to gate because the wait was
recognised — so it never gated *and* kept poking below the cap, ~5×/hour. Every poke put a
new `❯ continue…` + `● scheduled_wait…` pair on screen; agent_watch's digest moved; its
"unchanged pane stays armed" rule (2026-08-30, diamond-auction) correctly re-armed because
the screen *had* changed; 300 s later the pane was quiescent again on a fresh digest →
another `work_stopped_incomplete`. `agent_watch_state.emissions` for this target: **368**.

agent_watch is not the defect. A finished foreground turn with a leftover background
shell must read at rest — that decision (capacity-blockchain, 2026-08-30, `_BACKGROUND_SHELL_RE`
removed from the active markers) is what lets a real silent stop be noticed, and it is
kept. What was wrong is the *other* door poking a pane the first door had just called
"waiting by design".

## Fix (local commit)

`core/native_supervisor.py`, idle sweep: after the quiet check and before the cap, call
`gate_exemption(conn, target, cwd, now)`; on `intentional_external_wait` /
`recent_intentional_external_wait` record `skip / idle_sweep_<exemption>` and do not send.
Same evidence the cap branch already trusted, consulted before the send instead of after.

Fail-closed for real stalls: `recent_intentional_external_wait` is a `native_supervision`
row younger than `GATE_WAIT_LOOKBACK_SECS` (3600 s), written only by a turn boundary whose
hook payload still shows something armed. A shell that dies renews nothing; the sweep
resumes after the lookback. Declared waits (`mark_external_wait`) were already honoured.

Full suite: 3363 passed. Tests (`tests/test_native_supervisor.py`):
- `test_the_idle_sweep_honours_the_event_paths_intentional_wait_below_the_cap` — fails on
  the pre-fix tree (1 send), passes now (0 sends, skip reason recorded in
  `native_supervision_idle`).
- `test_the_idle_sweep_resumes_once_the_wait_evidence_has_expired` — stale evidence → the
  sweep continues as before.

## Expected effect after the companion restart (owner gate, not taken)

For `main105-backup-repair:0.0`: `native_supervision_idle` rows read
`skip / idle_sweep_recent_intentional_external_wait`, no further
`continued_same_agent_idle_sweep`; pane digest stops moving; agent_watch keeps its one
armed `quiescent` notification and emits no new `work_stopped_incomplete` until the
screen genuinely changes (the 02:35 verification, or the shell dying). Until the restart,
the loop continues at the pre-fix rate.

## Not touched
The backup process, its shell, the pane, `configs/.env`, services, routes, credentials.
