# Windows bridge audit — closed, two defects found and fixed, no third (2026-08-29)

Read-only audit of `core/windows_bridge.py` and `core/agent_fabric.py`'s windows
path, after the two lifecycle defects were fixed and deployed in `2e4c137`.

## Found and fixed (now live)

1. **Commands for a never-polling device hung forever.** `expire_stale()` ran
   only inside `lease()`. Fixed in `get_command()` (`62df2dc`).
2. **A late result could resurrect an expired command.** `complete()` treated only
   `done`/`failed` as terminal (`8e50ae1`).

## Audited and found CORRECT — no change made

* `verify_request` — clock-skew check, signature verified **before** the nonce is
  burned (so an unauthenticated caller cannot exhaust a device's nonce space),
  PRIMARY KEY collision treated as replay, and `NONCE_TTL_SECS=900` safely exceeds
  the ±300s skew window.
* `enqueue` — rejects unknown device, non-active device, unenrolled workspace,
  disabled workspace, bad workspace id, non-allowlisted action, bad params, bad
  command-id shape; idempotent on `command_id` (a replay is a lookup, never a
  second execution).
* `lease` — cannot double-lease (`UPDATE ... WHERE status='pending'`).
* `revoke` — retires the device's `pending` and `leased` commands.
* `complete` — enforces device ownership ("a device can never answer for
  another"), bounds and redacts the result, idempotent re-post.
* `inventory` — an offline device's workspaces are listed with `alive: false`,
  `healthy: false`, `online: false`. Verified live against the real enrolled
  device (offline 46.3h): `alive: False`, `online: False`. Correct, and
  deliberate per its docstring.

## Not defects — deliberate design, recorded so they are not "fixed" later

* `FABRIC_STATE["unknown"] = "WORKING"`, and `FABRIC_STATE.get(state, "WORKING")`
  for anything unrecognised. `unknown` is an **explicit** entry, not an accidental
  default, so this is the author's choice: assume work in progress rather than
  invite intervention. Consumers that need liveness must read `alive`/`healthy`,
  which are correct.

## Minor observability gap — NOT worth a code change on its own

The fabric inventory row carries `alive`/`online` but **not** `last_seen_at` or
`seconds_since_seen`, so a consumer cannot distinguish "asleep for two minutes"
from "gone for two days". `/api/v1/windows/devices` does expose both. Noted for
whoever next touches that shape; not fixed, because inventing a schema change to
a live fabric contract for a nice-to-have is not warranted.

## Conclusion

**No third substantive defect exists in this subsystem.** The audit is closed. The
remaining Windows work is not code in this repo:

* the enrolled device runs agent **0.1.0** against a repo client of **0.2.0** —
  an owner action on the PC, not a server change;
* the client's process-orphan bug
  (`OWNER_OS_WINDOWS_CLIENT_ORPHAN_ANALYSIS_2026-08-29.md`) needs a Windows test
  environment before it can honestly be written.
