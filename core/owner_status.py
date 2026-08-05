"""Concise owner-facing status: which agents are working / idle / blocked, and why.

Every field is read from DURABLE state that something else already wrote — pane capture,
the project queues, the governor's blocker ledger, owner gates, the recovery registry. This
module invents nothing: it never proposes work, never names a next step of its own, and
never guesses a reason it cannot source.

Read-only by construction: no writes, no actuation, no tmux input.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

CP_DB = os.getenv("CONTROL_PLANE_DB", "/root/ai-dev-runtime/control_plane.db")
AC_DB = os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")


def _rows(db: str, q: str, args=()) -> list:
    try:
        c = sqlite3.connect(db, timeout=10)
        c.row_factory = sqlite3.Row
        out = [dict(r) for r in c.execute(q, args)]
        c.close()
        return out
    except Exception:  # noqa: BLE001
        return []


def _why_blocked(target: str) -> Optional[dict]:
    """The durable blocker that is still holding this target, with its exact fields.

    Newest-first, but the newest row is NOT automatically the answer: taking it blindly let a
    later blocker whose gate had been ANSWERED mask an earlier one whose gate was still open,
    and the agent then rendered as "IDLE — at rest; no durable blocker recorded" while an
    owner gate sat open against it (live: the canary's stage_c payload gate, hidden behind an
    answered paste-probe gate). A missing reason is worse than a wrong one — it reads as
    "nothing needs you".
    """
    import json
    rows = _rows(CP_DB, "SELECT stage,fields,first_seen,last_seen FROM governor_blocker "
                        "WHERE target=? ORDER BY rowid DESC", (target,))
    if not rows:
        return None
    newest = None
    for r in rows:
        # Correlate the gate to THIS blocker via the governor's own correlation id. Taking
        # "any open gate for this agent" made arbitrage2 read as blocked by an unrelated
        # `unverified_owner_decision` gate from other work — a wrong reason is worse than none.
        stage = r.get("stage") or "-"
        corr = f"gov:{target}:{stage if stage != '-' else target}"
        gates = _rows(CP_DB, "SELECT id,kind,reason FROM owner_gate WHERE agent_id=? AND "
                             "state='open' AND correlation_id=? ORDER BY rowid DESC LIMIT 1",
                      (target, corr))
        try:
            fields = json.loads(r.get("fields") or "[]")
        except Exception:  # noqa: BLE001
            fields = []
        entry = {"stage": r.get("stage"), "missing_fields": fields,
                 "since": r.get("first_seen"), "last_seen": r.get("last_seen"),
                 "gate": (gates[0] if gates else None)}
        if newest is None:
            newest = entry                      # what to report if nothing is still open
        if gates:
            return entry                        # the newest blocker STILL holding the agent
    return newest


def status(targets: Optional[list] = None) -> dict:
    """One row per managed agent: state, why, and what it is waiting on."""
    from core import agent_control as ac
    from core import continuation_governor as cg
    try:
        from core import session_recovery as sr
        recovery = sr.status().get("targets", {})
    except Exception:  # noqa: BLE001
        recovery = {}

    cfg = cg.load_config()
    targets = targets or sorted(cfg.keys())
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "agents": []}

    for t in targets:
        entry = cfg.get(t) or {}
        row = {"target": t, "project": entry.get("project", ""),
               "role": entry.get("role", "")}
        try:
            ok, tail = ac.pane_capture(t, 20)
            row["state"] = ac.agent_status(t).get("state") if ok else "unreadable"
            row["queued_input"] = bool((ac.pending_input_text(t, tail) or "").strip())
        except Exception as e:  # noqa: BLE001
            row["state"] = "unknown"
            row["error"] = str(e)[:80]
            row["queued_input"] = False

        ptr = entry.get("authoritative_pointer") or ""
        if ptr and os.path.isfile(ptr):
            q = cg.parse_queue(ptr)
            if q.get("ok"):
                row["queue_pointer"] = q.get("pointer")
                row["queue_valid"] = True
                row["queue_complete"] = bool(q.get("complete"))
            else:
                row["queue_pointer"] = None
                row["queue_valid"] = False
                row["queue_problem"] = q.get("reason")
        else:
            row["queue_pointer"] = None
            row["queue_valid"] = None

        blocked = _why_blocked(t)
        if entry.get("enabled") is False:
            # A paused project must not read as "idle" — idle invites a nudge, paused is a
            # decision. Owner pause on arbitrage2, 2026-08-05.
            row["status"] = "paused"
            row["why"] = "owner pause active — Owner OS is not acting on this project"
        elif blocked and blocked.get("gate"):
            row["status"] = "blocked"
            row["why"] = blocked["gate"]["reason"]
            row["missing_fields"] = blocked["missing_fields"]
            row["blocked_since"] = blocked["since"]
        elif row["state"] in ("working", "shell_running"):
            row["status"] = "working"
            row["why"] = "agent is executing"
        elif row["queued_input"]:
            row["status"] = "queued_input"
            row["why"] = "text is sitting unsubmitted in the input line"
        elif row["state"] in ("idle", "waiting_owner", "waiting_input"):
            row["status"] = "idle"
            row["why"] = "at rest; no durable blocker recorded"
        else:
            row["status"] = row["state"]
            row["why"] = "see state"

        rec = recovery.get(t) or {}
        if rec.get("quarantined"):
            row["status"] = "quarantined"
            row["why"] = "recovery quarantine — crash-loop protection"
        row["recoveries_6h"] = rec.get("recoveries_6h")
        out["agents"].append(row)

    out["open_owner_gates"] = _rows(
        CP_DB, "SELECT kind,count(*) c FROM owner_gate WHERE state='open' GROUP BY kind")
    return out


def render(st: Optional[dict] = None) -> str:
    """Plain-text summary — what the owner reads at a glance."""
    st = st or status()
    lines = [f"OWNER OS — agent status @ {st['generated_at']}", ""]
    for a in st["agents"]:
        head = f"  {a['target']:26} {a['status'].upper():13} {a.get('why','')}"
        lines.append(head)
        if a.get("queue_complete"):
            lines.append("      queue: complete — every stage DONE")
        elif a.get("queue_pointer"):
            lines.append(f"      queue: {a['queue_pointer']}")
        if a.get("queue_valid") is False:
            lines.append(f"      queue PROBLEM: {a.get('queue_problem')}")
        for f in (a.get("missing_fields") or [])[:4]:
            lines.append(f"      needs: {f}")
    gates = st.get("open_owner_gates") or []
    if gates:
        lines.append("")
        lines.append("  open owner gates: " +
                     ", ".join(f"{g['kind']}={g['c']}" for g in gates))
    return "\n".join(lines)


if __name__ == "__main__":
    print(render())
