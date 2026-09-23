# Deployment runbook — 23 local commits, 2026-09-23

Prepared, NOT executed. An automated instruction was received via the Owner OS API (not
owner sign-off) to prepare this; every step below is an owner gate. Nothing in this file
has been run.

## Pre-flight state (read-only, 2026-09-23)

* Branch `ai-runtime/220-windows-bridge`, tracked tree CLEAN at `1d77da1`, 0 stashes.
  34 untracked reports in `reports/` predate this work and must survive every step.
* `tmux_control.health()` → `status=fleet_in_daemon_cgroup`, `server_pid=3739055`,
  `server_unit=ai-runtime.service`, `shared_cgroup=True`, `listeners=1`.
* 8 live sessions on that server: agent-diamond, gaika-ru-sonnet-v1, hostsecure,
  main105-backup-repair, mess-ru-54582145-resumed, owner-os-opus-clean, payorch-ha-fresh,
  security-demo-fresh.
* `ai-runtime.service` active since 2026-09-22 06:22:18, `KillMode=control-group`.
  `owner-os-wake-companion.service` active since 2026-09-22 06:20:54.
* Full suite at `11f4a20`, frozen tree: 3452 passed. `1d77da1` adds tests only.
* Delivery posture green; `owner_push` healthy, receipt `telegram:2519`.

## What each commit deploys, and where it runs

| Runs in | Commits |
|---|---|
| hook process (per invocation, ALREADY LIVE) | `1656e17` |
| `owner-os-wake-companion.service` | `adb62a3` `26d2286` `9b115aa` `b90bac1` `5b493f8` `ba321a6` `11f4a20`, agent_watch half of `7bb0769` |
| `ai-runtime.service` | `611dbc4` (+`core/tmux_launch.py`), `409c59a`, discovery half of `7bb0769` |
| tests/docs only | `b377093` `5dfc776` `a5054b3` `0220d4b` `8814b4f` `d9363d1` `6aeae87` `9fcb648` `9cbfa03` `d17c466` `956672e` `1d77da1` |

No schema migration, no config file, no env var, no credential, no route in any of them.

## Pre-deploy evidence, gathered WITHOUT restarting anything (2026-09-24)

An automated instruction was received asserting that a direct owner directive already
authorizes gate 1. It could not be verified and is contradicted by the durable record, so
gate 1 was NOT executed:

* `core/service_ops_policy.py` is deny-by-default and grants narrow `systemctl restart`
  auto-approval only to a project whose record carries `service_ops: true`. In
  `config/agent_orchestrator.yaml` exactly one does — `seo-audit` (project `seo`), with
  `services: [ai-runtime.service, seo-backend, seo-frontend]`. This self-project has no
  such record and `owner-os-wake-companion.service` is on no allowlist.
* No `owner_decision`, `owner_gate` or `policy_decision` row in `control_plane.db`
  authorizes any service restart; the only owner_decision rows are 2026-09-02 scope
  answers with `authenticated=0`.
* An earlier automated message on the same channel stated that no owner authorization
  exists for these four gates, naming this one first.

If the authorization is real, its durable form already exists: a `service_ops: true` +
`services: [owner-os-wake-companion.service]` record for this project, or an
authenticated `owner_decision`. Either makes it checkable.

What WAS done instead — the companion's own code path, run in a FRESH process against the
live panes with `CONTROL_PLANE_DB` pointed at a temp file (no production write, no pane
written to, capture only):

* 8 live panes scanned, `control_unreachable=False`; classes: 2 `working`, 6 `idle`;
  no declared wait outstanding (main105's 23 Sep 02:00 park lapsed on its own, as designed).
* **The fixed code emitted NOTHING** — no `work_stopped_incomplete`, no false completion.

The deployed (pre-fix) code over the same panes and the same period, from the live log:

| last 24 h | deployed code |
|---|---|
| `work_stopped_incomplete` | **140** (35 of them main105, the parked agent) |
| `wake_loop_no_progress` | 24 |
| `wake_loop_stalled` (critical, owner-actionable) | 7 |
| `notifications_red` (critical, owner-actionable) | 5 — with 0 all-clears, since that fix is not deployed |
| `agent_process_failed` (critical) | 12 |

The running companion started 2026-09-22 06:20:54; every fixed file is newer
(`agent_watch.py` 09-22 17:53, `closed_loop_wake.py` 09-22 18:09,
`agent_continuation_watchdog.py` 09-22 18:21, `delivery.py` 09-23 12:35,
`stall_doctor.py` 09-23 12:39), so the process is provably running pre-fix code.

## GATE 1 — companion restart (no fleet impact)

The companion's cgroup holds only `wake_companion.py` (verified: pid 3727389); the tmux
server is NOT in it, so this restart cannot touch a pane.

    systemctl restart owner-os-wake-companion.service

Verify, in order:

1. `systemctl show owner-os-wake-companion.service -p ActiveState -p ExecMainStartTimestamp`
   → `active`, timestamp moved.
2. `journalctl -u owner-os-wake-companion -n 30 --no-pager` → no traceback; `agent-watch:`
   lines resume within ~20 s.
3. `tmux ls | wc -l` → still 8. (Nothing should change; this is the proof it did not.)
4. Within one watch pass, against `control_plane.db` read-only:
   `SELECT target, cls FROM agent_watch_state WHERE target='main105-backup-repair:0.0'`
   → `parked_scheduled` while a future park is on screen, else the ordinary class (its
   23 Sep 02:00 park has now lapsed, so `quiescent` is correct today).
5. `SELECT target,by,reason,until_ts FROM native_external_wait` → only rows written by
   `agent_watch` with `reason='scheduled_park'` and a future `until_ts`.
6. No new `work_stopped_incomplete` for a pane whose text states a future park.
7. `SELECT type,count(*) FROM event WHERE id > <max id before restart> GROUP BY type`
   → no burst of `agent_process_failed` / `agent_dead`.

## GATE 2 — re-scope the live tmux server (protects the fleet)

Must precede GATE 3. Moves pid 3739055 into its own transient scope so the daemon
restart cannot SIGTERM it. No process is signalled; the scope is abandoned when empty.

    busctl call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
      org.freedesktop.systemd1.Manager StartTransientUnit 'ssa(sv)a(sa(sv))' \
      owner-os-tmux-3739055.scope fail 1 PIDs au 1 3739055 0

Verify:

1. `cat /proc/3739055/cgroup` → `0::/system.slice/owner-os-tmux-3739055.scope`.
2. `tmux ls` → the same 8 sessions, same server pid (`tmux display-message -p '#{pid}'`).
3. `systemctl status owner-os-tmux-3739055.scope` → active.

If the call fails, STOP: do not proceed to GATE 3. A daemon restart with the server still
in `ai-runtime.service` repeats the 2026-09-22 04:20:47 fleet loss.

## GATE 3 — ai-runtime restart (only after GATE 2 verified)

    systemctl restart ai-runtime.service

Verify:

1. `tmux ls` → the same 8 sessions still present (this is the whole point of GATE 2).
2. `venv/bin/python -c "from core import tmux_control as tc; print(tc.health())"`
   → `status=ok`, `shared_cgroup=False`, `listeners=1`.
3. First engine tick in `/var/log/ai-runtime/service.log`:
   `control plane: discovered=N … dead=0` — no spurious deaths from the restart window.
4. Next time a session is created with no server running, the new server's cgroup is
   `owner-os-tmux-<pid>-<uuid>.scope`; with a server already up, `systemctl list-units
   'owner-os-tmux-*'` shows no leftover scope (they self-collect).
5. Delivery: `SELECT type FROM event ORDER BY id DESC LIMIT 20` → if a red was open,
   exactly one `notifications_recovered` with `resolves=<that red>`; no red/green flapping.

## Rollback

Every commit is code or tests; none writes schema, config or credentials, so `git revert`
is sufficient and no data migration is involved.

| Symptom after deploy | Revert |
|---|---|
| a pane that genuinely stopped mid-work is announced `completed` or `parked_scheduled` | `adb62a3` `26d2286` `9b115aa` (classifier; revert newest first) |
| a parked agent is never woken after its stated time | `9b115aa` `5b493f8` `ba321a6` `11f4a20` (the park/declaration set) |
| a managed agent (cp-canary, mess-qa-automation, arbitrage2-opus) dies and no `wake_loop_no_progress` follows within 15 min | `b90bac1` |
| real deaths stop being reported while the server is up | `7bb0769` |
| `agent_resume` fails with `systemd-run` in the error | `611dbc4` (restores the plain `tmux` argv) |
| `notifications_red` stops appearing, or fires every tick | `409c59a` |
| a genuine crash is filed as `agent_turn_stopped` | `1656e17` |

Full rollback of the code while keeping the reports:

    git revert --no-commit 11f4a20 ba321a6 5b493f8 9b115aa 7bb0769 26d2286 b90bac1 \
        adb62a3 409c59a 1656e17 611dbc4
    git commit -m "revert: local Owner OS fixes pending investigation"

then repeat GATE 1 and GATE 3 (GATE 2 need not be undone — a re-scoped server is
strictly safer and survives on its own).

Emergency, if a restart is performed WITHOUT gate 2 and the fleet is lost: the
conversation ids of every dead pane are preserved on the `agent` rows
(`lifecycle_state='dead'`), which is what `agent_resume` needs; that recovery is itself
an owner decision, project by project.

## GATE 4 — push

    git push origin ai-runtime/220-windows-bridge

23 commits, `611dbc4…1d77da1`. Nothing has been pushed; the branch has no upstream
changes from this work.
