# Wake bridge — a submitted-but-unproven wake now leaves a record (2026-08-30)

Branch `fix/wake-abandonment-record`, from deployed `2e4c137`. **Local only: not
pushed, merged, deployed or restarted. No live database touched.**

Option **C** of the three put to the owner. A and B were not implemented.

## The gap (evidence)

`expire_stale` deliberately excludes events that were submitted:

```sql
AND NOT EXISTS (SELECT 1 FROM wake_submitted s WHERE s.event_id=a.event_id)
```

That is correct — a phrase that may already sit in the owner's chat must never be
re-offered, and `should_wake` independently refuses it with
`already_woke_for_this_event`. **Neither rule is changed here.**

But such an event then had no terminal record anywhere: never retried, never
superseded, never expired, absent from `wake_expire_audit`. Nothing recorded that
an `owner_action_required` alert might never have been seen.

Measured on the live db, read-only:

| Event | Type | Severity | oar | Attempts | Age when found |
| --- | --- | --- | --- | --- | --- |
| 12531 | `agent_waiting_input` | high | 1 | 1 | 12.1h |
| 12370 | `notifications_red` | critical | 1 | 1 | 13.5h |
| 11659 | `agent_waiting_input` | high | 1 | 1 | 17.8h |
| 11233 | `agent_waiting_input` | high | 1 | 1 | 23.6h |

All four: one delivery attempt, failed `cdp_error:WebSocketTimeoutException`, a
`wake_submitted` row present, `wake_expire_audit` absent. Event 12370 shows the
suppression explicitly — 79 subsequent decisions, every one
`already_woke_for_this_event`.

Context: of 32 events hit by that timeout, 19 later delivered and 9 of the
remaining 13 were correctly coalesced or superseded. Only these four fell through.

## What was implemented

* `wake_abandoned` — one row per event: `event_id` PRIMARY KEY (idempotent by
  construction), reason `submitted_delivery_unproven`, the last delivery reason,
  and age.
* `record_abandoned_wakes()` — sweeps exactly the set `expire_stale` excludes:
  decided, unacknowledged, not superseded, **submitted**, with **no** proven
  delivery, past `MAX_WAKE_AGE_SECS`.
* `abandoned_wakes()` — the log, newest first, for health surfaces.
* Called from `expire_stale` on the same tick, so no new scheduler.

Retirement uses the **same** `acknowledged=1` mechanism `expire_stale` uses: the
doorbell stops, the event stays fully readable in the durable CTO inbox. These
events were already unreachable via the dedupe rule; this only makes the terminal
state explicit rather than implicit.

**The no-duplicate invariant holds by construction** — this code only INSERTs into
a new audit table. It never re-offers an event, and a test pins that `should_wake`
still refuses with `already_woke_for_this_event` after recording.

**Deliberately no control-plane event emitted.** An abandonment event would itself
become a wake candidate, which could fail delivery and be abandoned in turn. A
durable audit row is the record; a feedback loop is not.

## A bug I introduced and caught

The first patch called the sweep only at the end of `expire_stale`, but that
function returns early via `if not rows: return []`. Since the abandonment set is
precisely what its query EXCLUDES, "nothing to expire" is the normal case — the
sweep would almost never have run. Fixed, and pinned by
`test_expire_stale_runs_the_sweep_even_when_nothing_is_expirable`.

## Verification

* `tests/test_wake_bridge.py` 38 -> **43 passed**. New tests: a submitted+unproven
  wake is recorded; it is NOT recorded while inside its window; a proven delivery
  is never abandoned; recording is idempotent and the event is still refused by
  the dedupe rule; the sweep runs even when nothing is expirable.
* Wake subsystem gate: **180 passed**.
* Mutation, three properties: disabling the age check, restoring the early return,
  and dropping the proven-delivery guard each fail their own test.

## Residual risks

1. **Backfill.** The sweep only records events still `acknowledged=0`. The four
   found above are in that state and would be recorded on the first tick after
   deploy — but any event already acknowledged by other means is not
   retro-recorded. This starts the log; it does not reconstruct history.
2. **Nothing consumes the log yet.** `abandoned_wakes()` exists and is tested, but
   no health surface or notification reads it. Wiring it into a surface is a
   follow-up, and doing so would add owner-visible signal — a separate decision.
3. **Volume interaction.** Unchanged by this branch (it emits no wake), but if
   `fix/wake-nonactionable-starvation` also ships, more events reach delivery and
   so more can end up abandoned. The two are independent but compose.
4. The underlying `cdp_error:WebSocketTimeoutException` is **not** fixed here —
   37 in 3 days, all routes. This makes its casualties visible, not fewer.

## Rollback

Never deployed. Additive: one new table, two new functions, two call sites in
`expire_stale`. No existing query, schema column, config, credential or protocol
changed. Restore `core/wake_bridge.py`.
