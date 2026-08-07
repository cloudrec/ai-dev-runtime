# Owner OS — the MESS session that kept coming back from the dead

**Date:** 2026-08-07 · **Scope:** `/root/ai-dev-runtime` only. `/opt/mess` was read for its
configured path and never written to. No Telegram credentials/config, no CDP, no wake
browser or canary, no other product project. `ai-runtime.service` was **not** restarted.

---

## 1. Symptom

Several times after a teardown/restart, `mess-qa-automation` reappeared as a live Claude
process in cwd `/root`, resuming the stale conversation
`406eab3c-66f4-4a35-b1cf-4a0f657480fc`, and sat forever on the folder-trust prompt:

```
Accessing workspace: /root
Quick safety check: Is this a project you created or one you trust?
❯ 1. Yes, I trust this folder
```

Captured live from `mess-qa-automation:0.0`, pane pid 842031, started 03:25:04 CEST.

## 2. Call chain — proven, not inferred

```
commander_autopilot.decide(target)                     core/commander_autopilot.py:137
  state == "dead"
  → target present in managed_sessions.yaml            core/commander_autopilot.py:144-152
  → live_actuation granted (zz-actuation-scope.conf)
  → session_recovery.recover(target, registry=reg)     core/commander_autopilot.py:160
       cwd = entry["cwd"]  →  /opt/mess-qa-automation   core/session_recovery.py:218 (old)
       duplicate proof: live_claude_for_cwd(/opt/mess-qa-automation) → none
       crash-loop cap:  recent_recoveries(... ok=1)     → 0, cap never reached
       tmux new-session -d -s mess-qa-automation \
            -c /opt/mess-qa-automation \
            claude --resume 406eab3c-…                  core/session_recovery.py:265 (old)
       → rc=0, pane starts in /root
       verify_recovered() → cwd_matches False → "verify_failed"
       → pane LEFT RUNNING
discovery.discover() next tick                          core/control_plane/discovery.py:135
  → sees a live claude pane → lifecycle "recovered", records cwd=/root
```

Timeline for one cycle (`service.log` is CEST, `session_recovery.ts` is UTC — this offset
is why the audit row initially looked absent):

| Time | Evidence |
|---|---|
| 03:24:42 CEST | `direct lifecycle: emitted=1 ['agent_process_failed']` |
| 03:25:00 CEST | `control plane: … dead=1 recovered=0` |
| 03:25:04 CEST | pane pid 842031 = `claude --resume 406eab3c-…`, cwd `/root` |
| 01:25:11 UTC (= 03:25:11 CEST) | `session_recovery` row: `revive, ok=0, verify_failed` |
| 03:25:31 CEST | `control plane: … dead=0 recovered=1` |

## 3. Root cause — four defects that had to line up

**(1) Two registries disagreed, and the wrong one won.**
`config/project_queues.yaml:24` (the authority, loaded by `continuation_governor`) gives
`cwd: /opt/mess`. `config/managed_sessions.yaml:26` gave `cwd: /opt/mess-qa-automation` —
**a directory that does not exist**. `recover()` read only the recovery registry.

**(2) `tmux -c <missing dir>` does not fail.** Proven against real tmux:

```
$ tmux new-session -d -s cwdprobe -c /opt/does-not-exist-probe 'sleep 30'
tmux rc=0
pane cwd=/root
```

It returns **rc=0** and silently starts in the server's default directory. `recover()`
checked `rc != 0`, so it believed it had succeeded. `/root` is untrusted → trust prompt.

**(3) A dead pane was treated as a reason to revive.** There was no open work:
`os_task_queue.active_task("mess-qa-automation:0.0")` → `None`. The session was dead
because the work had **finished**. Recovery exists to survive an accidental kill mid-task.

**(4) The crash-loop cap could never fire.** `recent_recoveries()` counted only `ok=1`
rows. Every one of the five revivals logged `ok=0 / verify_failed`, so the cap saw zero,
`session_quarantine` stayed empty, and the loop repeated roughly hourly:

```
2026-08-06T21:57:53Z  2026-08-06T23:51:48Z  2026-08-07T00:47:12Z
2026-08-07T00:48:19Z  2026-08-07T01:25:11Z  2026-08-07T02:25:53Z   all verify_failed
```

**The consequence nobody caught:** a recovery that failed verification still left its
wrongly placed pane alive, which the next discovery pass then recorded as a genuine agent
(`direct_agent_lifecycle` holds `mess-qa-automation:0.0` with `cwd=/root`).

## 4. Fix — fail-closed, `/root/ai-dev-runtime` only

`core/session_recovery.py`

* `authoritative_cwd(target, entry)` — the governor's project config is the authority on a
  project directory; the recovery registry is consulted only when the governor has none.
  Divergence is resolved in favour of the project config and logged.
* `recover()` resolves the directory **before** anything else and refuses
  `project_dir_missing` (with `owner_blocker`) when it is not a real directory. No tmux
  call is made with an unusable path, so the `/root` fallback is unreachable.
* `has_authoritative_work(target)` — recovery now requires an open ledger task. No task
  and not explicit → `no_open_work`, nothing started. An unreadable ledger fails **closed**.
  It is the **last** gate before acting, deliberately: `deliberate_stop`, the duplicate
  proof and the crash-loop quarantine are stronger statements about a target and each must
  remain the reported reason. (Placing it earlier masked all three — caught by the full
  suite, not by the targeted run.)
* `recover(..., explicit=True)` — the owner/MCP resume path. It skips the open-task
  requirement (the owner asking is the reason) but still requires a real project directory.
* `recent_recoveries()` counts **every** revive attempt, so failed revivals exhaust the cap
  and reach backoff and quarantine.
* A revival that fails verification **on cwd** kills the session it just created, so a
  failed recovery can no longer leave a live Claude behind.

`config/managed_sessions.yaml` — `cwd` for `mess-qa-automation:0.0` corrected
`/opt/mess-qa-automation` → `/opt/mess`, matching the project config. The code no longer
depends on this being right, but the drift is gone.

The conversation id is used only for `--resume`; it never influences the directory.

## 5. Tests

`tests/test_session_recovery_false_resurrection.py` — **16 tests**, one per defect and one
per requirement: completed task not resurrected; open task still recovers; unreadable
ledger fails closed; project config beats a diverged registry; a stale conversation cannot
redirect the directory; the path is re-read every time (so a restart cannot lose it);
missing/empty directory refuses instead of falling back; `/root` is never a start
directory; an existing correct live agent blocks a duplicate; explicit resume works and
still refuses a bad directory; a wrongly placed pane is torn down; failed revivals exhaust
the crash-loop cap. Two further tests assert the **shipped** configuration: no
registry/project divergence for any target, and every registered project directory exists.

`tests/test_autonomy_phase2.py` — three existing tests asserted that a dead pane recovers
with **no** ledger task, which is the contract that caused this incident. Each now states
the precondition it actually means, through a `_interrupted_mid_task` helper: this session
died with work still open. Nothing was weakened to make them pass.

| Suite | Result | Exit |
|---|---|---|
| New regression file | **16 passed** | 0 |
| Targeted (recovery, autonomy_phase2, autopilot, discovery, lifecycle, orchestrator, access/context recovery) | **181 passed** | 0 |
| Full | **1738 passed, 1 failed** (559.37s) | 1 |

The single full-suite failure is
`test_control_plane_canary_sim.py::test_flags_off_by_default_before_and_after_harness` —
**pre-existing and environmental**, unrelated to this change: the shell exports
`CONTROL_PLANE_ACTUATOR_ENABLED=1` (owner-approved `canary.conf`) while the test asserts
that flag defaults OFF. With `env -u CONTROL_PLANE_ACTUATOR_ENABLED …` that file is
8 passed, exit 0.

### Requirement-by-requirement evidence

| Requirement | Test | Result |
|---|---|---|
| completed task is not resurrected | `test_a_completed_task_is_not_resurrected` | no tmux call at all |
| authoritative project_dir survives restart state | `test_the_resolved_project_dir_is_read_fresh_each_time` | re-read every call, never cached |
| stale conversation cannot override project_dir | `test_a_stale_conversation_cannot_redirect_the_project_dir` | conversation resumed, directory unchanged |
| unknown cwd fails closed, not `/root` | `test_a_missing_project_dir_refuses_instead_of_falling_back_to_root`, `test_recovery_never_starts_a_session_in_root` | `project_dir_missing`, zero tmux calls |
| correct live agent blocks duplicate | `test_an_existing_correct_live_agent_prevents_a_duplicate` | `live_claude_exists_for_cwd` |
| explicit resume with project_dir works | `test_explicit_resume_with_a_project_dir_still_works` | recovers; and still refuses a bad dir |

## 6. Rollback

```
git revert --no-edit ae1418c
```

Restores `core/session_recovery.py` and `config/managed_sessions.yaml`. No schema change,
no data migration, nothing to undo in any database. Backups of the pre-change files and
both databases: `backups/session_recovery_fix_20260807-042911/`
(`PRAGMA integrity_check` → `ok`).

## 7. Next steps (not done in this iteration)

1. **A restart of `ai-runtime.service` is required to activate this.** The running process
   (PID 758325, started 02:46:56 CEST) holds the old `session_recovery` in memory and will
   keep re-reviving until it is restarted. Deliberately not done here.
2. **One stray pane is still live**: `mess-qa-automation:0.0`, pid 842031, sitting in
   `/root` on the trust prompt. It is the artifact of the last bad revival, not a real
   agent. It should be killed, and `direct_agent_lifecycle` still records
   `mess-qa-automation:0.0` with `cwd=/root` from it. Left in place pending owner approval.
