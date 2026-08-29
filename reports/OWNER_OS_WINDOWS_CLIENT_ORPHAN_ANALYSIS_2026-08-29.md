# Windows client — killing a Claude turn leaves the real process running (2026-08-29)

Read-only analysis of `clients/windows/owner_os_agent.py`. **No code change made** —
see "Why no fix here". Same defect class as the two server-side leaks already
staged, but the consequence differs.

## The mechanism

The client launches Claude as:

```python
self.proc = subprocess.Popen(argv, cwd=self.path, stdin=PIPE, stdout=PIPE,
                             stderr=PIPE, shell=False, text=True, ...)
```

with **no** `creationflags`, no `CREATE_NEW_PROCESS_GROUP`, no Job Object
(grep for `creationflags|CREATE_NEW_PROCESS_GROUP|JobObject|taskkill`: none).

Two kill paths both act on the direct child only:

* `send()` on timeout — `self.proc.kill()` (line ~506)
* `stop()` — `proc.terminate()`, then `proc.kill()` after 10s (line ~544)

On Windows the direct child is **`cmd.exe`**: the module's own docstring (line 21)
states "On Windows `claude` is a `.cmd` shim", and `_claude_cmd()` resolves
`claude.cmd`. Killing the shim does not kill the `node` process it spawned, which
is the actual Claude turn.

## Consequence — misattribution, not invisibility

`status()` calls `foreign_claude_in(self.path)`, so an orphan IS noticed. But it
is reported as:

```
state: "external_session", running: True, controllable: False,
owned_by: "owner (started outside Owner OS)"
```

So Owner OS's **own** orphan is reported back to the owner as a session the owner
started outside Owner OS, and marked **not controllable**. `stop()` cannot reach
it either — `self.proc` was cleared, so the runner believes it owns nothing.

Chain: a timed-out or owner-stopped turn keeps running; it is attributed to the
owner; it cannot be stopped through the bridge; and it keeps consuming API budget
until the machine is rebooted or it is killed by hand in Task Manager.

The comment above `status()` — "A Claude the OWNER started is reported, not
hidden: 'idle' would be a lie" — is the right instinct aimed at the wrong case.
It was written for a genuinely foreign session and silently absorbs this one.

## Why no fix here

The correct fix is Windows-specific process-tree termination — `creationflags=
subprocess.CREATE_NEW_PROCESS_GROUP` plus `taskkill /T /F /PID`, or a Job Object
with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. **None of it is testable on this Linux
host**, and this file runs only on the Windows PC. Shipping untested process-kill
code to the one enrolled device is worse than a precise report.

It also cannot reach the device without a client update, which is already an owner
action (see the 0.1.0 -> 0.2.0 skew in
`OWNER_OS_DEPLOY_READINESS_2026-08-29.md`).

## Proposed fix, for when a Windows test is possible

1. `Popen(..., creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)`.
2. Replace both `proc.kill()` sites with a tree kill:
   `subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)])`, falling back
   to `proc.kill()` if `taskkill` is unavailable.
3. Regression test on Windows: start a turn, `stop()` it, assert no `node` child
   survives — the direct analogue of the two server-side reap tests already
   written.

## Status

Recorded, not fixed. Independent of the three staged server branches; it changes
only the device client. No credential, protocol, schema or server change is
implied.
