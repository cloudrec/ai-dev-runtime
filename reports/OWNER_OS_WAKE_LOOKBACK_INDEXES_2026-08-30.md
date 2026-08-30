# Wake bridge — the cooldown lookback tables had no indexes (2026-08-30)

Branch `perf/wake-audit-indexes`, from deployed `2e4c137`. **Local only: not
pushed, merged, deployed or restarted. No live database was modified.**

Deliberately a separate branch from `fix/wake-nonactionable-starvation`: that one
changes owner-facing wake volume and needs a decision, this one is additive and
does not. They should be landable independently.

## Measured, not assumed

Live `control_plane.db`, read-only, 2026-08-30:

| Table | Rows | Indexes |
| --- | --- | --- |
| `wake_audit` | 104,396 | **none** |
| `wake_send` | 27,575 | **none** |
| `wake_delivery` | 4,441 | **none** |

Both cooldown lookbacks are
`WHERE allowed=1 AND actionable=? AND <route> ORDER BY id DESC LIMIT 1`, and
`EXPLAIN QUERY PLAN` reported `SCAN` for both.

Timed on the live data (avg of 200 calls):

| Case | Cost |
| --- | --- |
| recent row matches (normal) | **0.021 ms** |
| no row matches (full scan of 104k) | **23 ms** |
| `wake_send` lookback | 0.010 ms |

So this is **not** a live performance problem today: `ORDER BY id DESC LIMIT 1`
stops early whenever a recent row matches. The cost appears in the no-match case —
a route that has never had a send of that class — and both tables are append-only,
so it grows without bound.

## The fix

Two composite indexes, created in the existing migration functions so a live
database picks them up on the next connection:

* `ix_wake_send_lookback ON wake_send (allowed, actionable, route_key, id)`
* `ix_wake_audit_lookback ON wake_audit (decision, actionable, id)`

The audit index is created **after** the `_AUDIT_COLUMNS` loop, because
`actionable` is itself a migrated column — indexing it before the `ALTER` would
fail on any pre-existing database.

`CREATE INDEX IF NOT EXISTS` runs on every connection and is idempotent; that is
pinned by a test rather than assumed.

## What was deliberately NOT done

`wake_audit` has **no retention or pruning anywhere** — it grows forever. That is
the more substantial finding, and it is **not** fixed here, because deleting audit
rows is a policy decision, not a cleanup: these are precisely the rows used to
diagnose the starvation defect in
`OWNER_OS_WAKE_NONACTIONABLE_STARVATION_2026-08-30.md`. The countdown-reset
evidence came out of `wake_send`. Pruning them would have destroyed the evidence
that found the bug.

Retention needs an owner decision on how long wake history is kept. Recorded, not
taken.

## Verification

* `tests/test_wake_bridge.py` 38 -> **41 passed**. New tests pin: both indexes
  exist after the paths that create each table (`claim_send` runs the send
  migration, `record` runs the audit one); the worst-case no-match lookback uses
  the index rather than a scan; creating the index twice is safe.
* Wake regression gate: **158 passed**.
* Mutation: renaming both indexes away fails all three tests.

## Rollback

Never deployed, and no live database was touched. Two `CREATE INDEX IF NOT EXISTS`
statements in `core/wake_bridge.py`. Dropping the indexes is safe at any time; no
schema column, config, credential or protocol change.
