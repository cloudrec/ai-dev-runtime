# Wake bridge — actionable traffic starved a critical event for 4 hours (2026-08-30)

Branch `fix/wake-nonactionable-starvation`, from deployed `2e4c137`.
**Local only: not pushed, merged, deployed or restarted.**

Found by reading the live `owner-os-wake-companion` log while verifying that the
repo's other services were healthy — not from a report or a test.

## Live symptom

```
not delivered for event 13383; stays pending (not_claimed:global_cooldown_active:862s)
not delivered for event 13383; stays pending (not_claimed:global_cooldown_active:865s)
```

Event **13383** is `notifications_red`, `severity=critical`,
`owner_action_required=1`, raised 2026-08-29T20:49:13Z. It was still undelivered
~4 hours later, across **115** logged attempts, while unrelated events 13668,
13673 and 13674 were delivered successfully in the same window.

## Root cause

`claim_send()` has two lanes. The actionable lane scopes its lookback to
**actionable** sends:

```sql
SELECT ts FROM wake_send WHERE allowed=1 AND COALESCE(actionable,0)=1 AND <route>
```

The non-actionable lane scoped to **nothing**:

```sql
SELECT ts FROM wake_send WHERE allowed=1 AND <route>
```

So every actionable claim reset the non-actionable window. With
`COOLDOWN_SECS=900` and `ACTIONABLE_COOLDOWN_SECS=60`, actionable wakes arriving
every 60-90s meant a non-actionable event needed a 900s gap with **no send of any
kind** — which never occurred. Not delayed: **starved**.

`notifications_red` is not in `ACTIONABLE_EVENT_TYPES`, which is defensible on its
own terms ("a live agent waiting for a response now, as opposed to a durable
record of history"). The defect is the asymmetric lookback, not the
classification.

## Evidence from `wake_send`

The countdown decays normally, then snaps back the moment an actionable wake is
claimed:

```
 age_s  event ok act  reason
   157  13674  1   1  claimed_actionable
   189  13383  0   0  global_cooldown_active:862s   <- reset
   226  13673  1   1  claimed_actionable
   257  13383  0   0  global_cooldown_active:679s
   292  13383  0   0  global_cooldown_active:713s
   330  13383  0   0  global_cooldown_active:752s
   363  13383  0   0  global_cooldown_active:784s
   400  13383  0   0  global_cooldown_active:822s
   444  13383  0   0  global_cooldown_active:865s
```

Gaps between allowed sends: **251s and 68s** — both far under the 900s a
non-actionable claim required.

## The fix

One clause: the non-actionable lane now looks back at non-actionable sends only,
mirroring the actionable lane. The rate limit is unchanged — non-actionable
events remain capped at one per `COOLDOWN_SECS` per route; they simply stop being
reset by a lane they do not share.

## Verification

* `tests/test_wake_bridge.py` 38 -> **41 passed**. New tests pin: a stream of ten
  actionable wakes inside the window does not starve a non-actionable event; the
  non-actionable cooldown still applies within its own lane; the actionable lane
  is unchanged.
* Wake subsystem gate: **178 passed** (`wake_bridge`,
  `wake_delivery_verification`, `runtime_bridge`, `zero_human_ping`,
  `control_plane_delivery`, `windows_fabric`).
* Mutation: reverting the scope reproduces the live symptom exactly —
  `global_cooldown_active:599s` — while both guard tests still pass.

## Owner gate — this changes owner-facing wake volume

Deploying it means non-actionable wakes (`notifications_red`,
`notification_dead_letter`, `agent_process_failed` and similar) start reaching the
owner at up to one per 900s per route, where today they are effectively silenced
whenever actionable traffic is flowing. That is the intended behaviour and the
point of the fix, but it is a change in how often the owner is interrupted, so it
is an owner decision rather than a routine correctness fix.

Note the interaction with the credential-gated Telegram issue: event 13383 is
itself the `notifications_red` alarm about Telegram being down. Once this ships,
that alarm can actually reach the owner through the wake path.

## Rollback

Never deployed. One clause in `core/wake_bridge.py`; no schema, config, credential
or protocol change. Restore the file.
