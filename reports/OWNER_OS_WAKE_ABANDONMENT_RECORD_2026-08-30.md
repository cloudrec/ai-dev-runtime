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
superseded, never expired, absent from `wake_expire_audit`. Nothing recorded the unresolved outcome.

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


---

# Correction and WebSocket root cause (same day, after further read-only work)

## Correction to this report's framing

Above I wrote that these events mean "an `owner_action_required` alert might never
have been seen". **That overstates it.** `cdp_composer` latches `wake_submitted`
only at an explicit boundary:

```python
# THE LATCH BOUNDARY. A cleared composer means the page took the
# phrase — from here on it may be in the chat, so this event must
# never be submitted again, whatever verification says below.
_latch_submitted(source, event_id)
```

The latch fires **only after the composer is observed cleared**. So a latched
event's phrase almost certainly DID reach the owner's chat. What was never
confirmed is that the **assistant started** on it. That is a materially weaker
failure than a lost alert, and the code and health text now say so.

The record is still worth having — an unresolved wake should not vanish — but it
should be read as "phrase landed, assistant-start unconfirmed", not "the owner was
never told".

## WebSocket timeout root cause — no fix needed, and option B is unnecessary

37 `cdp_error:WebSocketTimeoutException` in 3 days, all routes, 1-5/hour, isolated
(one attempt per event). **Not** the wedged-renderer shape: that was the 4214
incident (113 consecutive against one hung page), and it is already guarded by
`page_responsive()` + `recover_wedged_tab()` which run BEFORE the attempt.

Given the latch boundary, these timeouts occur during **post-send verification** —
the loop waiting for the composer to clear and the assistant to start, against a
15s session timeout. They are transient/environmental, not a wedged page.

**This retires option B** from the earlier decision. B was "retry when the failure
provably preceded submission" — but the latch boundary already performs exactly
that discrimination: a pre-send failure never latches, so it stays retryable and
IS retried. Only post-latch failures are suppressed, and suppressing those is
correct, because the phrase is probably already in the chat. There is nothing for
B to add.

**No production, config or credential change is proposed for the WebSocket issue.**
It is a transient network/page condition whose casualties are now visible.

## A grep error worth recording

While tracing this I claimed `mark_submitted` had "no production caller". Wrong:
`tools/cdp_composer.py:323` calls it in both the pre- and post-deploy trees. The
grep that produced that claim filtered out `worktrees/` while running from inside
a worktree, excluding the very file being searched.

## Separate live-vs-git finding: the companion runs pre-deploy code

`owner-os-wake-companion.service` started **2026-08-29 20:46:51 CEST**. The deploy
commit `2e4c137` is 23:37 and the `ai-runtime.service` restart was 23:40 — only
`ai-runtime.service` was restarted, so the companion still runs the code it loaded
at 20:46 (`5618ce3`).

**Impact of that gap: none from this deploy.** `2e4c137` changed `ai_planner`,
`job_executor`, `deliver` and `windows_bridge`; the companion uses `wake_bridge`
and `cdp_composer`, neither of which that deploy touched. So the stale process is
running identical code for its own purposes.

It matters for the FUTURE, though: any wake-bridge change — including the
abandonment record here, and `fix/wake-nonactionable-starvation` — will not take
effect in the companion until that service is restarted too. Restarting it is an
owner gate, not taken.
