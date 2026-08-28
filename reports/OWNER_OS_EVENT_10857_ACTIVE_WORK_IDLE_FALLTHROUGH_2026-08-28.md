# Event 10857 — active work fell through to idle, not just waiting_input (gaika-server, 2026-08-28)

## The regression, after ca7411a/a992b3a

Owner OS emitted `agent_waiting_input` (`idle → waiting_input`) for
`gaika-server:0.0` at `2026-08-28T20:16:57Z`. Live re-check found the same
target reporting `agent_status`/`agent_list` state `idle`, `pending=null`,
while the pane genuinely showed:

- an active foreground stability command
  (`for i in 1 2; do node --test tests/*.test.mjs …`)
- a live, bare gerund spinner: `✽ Razzle-dazzling…` — no digit, no duration, no
  `(thinking)` suffix at all

## Root cause — the prior fix only covered half the fallthrough

Reproduced live, read-only (`tmux capture-pane -p`, `agent_control.agent_list()`):
`conversation_recently_active('/opt/gaika-extension')` correctly returned `True`
at the moment of the earlier check — the general backstop from event 10801's fix
(`a992b3a`) *was* firing. But that fix only used the signal to **skip**
`_pane_pending_input()` — it never overrode the plain `idle` default when no
active-run pattern matched and pending was already empty (exactly this case:
composer line was blank). The result: a genuinely-working agent still reported
`idle`.

`idle` is itself one of `waiting_transitions.PROGRESS_STATES` — a valid "prior
state" for the edge check (`is_edge(prev_state, cur_state)` fires on any
transition FROM a progress state INTO waiting). So a later tick where any
composer text appears — real or ghost — can still produce a false
`idle → waiting_input` edge, even though the agent never actually stopped. This
is the exact shape event 10857 recorded.

## Fix — `core/agent_control.py`

`classify_state()` now takes `recently_active` as a first-class parameter,
checked immediately after the real permission-dialog check (a genuine dialog
still outranks it — it's structural, high-confidence evidence a live transcript
write does not override) and before every remaining at-rest reading:
pending-input, stale external-block text, the background-monitor check, and the
`idle` default all lose to it now, not just pending-input.

Both `agent_control.py` call sites (`agent_list()`, the single-target status
path) compute `recently_active` once and now pass it through to
`classify_state` directly, instead of only using it to gate the
`_pane_pending_input()` call.

## Tests

5 new/updated regressions, `tests/test_agent_control.py`:
- `test_recently_active_wins_over_idle_fallthrough` — identical tail, asserts
  `working` when the signal is true and (sanity check) `idle` when it's false.
- `test_recently_active_does_not_override_a_real_permission_dialog`
- `test_gaika_server_agent_list_reports_working_not_idle_for_bare_gerund_spinner`
  — the exact end-to-end shape via `agent_list()`.
- `test_recent_transcript_activity_overrides_a_pending_input_guess` (event
  10801's test) strengthened from `assert st != "waiting_input"` to
  `assert st == "working"`.

```
121/121  test_agent_control.py + test_access_recovery.py
344/344  broader gate (+ test_agent_orchestrator, test_context_budget,
         test_stall_doctor, test_agent_watch, test_agent_queued_prompt,
         test_queued_input_delivery_failure, test_queued_input_stall_incidents,
         test_wake_companion, test_closed_loop_wake)
2532/2533 full suite (1 pre-existing, unrelated failure —
         test_delivery_attribution.py, confirmed via git-stash earlier this
         session to fail identically without any of this session's changes)
```

## Commit

`d7b8bfa` — *fix(agent-control): recently_active wins over idle too, not just
pending (event 10857)* — `core/agent_control.py`, `tests/test_agent_control.py`.
Local only at time of writing; deploy/push status recorded separately once
authorized.

## Still open — identified, not fixed here

Two further ambiguities in `_pane_shell_running()`/`_IDLE_FG_COMMANDS` were
identified while investigating the "active foreground stability command" half
of the report, but were **not** touched — both are narrower-benefit,
higher-risk changes than the general `recently_active` fix above, and neither
was verified against a live, currently-running repro:

1. tmux's `pane_current_command` for a `for`/`while` loop typically reports the
   **shell itself** (`bash`), not the command running inside the loop body —
   and `bash`/`sh`/`zsh` are (correctly, for the normal case) in
   `_IDLE_FG_COMMANDS`, since an idle shell prompt is the overwhelmingly common
   resting state of most panes.
2. `"node"` is in `_IDLE_FG_COMMANDS` for Claude Code's own runtime (its
   comment: *"the Claude/node process itself"*) — but tmux's process-name-only
   reporting cannot distinguish that from a genuine user-invoked `node --test …`
   command using the same reported name.

Both would need a more reliable signal than `pane_current_command` alone (e.g.
full argv inspection) to resolve without risking false "shell_running" positives
on the ordinary idle-shell-prompt case, which is far more common than either
incident shape. Flagged for a future, separately-scoped, separately-tested fix
if this pattern recurs.

### Follow-up (later same day): deterministic reproduction, no fix implemented

Reproduced both ambiguities directly — a throwaway tmux session
(`_repro_scratch`), created and killed by this investigation, never touched any
live/owner agent:

```
$ tmux new-session -d -s _repro_scratch
$ tmux send-keys -t _repro_scratch "for i in 1 2; do node -e 'setTimeout(()=>{},3000)'; done" Enter
# pane_current_command sampled every 0.5s:
bash
bash
bash
node
node
node
```

**Finding 1 (loop-startup race) — real, but SHORT, not a sustained
misclassification.** `bash` is reported only for the ~1-1.5s the shell takes to
parse the loop and exec the first child; once `node` is actually running,
`pane_current_command` correctly reports `node`, not `bash`. This narrows the
original hypothesis: a `for`/`while` loop does not read as idle-safe for its
whole duration, only for a brief startup window per iteration boundary.

**Finding 2 (`"node"` name ambiguity) — confirmed structural, not transient.**
For as long as the loop's `node` subprocess actually runs, `pane_current_command`
reports exactly `node` — genuinely indistinguishable, by name alone, from
whatever value Claude Code's own runtime would report if it also manifests as
`node` on some install (the reason `"node"` was added to `_IDLE_FG_COMMANDS` in
the first place, per its own comment). On *this* host, `find_claude_in_pane`
already independently proves whether a Claude process is actually running in
the pane (that's the whole `is_agent`/`claude` gate `agent_list()` uses upstream
of `_pane_shell_running`), and this host's own `claude` CLI was observed
reporting its foreground command as literally `claude`, never `node` — so
`"node"` may be dead weight on this specific install. But that is one host, one
observation, and removing `"node"` from `_IDLE_FG_COMMANDS` blind, without a
counter-repro of the install shape the entry was originally added to defend
against, risks reintroducing exactly the bug it was added to prevent.

**No fix implemented.** Per the explicit "do not guess a fix without a live
deterministic repro" instruction: finding 1 does not warrant a change (the race
is real but too short-lived to be the incident's likely cause on its own, and
narrowing `_IDLE_FG_COMMANDS` for it would reduce the false-idle window by
~1s at the cost of new false-positive risk everywhere a shell briefly parses a
command). Finding 2 has a real repro for the *ambiguity* but not for the
*original bug `"node"` defends against* — without that second repro, a change
here is not safely verifiable and was not made. Both remain documented,
open, and gated on a future incident or deliberate cross-install verification.
