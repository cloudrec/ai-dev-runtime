"""Phase 3 post-fix soak recorder — detached, restart-persistent.

Tracks what the Phase 3 acceptance cares about, which is NOT what Phase 2 tracked:
sampling gaps, duplicate submissions, wrong-project actions, unknown prompt answers,
recoveries, /clear resumes, and quarantine events.

Read-only. Runs under setsid alongside the Phase 2 recorder without disturbing it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/root/ai-dev-runtime")

OUT = os.getenv("PHASE3_SOAK_OUT", "/root/ai-dev-runtime/reports/phase3_soak.jsonl")
DURATION = float(os.getenv("PHASE3_SOAK_HOURS", "24")) * 3600
INTERVAL = 60
CP = "/root/ai-dev-runtime/control_plane.db"
AC = "/root/ai-dev-runtime/agent_control.db"
MANAGED = ["cp-canary:0.0", "mess-qa-automation:0.0", "arbitrage2-opus:0.0"]
WATCHED = MANAGED + ["payment:0.0", "owneros-direct-fix:0.0"]


def _rows(db, q, args=()):
    try:
        c = sqlite3.connect(db, timeout=10)
        c.row_factory = sqlite3.Row
        out = [dict(r) for r in c.execute(q, args)]
        c.close()
        return out
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e)[:120]}]


def sample():
    from core import agent_control as ac
    from core import continuation_governor as cg
    from core import session_recovery as sr

    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": time.time(), "sessions": {}}
    for t in WATCHED:
        try:
            ok, tail = ac.pane_capture(t, 20)
            row["sessions"][t] = {
                "state": ac.agent_status(t).get("state"),
                "capture_ok": ok,
                "queued": (ac.pending_input_text(t, tail) or "")[:60],
            }
        except Exception as e:  # noqa: BLE001
            row["sessions"][t] = {"error": str(e)[:100]}

    p = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    names = [x for x in p.stdout.split() if x]
    row["pane_counts"] = {t.split(":")[0]: names.count(t.split(":")[0]) for t in WATCHED}
    row["duplicates"] = {k: v for k, v in row["pane_counts"].items() if v > 1}

    # governor activity: submissions, blockers, wrong-project refusals
    row["governor"] = _rows(CP, "SELECT ts,target,decision FROM autopilot_run "
                                "WHERE decision LIKE 'governor%' ORDER BY rowid DESC LIMIT 8")
    row["blockers"] = _rows(CP, "SELECT target,stage,first_seen,last_seen FROM governor_blocker")
    row["gates_open"] = _rows(CP, "SELECT kind,count(*) c FROM owner_gate WHERE state='open' "
                                  "GROUP BY kind")
    # unknown prompt answers must stay ZERO unless an approved gate matched
    row["gate_answers"] = _rows(AC, "SELECT ts,target,entry_id,reason,answered "
                                    "FROM gate_answer_log ORDER BY rowid DESC LIMIT 5")
    row["recoveries"] = _rows(AC, "SELECT ts,target,action,ok,reason FROM session_recovery "
                                  "ORDER BY rowid DESC LIMIT 5")
    row["quarantine"] = _rows(AC, "SELECT target,since,reason FROM session_quarantine")
    row["rotations"] = _rows(AC, "SELECT target,status,new_conversation_id FROM context_rotation "
                                 "ORDER BY rowid DESC LIMIT 3")
    try:
        row["queue_pointers"] = {
            t: (cg.parse_queue(cfg["authoritative_pointer"]) or {}).get("pointer")
            for t, cfg in cg.load_config().items()
            if cfg.get("authoritative_pointer")
        }
    except Exception as e:  # noqa: BLE001
        row["queue_pointers"] = {"error": str(e)[:100]}
    try:
        row["recovery_status"] = sr.status()["targets"]
    except Exception as e:  # noqa: BLE001
        row["recovery_status"] = {"error": str(e)[:100]}

    row["service"] = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "-p", "ActiveState", "--value",
         "ai-runtime.service"], capture_output=True, text=True).stdout.split()
    return row


def main():
    end = time.time() + DURATION
    with open(OUT, "a") as fh:
        while time.time() < end:
            try:
                fh.write(json.dumps(sample(), ensure_ascii=False) + "\n")
                fh.flush()
            except Exception as e:  # noqa: BLE001
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "sample_error": str(e)[:200]}) + "\n")
                fh.flush()
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
