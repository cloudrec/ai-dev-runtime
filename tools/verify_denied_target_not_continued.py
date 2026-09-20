#!/usr/bin/env python3
"""Read-only post-restart proof for efae654 (event 55938).

`is_supervised` typed into `diamond-auction:0.0` — a deny-listed, deliberately parked agent
— 26 times because it read the denylist against the resolved project alone. The fix loads
only when the companion restarts, so the claim "a denied target is never continued again"
is settled by the supervision journal AFTER that restart, and by nothing before it (the
6 h gate from 55938 suppresses sends until ~21:43Z regardless of the code).

Criteria, over `native_supervision` + `native_supervision_idle` rows for the target with
`ts_epoch >= since` (default: the companion's current start time):
  * no row with action `continue` (event path or idle sweep);
  * the first row that is not a gate-cooldown skip (`continuation_gate_open`,
    `cap_reached_but_recent_intentional_external_wait`) reads `skip` /
    `value_bearing_send_blocked` — the terminal, never-typed-into reason.
Writes nothing. Exit 0 = confirmed, 2 = not yet observable, 3 = CONTRADICTED.
"""
from __future__ import annotations
import argparse
import sqlite3
import subprocess
import sys
import time

DB = "/root/ai-dev-runtime/control_plane.db"
TARGET = "diamond-auction:0.0"
EXPECTED = "value_bearing_send_blocked"
COOLDOWN_SKIPS = {"continuation_gate_open", "cap_reached_but_recent_intentional_external_wait"}


def companion_started() -> float | None:
    try:
        out = subprocess.run(["systemctl", "show", "owner-os-wake-companion",
                              "-p", "ExecMainStartTimestampMonotonic", "-p", "ActiveEnterTimestamp"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if line.startswith("ActiveEnterTimestamp=") and line.split("=", 1)[1].strip():
            stamp = line.split("=", 1)[1].strip()
            try:
                return time.mktime(time.strptime(stamp, "%a %Y-%m-%d %H:%M:%S %Z"))
            except ValueError:
                return None
    return None


def judge(rows: list, *, expected: str = EXPECTED) -> dict:
    """Pure. `rows` = [(ts_epoch, source, action, reason)] for one target, any order."""
    rows = sorted(rows, key=lambda r: r[0])
    continued = [r for r in rows if r[2] == "continue"]
    if continued:
        r = continued[0]
        return {"status": "contradicted", "exit": 3,
                "detail": f"{r[1]} continued the target at epoch {r[0]:.0f} ({r[3]})"}
    decisive = [r for r in rows if not (r[2] == "skip" and r[3] in COOLDOWN_SKIPS)]
    if not decisive:
        return {"status": "not_yet_observable", "exit": 2,
                "detail": f"{len(rows)} row(s), all gate-cooldown skips or none; "
                          f"no supervision decision recorded since the restart"}
    first = decisive[0]
    if first[2] == "skip" and first[3] == expected:
        return {"status": "confirmed", "exit": 0,
                "detail": f"first decision {first[1]} skip/{expected}; "
                          f"{len(rows)} row(s), 0 continue"}
    return {"status": "contradicted", "exit": 3,
            "detail": f"first decision was {first[1]} {first[2]}/{first[3]}, not skip/{expected}"}


def load(db: str, target: str, since: float) -> list:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ev = conn.execute("SELECT ts_epoch, 'event_path', action, reason FROM native_supervision "
                          "WHERE target=? AND ts_epoch>=?", (target, since)).fetchall()
        idle = conn.execute("SELECT ts_epoch, 'idle_sweep', action, reason FROM native_supervision_idle "
                            "WHERE target=? AND ts_epoch>=?", (target, since)).fetchall()
    finally:
        conn.close()
    return list(ev) + list(idle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=TARGET)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--since", type=float, default=None,
                    help="epoch; default = companion ActiveEnterTimestamp")
    a = ap.parse_args()
    since = a.since if a.since is not None else companion_started()
    if since is None:
        print("status=not_yet_observable: companion start time unknown; pass --since")
        return 2
    r = judge(load(a.db, a.target, since))
    print(f"status={r['status']}: {r['detail']} (since {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(since))})")
    return int(r["exit"])


if __name__ == "__main__":
    sys.exit(main())
