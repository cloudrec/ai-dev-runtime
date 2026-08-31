#!/usr/bin/env python3
"""Read-only check of the ONE claim that could not be settled in production.

`66fe932` made the continuation cap stop waking the owner for an agent that has no assigned
task: the gate still opens, so sends stop, but it emits at `info` with
`owner_action_required=0`. Live, that was unobservable — every candidate target already had
an `agent_continuation_exhausted` inside the 6 h `nativesup:gate:<target>` dedup window, so
silence proved nothing. `9e8c439` proves it deterministically in an isolated DB; this checks
the same claim against production once a window has actually expired.

Writes nothing. Exit 0 = confirmed, 2 = not yet observable, 3 = CONTRADICTED.

Two arithmetic traps, both hit while investigating this and both avoided here:
  * `event.ts` is ISO with a `T` separator, so string-comparing it against
    `datetime('now')` (a space-separated form) silently mis-sorts. Use `ts_epoch`.
  * `strftime('%s','now')` returns TEXT, and SQLite orders every numeric below every
    string, so `ts_epoch + N < strftime(...)` is unconditionally true. Cast it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

GATE_TTL_SECS = int(os.getenv("NATIVE_SUPERVISOR_GATE_TTL_SECS", "21600"))
DB = os.getenv("CONTROL_PLANE_DB", "/root/ai-dev-runtime/control_plane.db")


def check(db: str = DB, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, ts, agent_id, severity, COALESCE(owner_action_required,0), ts_epoch "
            "FROM event WHERE type='agent_continuation_exhausted' ORDER BY ts_epoch").fetchall()
    finally:
        conn.close()
    if not rows:
        return {"status": "no_gate_events", "exit": 2, "detail": "nothing to observe yet"}

    # The window that matters is the one opened by the FIRST gate event for a target.
    # Keying off the latest instead moves the deadline forward with every new event, so
    # nothing ever qualifies as evidence — the checker's own first version did exactly
    # that and reported `masked` for a case it should have confirmed.
    expiries: dict = {}
    for r in rows:                                   # already ordered by ts_epoch
        expiries.setdefault(r[2], r[5] + GATE_TTL_SECS)
    earliest = min(expiries.values())
    # An event is EVIDENCE only if it was emitted after its own target's window expired;
    # anything earlier could have been suppressed by the dedup rather than by the fix.
    evidence = [r for r in rows if r[5] >= expiries[r[2]]]
    if not evidence:
        return {"status": "masked", "exit": 2, "earliest_expiry_epoch": earliest,
                "minutes_left": max(0, int((earliest - now) // 60)),
                "detail": "every gate event predates its own dedup window expiry"}

    woke = [r for r in evidence if r[3] != "info" or r[4]]
    if woke:
        return {"status": "contradicted", "exit": 3,
                "detail": f"gate event {woke[0][0]} woke the owner "
                          f"(severity={woke[0][3]}, oar={woke[0][4]})"}
    return {"status": "confirmed", "exit": 0,
            "detail": f"{len(evidence)} post-expiry gate event(s), all info/oar=0"}


def main() -> int:
    r = check()
    print(f"status={r['status']}: {r['detail']}")
    if r.get("minutes_left") is not None:
        print(f"earliest observable: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(r['earliest_expiry_epoch']))}"
              f" ({r['minutes_left']} min left)")
    return int(r["exit"])


if __name__ == "__main__":
    sys.exit(main())
