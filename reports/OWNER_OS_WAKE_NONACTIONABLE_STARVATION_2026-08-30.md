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

---

## Quantified impact (measured 2026-08-30, read-only)

`wake_expire_audit` records events retired without ever being delivered. **87
events expired; 75 of them were `critical` or `owner_action_required=1`**, each
aging out at ~10,800s (the 3h max wake age).

By type:

| Type | Count | Actionable? |
| --- | --- | --- |
| `notification_dead_letter` | 53 | no |
| `agent_waiting_input` | 11 | **yes** |
| `task_completed` | 7 | no |
| `agent_process_failed` | 5 | no |
| `agent_dead` | 4 | no |
| `agent_prompt_needs_response` | 4 | no |

The ~70 **non-actionable** expiries are the starvation defect's body count: they
could never claim a send slot while actionable traffic flowed, so they sat pending
until they aged out. 53 of them were the `notification_dead_letter` alarm — the
owner was never told, 53 times, that a notification channel was failing.

## The 11 actionable expiries are a DIFFERENT, already-fixed bug

Starvation does not explain those: `agent_waiting_input` is actionable. Checked
rather than assumed — they were claimed **366 times** and every attempt failed
with `composer_did_not_clear_after_send`. They never lost a claim; they lost the
delivery.

That failure was a mid-August spike, and it is largely gone:

```
2026-08-16  511      2026-08-24    3
2026-08-17  490      2026-08-25    1
2026-08-20  273      2026-08-28    1
2026-08-21  106      2026-08-29   18
```

Last three days: **744 delivered, 68 failed (91.6% success)**. Residual, not
systemic — prior composer/verifier work already addressed it. **No fix is proposed
here**, and the 11 expiries are historical collateral, not evidence of a live
defect.

## What this changes about the gate

It does not change the decision, but it sizes it. Deploying this fix means the
class of alert that silently expired ~70 times starts reaching the owner at up to
one per 900s per route. The safest default remains **not deploying blind**: the
volume increase is real, and the owner should choose it deliberately.
