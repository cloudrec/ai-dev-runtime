"""Phase 2 long soak recorder — runs detached, survives this session, reaches 24h.

No interactive shell holds it: it is started with setsid+nohup and writes JSONL. If it
dies it is restarted by the wrapper loop in phase2_soak.sh, so the record is continuous.

Samples every 60s: managed session states, duplicate panes, recovery counts, quarantine,
terminal stickiness, queued input (ghost-aware), unapproved gate answers, service health
and audit-log integrity.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/root/ai-dev-runtime")

OUT = os.getenv("PHASE2_SOAK_OUT", "/root/ai-dev-runtime/reports/phase2_soak.jsonl")
DURATION = float(os.getenv("PHASE2_SOAK_HOURS", "24")) * 3600
INTERVAL = 60
TARGETS = ["cp-canary:0.0", "mess-qa-automation:0.0", "arbitrage2-opus:0.0",
           "payment:0.0", "owneros-direct-fix:0.0"]
MANAGED = ["cp-canary:0.0", "mess-qa-automation:0.0", "arbitrage2-opus:0.0"]
AC = "/root/ai-dev-runtime/agent_control.db"
CP = "/root/ai-dev-runtime/control_plane.db"


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
    from core import agent_continuation_watchdog as cw
    from core import project_state as ps
    from core import session_recovery as sr

    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": time.time(), "sessions": {}}
    for t in TARGETS:
        try:
            ok, tail = ac.pane_capture(t, 20)
            row["sessions"][t] = {
                "state": ac.agent_status(t).get("state"),
                "capture_ok": ok,
                # ghost-aware: the dim recall suggestion is not queued work
                "queued": (ac.pending_input_text(t, tail) or "")[:60],
            }
        except Exception as e:  # noqa: BLE001
            row["sessions"][t] = {"error": str(e)[:100]}

    p = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}"],
                       capture_output=True, text=True)
    names = [x for x in p.stdout.split() if x]
    row["pane_counts"] = {t.split(":")[0]: names.count(t.split(":")[0]) for t in TARGETS}
    row["duplicates"] = {k: v for k, v in row["pane_counts"].items() if v > 1}

    try:
        row["recovery"] = sr.status()["targets"]
    except Exception as e:  # noqa: BLE001
        row["recovery"] = {"error": str(e)[:100]}
    try:
        row["terminal_markers"] = ps.readout()
    except Exception as e:  # noqa: BLE001
        row["terminal_markers"] = [{"error": str(e)[:100]}]

    row["gate_answers"] = _rows(AC, "SELECT ts,target,entry_id,command,answer,reason,answered "
                                    "FROM gate_answer_log ORDER BY rowid DESC LIMIT 5")
    row["autopilot"] = _rows(CP, "SELECT ts,target,decision FROM autopilot_run "
                                 "ORDER BY rowid DESC LIMIT 6")
    row["health"] = _rows(AC, "SELECT * FROM cw_health ORDER BY rowid DESC LIMIT 1")
    row["service"] = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "-p", "ActiveState", "--value",
         "ai-runtime.service"], capture_output=True, text=True).stdout.split()
    # audit-log integrity: the tables must exist and be readable
    row["audit_ok"] = all("error" not in (r or {}) for r in
                          (row["gate_answers"][:1] or [{}]) + (row["health"][:1] or [{}]))
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
