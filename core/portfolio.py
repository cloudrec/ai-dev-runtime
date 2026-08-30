"""Portfolio brain — durable goals, backlogs and an opportunity ledger.

This is the DATA layer and the scoring, deliberately without dispatch. Recording and ranking
an idea is safe; acting on one needs the owner's permitted-work set, which does not exist yet.
So `propose()` writes a row and stops. Nothing here creates a task, touches an agent or spends
a token.

Durable by design so no long chat context is required: an idea survives restarts, compaction
and a fresh session, and the reason it was ranked where it was survives with it.

Scoring is deliberately blunt and explainable:

    score = expected_revenue * confidence * reuse_bonus / (effort * risk)

Anything cleverer would be false precision on numbers that are themselves estimates. The
point is a defensible ORDER, not a valuation — and every field that produced the number is
stored beside it so a ranking can always be argued with.
"""
from __future__ import annotations

import os
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# Idle capacity only: the portfolio must never compete with managed project work.
MAX_OPEN_EXPERIMENTS = int(os.getenv("PORTFOLIO_MAX_OPEN_EXPERIMENTS", "2"))

KIND_OPPORTUNITY, KIND_EXPERIMENT, KIND_PROMOTION = "opportunity", "experiment", "promotion"
STATE_PROPOSED, STATE_SPECIFIED = "proposed", "specified"
STATE_APPROVED, STATE_REJECTED, STATE_DONE = "approved", "rejected", "done"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pf_goal (
    project TEXT PRIMARY KEY, goal TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pf_item (
    id TEXT PRIMARY KEY,
    kind TEXT, project TEXT, title TEXT, detail TEXT, state TEXT,
    expected_revenue REAL, confidence REAL, effort REAL, risk REAL, reuse REAL,
    score REAL, evidence TEXT, created_at TEXT, created_ts REAL, updated_at TEXT,
    decided_by TEXT, decision_note TEXT
);
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def set_goal(project: str, goal: str, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        conn.execute("INSERT INTO pf_goal (project,goal,updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(project) DO UPDATE SET goal=excluded.goal,"
                     "updated_at=excluded.updated_at", (project, goal, now_iso()))
        conn.commit()
        return {"project": project, "goal": goal}
    finally:
        if own:
            conn.close()


def goals(conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT project,goal FROM pf_goal")}
    finally:
        if own:
            conn.close()


def score(*, expected_revenue: float, confidence: float, effort: float, risk: float,
          reuse: float = 1.0) -> float:
    """Blunt and explainable. Effort and risk are clamped at a floor so a single optimistic
    zero cannot produce an infinite score and park a pet idea permanently at the top."""
    eff = max(float(effort), 0.1)
    rsk = max(float(risk), 0.1)
    conf = min(max(float(confidence), 0.0), 1.0)
    return round(float(expected_revenue) * conf * max(float(reuse), 0.1) / (eff * rsk), 4)


def propose(kind: str, *, project: str, title: str, detail: str = "",
            expected_revenue: float = 0.0, confidence: float = 0.5,
            effort: float = 1.0, risk: float = 1.0, reuse: float = 1.0,
            evidence: str = "", conn=None) -> dict:
    """Record an idea. Writes a row and STOPS — no task, no agent, no spend.

    Dispatch is intentionally absent until the owner defines the permitted-work set.
    """
    import uuid
    conn, own = _conn(conn)
    try:
        iid = uuid.uuid4().hex[:12]
        sc = score(expected_revenue=expected_revenue, confidence=confidence,
                   effort=effort, risk=risk, reuse=reuse)
        conn.execute(
            "INSERT INTO pf_item (id,kind,project,title,detail,state,expected_revenue,"
            "confidence,effort,risk,reuse,score,evidence,created_at,created_ts,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, kind, project, title[:200], detail[:2000], STATE_PROPOSED,
             float(expected_revenue), float(confidence), float(effort), float(risk),
             float(reuse), sc, evidence[:500], now_iso(), now_ts(), now_iso()))
        conn.commit()
        return {"id": iid, "kind": kind, "project": project, "title": title,
                "state": STATE_PROPOSED, "score": sc, "dispatched": False,
                "note": "recorded only — dispatch requires the owner's permitted-work set"}
    finally:
        if own:
            conn.close()


def ranked(kind: Optional[str] = None, state: str = STATE_PROPOSED, limit: int = 20,
           conn=None) -> list:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        if kind:
            rows = conn.execute("SELECT * FROM pf_item WHERE kind=? AND state=? "
                                "ORDER BY score DESC, created_ts ASC LIMIT ?",
                                (kind, state, limit))
        else:
            rows = conn.execute("SELECT * FROM pf_item WHERE state=? "
                                "ORDER BY score DESC, created_ts ASC LIMIT ?", (state, limit))
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def decide(item_id: str, state: str, *, by: str = "owner", note: str = "",
           conn=None) -> dict:
    """Approval and rejection are OWNER acts. Nothing in this module approves its own idea."""
    if state not in (STATE_APPROVED, STATE_REJECTED, STATE_SPECIFIED, STATE_DONE):
        return {"ok": False, "reason": f"invalid_state:{state}"}
    conn, own = _conn(conn)
    try:
        conn.execute("UPDATE pf_item SET state=?, decided_by=?, decision_note=?, updated_at=? "
                     "WHERE id=?", (state, by, note[:300], now_iso(), item_id))
        conn.commit()
        return {"ok": True, "id": item_id, "state": state, "decided_by": by}
    finally:
        if own:
            conn.close()


def has_idle_capacity(conn=None) -> dict:
    """May the portfolio consume capacity at all? Managed project work always wins."""
    try:
        from core import night_shift as ns
        inflight = ns.inflight(conn=conn)
        cap = ns.MAX_INFLIGHT
    except Exception:  # noqa: BLE001
        inflight, cap = 0, 1
    open_items = len(ranked(state=STATE_APPROVED, limit=100, conn=conn))
    if inflight > 0:
        return {"idle": False, "reason": "managed project work is in flight",
                "inflight": inflight}
    if open_items >= MAX_OPEN_EXPERIMENTS:
        return {"idle": False, "reason": "open experiment limit reached",
                "open": open_items}
    return {"idle": True, "reason": "no managed work in flight", "capacity": cap - inflight}
