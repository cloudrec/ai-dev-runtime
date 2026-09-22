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

## Addendum — event 57118 and the rest of the wake-loop audit

An automated instruction was received (not owner sign-off) to finish the audit before
deployment. 57118 was `quiescent` on the Owner OS pane itself: its report ended on the
gate list with no stated finish, so the reader poked it — the 57111 shape on the self
agent. The self-wake flow (`wake_bridge.SELF_WAKE_FLOW`) is by design; the fix is the
same classifier fix plus a stated finish at the end of a report.

Two further defects found and fixed in the path:

* **`26d2286` — "incomplete" is not a finish.** `_FINISH_RE`'s `complet(?:e|ed)\b` had
  no leading boundary; the harness's abort banner "The response above may be
  incomplete." satisfied it. **48** `task_completed` events since 2026-09-01 (57114
  among them) announced an aborted turn as a completion — each a lifecycle wake. Now
  `\bcomplet…`; the banner at rest classifies `quiescent`.
* **`7bb0769` — an inventory that could not be read marks nothing dead.** `agent_list`
  states `control_unreachable` for a missing server, but `discovery.discover` read its
  zero rows as "every registered agent is gone" (nine `agent_dead` at 04:22:19Z, each
  opening an SLO watch that escalated twice), and `agent_watch`'s vanish path counted
  the blind read as a miss toward a critical `agent_process_failed` per pane. Right by
  luck on 2026-09-22; in the 2026-08-30 shape (socket deleted, server and panes alive)
  it declares a live fleet dead. Both now return untouched on an unreachable inventory;
  reachable-and-empty still marks dead (control tests). This is the origin of the
  57057 chain, one layer above the watch fix.

Reviewed and left alone: `slo_scan` bound (two emissions per watch), `register_delivery`
refusing watchdog-class deliveries, `_progress_since`, the stall-doctor and owner-gate
resolutions, `wake_bridge` dedup of `agentwatch:<target>:<cls>:<digest>`.

## Where each fix runs (corrects the earlier statement)

| Commit | Module | Runs in | Live when |
|---|---|---|---|
| 1656e17 | hooks/owneros_hook.py | per hook invocation from the checkout | **already live** |
| 611dbc4 | core/tmux_launch.py, agent_control, session_recovery, tmux_control.health | `ai-runtime.service` | after daemon restart |
| 7bb0769 (discovery half) | core/control_plane/discovery.py | `ai-runtime.service` engine | after daemon restart |
| adb62a3, 26d2286, 7bb0769 (agent_watch half) | core/agent_watch.py | `owner-os-wake-companion.service` (`tools/wake_companion.py: watch_agents`) | after companion restart |
| b90bac1 | core/closed_loop_wake.py | `owner-os-wake-companion.service` (`watch_closed_loop`) | after companion restart |

The companion's cgroup holds only `wake_companion.py` (pid 3727389, verified read-only);
the tmux server 3739055 is in `ai-runtime.service`'s cgroup. **Restarting the companion
does not touch the fleet**; restarting the daemon does, until the server is re-scoped.

## Post-restart verification plan

Companion restart (lower risk, no fleet impact):
1. `journalctl -u owner-os-wake-companion -n 20` — starts clean, `agent-watch:` lines resume.
2. Within one agent_watch pass (≤20 s): `agent_watch_state` for `main105-backup-repair:0.0`
   shows `cls='completed'` (was `quiescent`); no new `work_stopped_incomplete` for it
   unless its pane text changes with failure evidence.
3. Within one `slo_scan` (≤20 s): `wake_loop_watch` rows for 57008/57009/57015 gain
   `resolved=1, resolved_reason='dead_pane_awaiting_owner_resume'` (the `escalated=1`
   57008 row is already closed; the check matters for the next death).
4. Grep the companion log for `control_unreachable` — must be absent while the server is up.

Daemon restart (only after the tmux re-scope gate):
1. `cat /proc/3739055/cgroup` still shows the transient scope, `tmux ls` lists all
   sessions, `agent_list` shows `tmux_running=true`.
2. `GET /api/v1/agents/tmux-control/health` (or `tmux_control.health()`) → `status=ok`,
   `shared_cgroup=false`.
3. First `agent_resume` after restart: `/proc/<new tmux client pid>/cgroup` for any NEW
   server = `owner-os-tmux-<pid>-<uuid>.scope`; `systemctl list-units 'owner-os-tmux-*'`
   shows it. If a server already exists, no scope remains (collected).
4. Service log shows `control plane: discovered=N … dead=0` on the first tick (no
   spurious deaths from the restart window).

## Rollback conditions

* Any restart-window burst of `agent_dead`/`agent_process_failed` for panes that are
  visibly alive → `git revert 7bb0769` is NOT the fix (that commit prevents exactly this);
  check `agent_list` reachability first.
* A managed agent (registry: cp-canary, mess-qa-automation, arbitrage2-opus) dies and no
  `wake_loop_no_progress` follows within 15 min → `b90bac1` mis-scoped; revert it.
* A pane that genuinely stopped mid-work is classified `completed` → the culprit is
  `adb62a3`'s park vocabulary or the `●` change; revert `adb62a3` (and `26d2286`, which
  depends on the same regex) — both are pure classifier changes, no schema.
* `agent_resume` fails with `systemd-run` in the error → `611dbc4`; setting
  `PATH` without `systemd-run` or reverting the commit restores the plain `tmux` argv.
  No schema or data migration in any of the seven commits; all revert cleanly.

## Full suite results

* at a5054b3 (adb62a3 + b90bac1): **3404 passed**, 1 warning, 989.72 s.
* at 0220d4b (+26d2286, +7bb0769): **3409 passed**, 0 failed, 1 warning, 926.11 s.
  `venv/bin/python -m pytest tests -q -x -p no:cacheprovider`, working tree = HEAD.

## Remaining gates (privileged — not taken)

1. `systemctl restart owner-os-wake-companion.service` — deploys adb62a3, 26d2286,
   b90bac1, agent_watch half of 7bb0769. Lower risk: no fleet impact.
2. Re-scope live tmux server 3739055 (`busctl … StartTransientUnit … PIDs au 1 3739055`).
3. `systemctl restart ai-runtime.service` — only after 2; deploys 611dbc4 and the
   discovery half of 7bb0769.
4. Push (local commits since a4604be: 611dbc4, 1656e17, b377093, 5dfc776, adb62a3,
   b90bac1, a5054b3, 26d2286, 7bb0769, + this report).
