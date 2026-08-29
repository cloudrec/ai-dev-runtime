# Windows bridge — a late result could resurrect an expired command (2026-08-29)

Branch `fix/windows-late-result-after-expiry`, cut from
`fix/windows-command-expiry-on-read` (`62df2dc`) — same subsystem, and the expiry
fix makes this state materially more reachable. **Staged only: not pushed,
merged, deployed or restarted.**

## The defect

`complete()` guarded re-posts with:

```python
if row["status"] in ("done", "failed"):
    return _public_command(row)      # idempotent re-post
```

`expired` is **not** in that set, so an expired command was still writable. And
`expire_stale()` retires `pending` **and `leased`** commands, so the state is
reachable by an ordinary sequence:

1. Owner enqueues `agent.start`; the device leases it and begins the work.
2. The device goes dark mid-execution (sleep, network drop).
3. TTL passes; the command is retired -> `expired`, error
   `"device did not collect the command in time"`.
4. The owner reads that refusal and may re-issue the command on the basis of it.
5. The device wakes, finishes the ORIGINAL work, and posts its result.
6. `complete()` overwrites `expired` -> `done, ok=1`.

The owner was told the command was refused, and the record then says it
succeeded. If they re-issued in step 4, the action ran twice and nothing in the
record shows it. That is exactly the "half-applied action" the module docstring
disclaims.

Reproduced directly before the fix:

```
after lease :  leased
after expiry:  expired | owner was told: device did not collect the command in time
after late result: done ok= True
```

## The fix

`expired` is now terminal in `complete()`. A late result does not overwrite the
refusal the owner already saw.

It is **not** silently dropped: the arrival is audited as
`windows_late_result_after_expiry` (device, command, action, reported ok), because
a device reporting against an expired command means that work probably ran, and
that is precisely what an operator needs to know when reconciling a re-issue.

Deliberately unchanged: `done`/`failed` idempotent re-post, the ordinary
lease -> complete path, TTL, statuses, the wire protocol, and result
redaction/bounding.

## Verification

* `tests/test_windows_bridge.py` 55 -> **58 passed**. New tests pin: a late
  success cannot overwrite `expired` (and the original refusal text survives); a
  late failure cannot either; and a guard that the ordinary
  lease -> complete path still reaches `done`.
* Windows/bridge regression gate: **202 passed** (`windows_bridge`,
  `windows_client`, `windows_e2e`, `windows_fabric`, `runtime_bridge`,
  `wake_bridge`).
* Mutation: making `expired` writable again fails both late-result tests while
  the ordinary-path guard still passes. Production file restored clean.

## Rollback

Never deployed. If it later is: one guard clause in one function of
`core/windows_bridge.py`. No schema, config, credential, protocol or status-value
change — restoring the file is sufficient. Restore the file rather than
`reset --hard`, which would discard the 29 unrelated dirty `reports/*`.

## Gate

Landing needs merge + push + `systemctl restart ai-runtime.service`. Not
authorized; not done. This branch builds on `fix/windows-command-expiry-on-read`,
so landing it lands that one too.
