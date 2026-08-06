"""Night Shift CTO — the executive layer above the existing control loops.

The loops below this (autopilot 60s, watchdog 30s, supervisor 45s, context budget 120s) already
run continuously and already ACT. What was missing is the layer that decides: observe →
diagnose → prioritize → act → verify → record, driven by events rather than by a clock.

Two invariants inherited from the ledger work, both load-bearing:

  * Every action is a ROW first. The executive proposes; `os_task` delivers; the transcript
    acknowledges. Nothing is ever inferred from what a pane appears to show.
  * The event signal is an ACCELERATOR, never the only path. A missed signal costs latency,
    not correctness, because the bounded tick still runs. Same fail-closed shape as the
    ledger's acknowledgement timeout.

This phase is deliberately TIER 0: pure deterministic code, no model calls, no token spend.
Liveness is proven by rows and timestamps, never by asking a model to say something.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# The executive wakes immediately on a signal; this is the floor it falls back to.
FALLBACK_TICK_SECS = int(os.getenv("NIGHT_SHIFT_TICK_SECS", "60"))
# Never hold more than this many tasks in flight across all targets.
MAX_INFLIGHT = int(os.getenv("NIGHT_SHIFT_MAX_INFLIGHT", "3"))
# A proposal identical to one already made inside this window is make-work.
PROPOSAL_COOLDOWN_SECS = int(os.getenv("NIGHT_SHIFT_PROPOSAL_COOLDOWN_SECS", "3600"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ns_signal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, source TEXT, kind TEXT, target TEXT, payload TEXT,
    consumed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ns_proposal (
    fingerprint TEXT PRIMARY KEY,
    target TEXT, kind TEXT, summary TEXT, first_ts REAL, last_ts REAL, count INTEGER
);
CREATE TABLE IF NOT EXISTS ns_pass (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, at TEXT, trigger TEXT, observed INTEGER, findings TEXT, acted TEXT
);
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


# ── signals: what makes the executive wake NOW ───────────────────────────────
def signal(source: str, kind: str, *, target: str = "", payload: Optional[dict] = None,
           conn=None) -> int:
    """Record something worth waking for. Cheap, durable, never blocking the emitter."""
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "INSERT INTO ns_signal (ts,at,source,kind,target,payload) VALUES (?,?,?,?,?,?)",
            (now_ts(), now_iso(), source, kind, target,
             json.dumps(payload or {})[:2000]))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def pending_signals(conn=None, limit: int = 200) -> list:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ns_signal WHERE consumed=0 ORDER BY id LIMIT ?", (limit,))]
    finally:
        if own:
            conn.close()


def consume_signals(ids: list, conn=None) -> int:
    if not ids:
        return 0
    conn, own = _conn(conn)
    try:
        conn.executemany("UPDATE ns_signal SET consumed=1 WHERE id=?", [(i,) for i in ids])
        conn.commit()
        return len(ids)
    finally:
        if own:
            conn.close()


SIGNAL_RETENTION_SECS = int(os.getenv("NIGHT_SHIFT_SIGNAL_RETENTION_SECS", str(7 * 86400)))


def prune_signals(conn=None, now: Optional[float] = None) -> int:
    """Drop consumed signals past their retention window.

    Wiring a producer with no consumer let this table reach 71 rows within minutes of
    deploying the emitter. Consumption alone is not enough — consumed rows must also age
    out, or the accelerator becomes a slow leak.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        cur = conn.execute("DELETE FROM ns_signal WHERE consumed=1 AND ts < ?",
                           (now - SIGNAL_RETENTION_SECS,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        if own:
            conn.close()


# ── the make-work brake ──────────────────────────────────────────────────────
def _fingerprint(target: str, kind: str, summary: str) -> str:
    import hashlib
    return hashlib.sha256(f"{target}\x1f{kind}\x1f{summary}".encode()).hexdigest()[:16]


def should_propose(target: str, kind: str, summary: str, *, conn=None,
                   now: Optional[float] = None) -> dict:
    """Is this proposal new work, or the same idea again?

    Two brakes, because an autonomous executive fails by DOING TOO MUCH far more often than
    by doing too little: an identical proposal inside the cooldown is suppressed, and a
    target that already has an active task is never given a second one.
    """
    now = now if now is not None else now_ts()
    fp = _fingerprint(target, kind, summary)
    conn, own = _conn(conn)
    try:
        try:
            from core import os_task_queue as q
            if q.active_task(target, conn=conn):
                return {"propose": False, "reason": "target_already_has_active_task",
                        "fingerprint": fp}
        except Exception:  # noqa: BLE001
            pass
        r = conn.execute("SELECT last_ts,count FROM ns_proposal WHERE fingerprint=?",
                         (fp,)).fetchone()
        if r and (now - float(r[0] or 0)) < PROPOSAL_COOLDOWN_SECS:
            conn.execute("UPDATE ns_proposal SET last_ts=?, count=count+1 WHERE fingerprint=?",
                         (now, fp))
            conn.commit()
            return {"propose": False, "reason": "duplicate_proposal_in_cooldown",
                    "fingerprint": fp, "seen": int(r[1] or 0) + 1}
        conn.execute(
            "INSERT INTO ns_proposal (fingerprint,target,kind,summary,first_ts,last_ts,count) "
            "VALUES (?,?,?,?,?,?,1) ON CONFLICT(fingerprint) DO UPDATE SET last_ts=excluded.last_ts,"
            "count=ns_proposal.count+1",
            (fp, target, kind, summary[:300], now, now))
        conn.commit()
        return {"propose": True, "reason": "new_proposal", "fingerprint": fp}
    finally:
        if own:
            conn.close()


def inflight(conn=None) -> int:
    """How many tasks are already in flight across every target."""
    conn, own = _conn(conn)
    try:
        from core import os_task_queue as q
        rows = q._list("state IN (?,?,?)", (q.SUBMITTED, q.ACKNOWLEDGED, q.WORKING),
                       conn=conn)
        return len(rows)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        if own:
            conn.close()


# ── observe → diagnose → prioritize ──────────────────────────────────────────
_SEVERITY_RANK = {"critical": 0, "high": 1, "warning": 2, "info": 3}


def observe(conn=None) -> dict:
    """Read durable state only. No model, no pane interpretation, no side effects."""
    out = {"signals": pending_signals(conn=conn), "inflight": inflight(conn=conn),
           "targets": {}, "open_gates": []}
    try:
        from core import continuation_governor as cg
        cfg = cg.load_config()
        for t, e in cfg.items():
            out["targets"][t] = {"enabled": bool(e.get("enabled")),
                                 "project": e.get("project", ""),
                                 "role": e.get("role", "")}
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import owner_status as osx
        gates = osx._rows(osx.CP_DB, "SELECT id,agent_id,kind,reason FROM owner_gate "
                                     "WHERE state='open'")
        out["open_gates"] = [g for g in gates
                             if osx.classify_gate(g.get("kind")) == "owner_decision"]
    except Exception:  # noqa: BLE001
        pass
    return out


def diagnose(obs: dict) -> list:
    """Turn observations into findings. Deterministic; policy-excluded and paused projects
    are classified as such rather than as problems."""
    findings = []
    for sig in obs.get("signals", []):
        findings.append({"kind": sig.get("kind"), "target": sig.get("target") or "",
                         "severity": "high" if sig.get("kind", "").endswith("failed")
                                     else "info",
                         "source": sig.get("source"), "signal_id": sig.get("id")})
    for g in obs.get("open_gates", []):
        findings.append({"kind": "owner_decision_open", "target": g.get("agent_id") or "",
                         "severity": "high", "source": "owner_gate",
                         "detail": (g.get("reason") or "")[:160]})
    for t, meta in (obs.get("targets") or {}).items():
        if not meta.get("enabled"):
            findings.append({"kind": "project_paused", "target": t, "severity": "info",
                             "source": "policy",
                             "detail": "paused or policy-excluded — not a blocker"})
    return findings


def prioritize(findings: list) -> list:
    """Critical first, then high, then the rest; stable within a severity."""
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(f.get("severity"), 9))


def executive_pass(*, trigger: str = "tick", conn=None, now: Optional[float] = None) -> dict:
    """One full pass. Records what it saw and what it did, so a later reader can audit it
    without re-deriving anything."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        obs = observe(conn=conn)
        findings = prioritize(diagnose(obs))
        acted: list = []
        # This phase ACTS only by clearing consumed signals and recording. Task creation
        # arrives with the portfolio brain (phase 4) behind the same brakes, so an executive
        # that is merely alive can never manufacture work.
        consume_signals([s["id"] for s in obs.get("signals", [])], conn=conn)
        prune_signals(conn=conn, now=now)
        conn.execute("INSERT INTO ns_pass (ts,at,trigger,observed,findings,acted) "
                     "VALUES (?,?,?,?,?,?)",
                     (now, now_iso(), trigger, len(obs.get("signals", [])),
                      json.dumps(findings)[:4000], json.dumps(acted)[:2000]))
        conn.commit()
        return {"trigger": trigger, "observed": len(obs.get("signals", [])),
                "findings": findings, "acted": acted, "inflight": obs.get("inflight", 0),
                "capacity": max(0, MAX_INFLIGHT - obs.get("inflight", 0))}
    finally:
        if own:
            conn.close()
