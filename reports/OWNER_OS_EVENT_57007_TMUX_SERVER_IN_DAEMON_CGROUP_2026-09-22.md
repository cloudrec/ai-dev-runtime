# Event 57007/57008 — total tmux-server loss at 04:20:47Z, 2026-09-22

Trigger: an automated instruction was received via the Owner OS API (not owner sign-off)
asking why the tmux server disappeared at ~04:22Z with every managed pane.

## Verdict

**External host maintenance exposed an Owner OS spawn defect.** The host did not reboot
(up since 2026-09-17 22:48 local). `unattended-upgrade` ran 06:18:29–06:22:21 local
(04:18–04:22Z); after the `libexpat1` batch, needrestart issued
`systemctl restart … ai-runtime.service …` (36 units) at **06:20:47**. The tmux server
(pid 131368) lived inside `ai-runtime.service`'s cgroup, so systemd's SIGTERM to the
control group (KillMode=control-group) ended it and every pane in the same second.

The defect is Owner OS's: it spawns sessions with `tmux new-session -d` from inside the
API daemon, and when no server exists that client forks the server into the daemon's
cgroup. Any daemon restart (deploy, crash, needrestart) is then a fleet restart.

Detection worked as designed and is not a bug: event 57007
`agent_control_plane_unreachable reason=no_server` fired at 04:21:48Z (≤60 s), critical,
owner_action_required=1; 57008 `agent_dead capacity-blockchain:0.0` at 04:22:19Z.
`tmux_control` deliberately never starts a server ("a missing server is an owner
decision"), so no auto-recovery was expected.

## Evidence

| # | Fact | Source |
|---|------|--------|
| 1 | No reboot; uptime 4 d 7 h at 06:24 local | `uptime`, `who -b` |
| 2 | `unattended-upgrade` 06:18:29→06:22:21; packages incl. libglib2.0, libxml2, libexpat1, rsyslog | `/var/log/unattended-upgrades/unattended-upgrades.log`, `/var/log/apt/history.log` |
| 3 | needrestart batch at 06:20:47: `systemctl restart 3d-print-shop.service acap-node.service … ai-runtime.service … owner-os-wake-companion.service …` | `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log` |
| 4 | All four ptys of tmux server 131368 (pts/0–3, open since Sep 17 23:33–23:42) ended **Tue Sep 22 06:20:47** | `last -F` |
| 5 | `ai-runtime.service`: old uvicorn pid 6557 "Finished server process" then "Started server process [3733164]"; `State 'final-sigterm' timed out. Killing.` at 06:22:18 | `/var/log/ai-runtime/service.log`, journal |
| 6 | Control plane: `no_server socket=/tmp/tmux-0/default listeners=0 pids=[] repair=not_attempted event=57007` at 06:21:48 | journal (`python[3727389]`) |
| 7 | Replacement server pid 3739055 (spawned via the same Owner OS path) cgroup = `0::/system.slice/ai-runtime.service` | `/proc/3739055/cgroup` |
| 8 | `KillMode=control-group`, `TimeoutStopUSec=1min 30s` | `systemctl show ai-runtime.service` |
| 9 | Only two Owner OS spawn sites, both inside the daemon: `core/agent_control.py` (`agent_resume`) and `core/session_recovery.py` (revive of a missing session) | grep |
| 10 | Live check after fix: `server_cgroup_risk()` → `server_pid=3739055 server_unit=ai-runtime.service own_unit=ai-runtime.service shared_cgroup=true` | `core/tmux_control.py` |

Unresolved detail (does not change the verdict): pane %0 of server 131368 opened at
21:33:37Z on Sep 17 with no matching `agent_resume` audit line and no journal coverage
(journal retention starts Sep 19). The server nevertheless died with the unit at
06:20:47, which only a cgroup kill explains; an ssh-started server would have sat in a
`session-N.scope` that needrestart never touches.

## Fix (local commit, no service restarted)

* `core/tmux_launch.py` (new): when this process is inside a systemd `.service`,
  `new-session` is launched through `systemd-run --scope --quiet --collect --unit
  owner-os-tmux-<pid>-<uuid>`; the client and the server it forks land in their own
  transient scope, which survives its starter and unloads itself once empty. Outside a
  service, or without `systemd-run`, the argv is untouched. Only `new-session` is wrapped.
* `core/agent_control._tmux` and `core/session_recovery._tmux` route through it.
* `core/tmux_control.server_cgroup_risk()` + `health()`: a reachable server whose unit
  equals the daemon's now reports `status=fleet_in_daemon_cgroup` with a warning. Read-only.
* Proven live: `systemd-run --scope … cat /proc/self/cgroup` from this pane →
  `/system.slice/owner-os-probe-*.scope`; a scoped `tmux list-sessions` reaches server
  3739055; no scope left behind.
* Tests: `tests/test_tmux_launch.py` (16) — cgroup parsing v1/v2/scope/ssh, prefix on/off,
  unique scope names (the first draft collided within one second — caught by the test),
  both seams, health flag on/off, no pid guessing. Neighbouring suites: 204 passed.

## What the fix does NOT do

* It does not move the **current** server 3739055 out of `ai-runtime.service`. Every
  session on it (`owner-os-opus-clean`, `hostsecure`, `main105-backup-repair`,
  `mess-ru-54582145-resumed`, `payorch-ha-fresh`, `security-demo-fresh` at 06:35 local)
  still dies on the next daemon restart — **including the restart that would deploy
  this fix.**
* It does not change the unit file (`KillMode=`), needrestart config, or the
  unattended-upgrades schedule. Those are host changes.

## Safest next action (owner decision)

1. Before ANY `ai-runtime.service` restart, re-scope the live server so the fleet survives:
   `busctl call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.systemd1.Manager StartTransientUnit 'ssa(sv)a(sa(sv))' owner-os-tmux-3739055.scope fail 1 PIDs au 1 3739055 0`
   then verify `cat /proc/3739055/cgroup` shows the scope. Reversible (the scope is abandoned
   when empty), no process is signalled.
2. Then restart the daemon to deploy the fix; new servers Owner OS starts will land in
   their own scope automatically.
3. Optional host hardening (separate decision): `$nrconf{override_rc}{qr(^ai-runtime)} = 0`
   in `/etc/needrestart/conf.d/` so unattended upgrades never restart the daemon unasked.

Nothing was pushed, no agent was created or messaged, capacity/ACAP/auction untouched.

## Addendum — event 57024 (automated instruction received; not owner sign-off)

**57024 is not a consequence of the cgroup kill.** It is a `claude_hook StopFailure`
for session `f769eed8…` (this repo's own restored pane) at 04:28:26Z with
"API Error: Connection lost mid-response", four seconds after the identical failure in
the mess session (57023, 04:28:22Z). Both panes had been re-created minutes earlier
(57021 04:24:54Z, 57022 04:28:00Z); journal 06:27:30–06:28:40 local shows no
firewall/route/DNS change — two independent sessions losing the API within 4 s is an
egress/upstream blip. Both sessions took their next instruction within 60 s
(57029/57030 `agent_turn_stopped`).

Accounting defect found and fixed (commit after 611dbc4): the StopFailure door mapped
every non-limit failure to `agent_process_failed` critical, owner_action_required=1,
without reading the message. Measured 2026-09-08..22: **63 of 130** such criticals were
transport aborts (51 "Connection lost mid-response", 8 "Server error mid-response",
4 "The response stopped arriving"); 50/51 connection-lost sessions emitted their next
hook record within 30 min (p50 60 s = the harness's own idle notification; 27 were a
normal `Stop`). Now `_TRANSPORT_ABORT_RE` (core/agent_watch.py, anchored on the
"API Error:" banner, refusal banner excluded) maps them to `agent_turn_stopped`/info,
which `native_supervisor.decide()` already reads as "continue". Fails closed. Six
regression tests; the earlier control test had used that very banner as its "real
crash" example and now uses a genuine 500.

Reviewed and left alone (expected lifecycle, not defects):
* 57018–57020 `agent_process_failed … pane vanished` and 57008–57016 `agent_dead` — correct
  for a server kill; `agent_recovered` (57021/22/25/27/31/32/38) is emitted by discovery on
  same-conversation return. `resolves` is written by three emitters and read by none: the
  event table is a log, there is no open-ledger keyed on it, so nothing is "left open".
* 57028→57031 security-demo-fresh dead/recovered flap = the automated caller's
  `agent_stop` (`switch-to-latest-conversation-57014`) + resume with another conversation.
* 57007 `no_server` with `repair=not_attempted`: by design — `tmux_control` never starts a
  server; a missing server is an owner decision.
* Still dead, untouched per instruction: capacity-blockchain, diamond-auction,
  gaika-ru-sonnet-v1 (conversation ids preserved in 57008/57009/57015).

Fix 611dbc4 re-verified: `tests/test_tmux_launch.py` 16/16; scoped `tmux list-sessions`
round-trip proven live; `tmux_control.health()` on the live server reports
`fleet_in_daemon_cgroup` for pid 3739055.

## Full suite result

`venv/bin/python -m pytest tests -q -x -p no:cacheprovider` at b377093 (working tree = HEAD):
**3388 passed, 0 failed, 1 warning in 870.99s (0:14:30)**.

## Remaining gates (privileged — not taken)

1. Re-scope live server 3739055 (`busctl … StartTransientUnit … PIDs`) — protects the 6
   live sessions before any restart.
2. `systemctl restart ai-runtime.service` — deploys 611dbc4 + the hook fix (the hook
   itself is read per invocation and is already live for new StopFailures; the daemon
   side needs the restart).
3. Push.
