# Windows bridge — a command for a device that never polls hung forever (2026-08-29)

Branch `fix/windows-command-expiry-on-read`, cut from deployed `5618ce3`.
**Staged only: not pushed, merged, deployed or restarted.**

## The defect

`core/windows_bridge` states its contract in the module docstring:

> If the laptop is asleep the command simply expires — an offline device is a
> refusal with a reason, never a hang and never a half-applied action.

`expire_stale()` is what enforces that. It was called from exactly **one** place:
`lease()` — which runs only when the device long-polls.

So the sweep depended on the device coming back. For a device that does not, a
queued command stayed `pending` **forever**:

* `get_command()` read raw rows and never swept, so
  `GET /api/v1/windows/command/{id}` reported `pending` indefinitely.
* `wait_for_result()` polls for `done|failed|expired`, so it could never observe
  `expired` and always exited via `timed_out=True` — telling the caller "the work
  may still land later" about a command that never could.

`COMMAND_TTL_SECS = 900` was effectively dead for exactly the case the contract
is about. This is live shape, not hypothetical: the enrolled device
`win-92840f98d82ad3fe` has been offline since 2026-08-27 (`online: false`,
~41h at time of writing).

**Why the existing tests missed it.** `test_an_uncollected_command_expires_instead_of_hanging`
calls `wb.expire_stale(now=...)` *by hand*, proving the sweeper works while
asserting nothing about anything in production calling it.

## The fix

`get_command()` expires the row it is reading, and only when that row is
genuinely past its TTL. One extra `UPDATE` on the rare stale read; **zero writes
on the common path**, so an ordinary read still takes no write lock — deliberate,
given the advisory-lock discipline the workers rely on.

Not changed: `COMMAND_TTL_SECS`, `expire_stale()` itself, `lease()`, the
enqueue/complete paths, the wire protocol, and every status value. A device that
polls behaves exactly as before.

## Verification

* `tests/test_windows_bridge.py` 51 -> **55 passed**. New tests pin: a
  never-collected command reads as `expired` with its reason; `wait_for_result`
  returns `expired` rather than a bare timeout; a **fresh** command is not
  expired by being read; and a **finished** command is never rewritten by a read.
* Windows/bridge regression gate: **199 passed** (`windows_bridge`,
  `windows_client`, `windows_e2e`, `windows_fabric`, `runtime_bridge`,
  `wake_bridge`).
* Mutation: disabling the read-path expiry fails the two expiry tests while the
  two guard tests still pass. Production file restored clean.

## Rollback

Never deployed, so nothing to undo. If it later is: the change is one function in
one file. No schema, config, credential, protocol or status-value change, so
restoring `core/windows_bridge.py` is sufficient. Restore the file rather than
`reset --hard`, which would discard the 29 unrelated dirty `reports/*`.

## Gate

Landing needs merge + push + `systemctl restart ai-runtime.service`. Not
authorized; not done. Independent of `fix/test-step-process-group` and
`feat/salvage-observability`.
