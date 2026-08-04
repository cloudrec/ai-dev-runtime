"""Detached 6h/24h soak checkpoint evaluator.

The interactive waiters kept being killed with the session; the evidence must not depend
on one. This runs under setsid alongside the recorder: it waits until the sample count
reaches a threshold, then writes a verdict file that any later session can simply read.

It only summarises what the recorder captured — it never decides the phase verdict.
"""
from __future__ import annotations

import json
import os
import sys
import time

SOAK = os.getenv("PHASE2_SOAK_OUT", "/root/ai-dev-runtime/reports/phase2_soak.jsonl")
OUT_DIR = "/root/ai-dev-runtime/reports"
MANAGED = ["cp-canary:0.0", "mess-qa-automation:0.0", "arbitrage2-opus:0.0"]


def load():
    rows = []
    try:
        with open(SOAK) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
    except FileNotFoundError:
        pass
    return rows


def analyse(rows, label):
    if not rows:
        return {"label": label, "samples": 0, "ok": False, "reason": "no samples"}
    span = rows[-1]["epoch"] - rows[0]["epoch"]
    gaps = [round(b["epoch"] - a["epoch"]) for a, b in zip(rows, rows[1:])
            if b["epoch"] - a["epoch"] > 150]
    dup = [r["ts"] for r in rows if r.get("duplicates")]
    answered = [g for r in rows for g in (r.get("gate_answers") or []) if g.get("answered")]
    # a managed session parked with genuinely queued text across >2 consecutive samples
    stall = {t: 0 for t in MANAGED}
    worst = {t: 0 for t in MANAGED}
    for r in rows:
        for t in MANAGED:
            s = (r.get("sessions") or {}).get(t) or {}
            stuck = s.get("state") in ("idle", "waiting_input") and bool((s.get("queued") or "").strip())
            stall[t] = stall[t] + 1 if stuck else 0
            worst[t] = max(worst[t], stall[t])
    recoveries = {}
    quarantined = []
    last_rec = (rows[-1].get("recovery") or {})
    for t, v in last_rec.items():
        if isinstance(v, dict):
            recoveries[t] = v.get("recoveries_6h")
            if v.get("quarantined"):
                quarantined.append(t)
    markers = rows[-1].get("terminal_markers") or []
    service_states = {tuple(r.get("service") or [])[-1] if r.get("service") else "?" for r in rows}
    audit_ok = all(r.get("audit_ok", True) for r in rows)
    errors = [r for r in rows if r.get("sample_error")]

    ok = (not gaps and not dup and not answered and not quarantined
          and all(v <= 2 for v in worst.values()) and audit_ok
          and service_states <= {"active"} and not errors)
    return {
        "label": label,
        "samples": len(rows),
        "window_hours": round(span / 3600, 2),
        "first": rows[0]["ts"], "last": rows[-1]["ts"],
        "sampling_gaps_over_150s": gaps,
        "duplicate_pane_samples": dup,
        "gate_answers": answered,
        "max_stall_streak_per_managed_session": worst,
        "recoveries_6h": recoveries,
        "quarantined": quarantined,
        "terminal_markers": [(m.get("target"), m.get("status")) for m in markers
                             if isinstance(m, dict)],
        "service_states_seen": sorted(service_states),
        "audit_log_ok": audit_ok,
        "sample_errors": len(errors),
        "clean": ok,
    }


def main():
    targets = [(int(os.getenv("PHASE2_CHECKPOINT_SAMPLES", "360")), "6h",
                f"{OUT_DIR}/phase2_soak_6h_checkpoint.json"),
               (1440, "24h", f"{OUT_DIR}/phase2_soak_24h_checkpoint.json")]
    done = set()
    deadline = time.time() + 26 * 3600
    while time.time() < deadline and len(done) < len(targets):
        rows = load()
        for need, label, path in targets:
            if label in done or len(rows) < need:
                continue
            res = analyse(rows[:need], label)
            res["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(path, "w") as fh:
                json.dump(res, fh, indent=2, ensure_ascii=False)
            done.add(label)
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
