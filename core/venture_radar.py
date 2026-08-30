"""Venture Radar core (task 193) — candidate ledger for new-venture ideas.

Canonical Owner OS 2.0 core (2026-08-15 architecture decision): this module is
the single source of truth; seo-backend or any UI consumes it over the /api/v1
adapter contract only.

Same discipline as core/portfolio.py: this is a DATA + POLICY layer with no
dispatch. Recording, researching and ranking a candidate is safe; building one
is a business decision. So APPROVED/REJECTED/BUILDING are owner acts recorded
in a durable decision ledger, and nothing in this module creates a task,
touches an agent, spends money, or calls the network.

A candidate is a CARD with a closed field set (unknown keys refused, exactly
like the Task Contract): the point is that every candidate answers the same
questions, so two candidates can be argued against each other line by line.

Lifecycle (fail-closed; no self-approval):

    IDEA -> RESEARCHED -> PROPOSED -> APPROVED | REJECTED
    APPROVED -> BUILDING

Scoring stays blunt and explainable, like portfolio.score: a defensible ORDER,
not a valuation, with every input stored beside the result.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

MODE_RECOMBINATION = "adjacent_recombination"   # recombine existing assets
MODE_FRESH_NICHE = "fresh_niche"                # net-new market entry
MODES = (MODE_RECOMBINATION, MODE_FRESH_NICHE)

IDEA, RESEARCHED, PROPOSED = "IDEA", "RESEARCHED", "PROPOSED"
APPROVED, REJECTED, BUILDING = "APPROVED", "REJECTED", "BUILDING"

TRANSITIONS = {
    IDEA: {RESEARCHED},
    RESEARCHED: {PROPOSED},
    PROPOSED: {APPROVED, REJECTED},
    APPROVED: {BUILDING, REJECTED},
    REJECTED: set(),
    BUILDING: set(),
}
# states only the OWNER may enter — the radar never green-lights its own idea
OWNER_ONLY_STATES = {APPROVED, REJECTED, BUILDING}

# The closed card vocabulary. Every candidate answers the same questions.
CARD_FIELDS = (
    "problem",              # what hurts, for whom
    "target_buyer",         # who pays
    "why_now",              # timing argument
    "market_evidence",      # observed demand signals (not hopes)
    "differentiation",      # why us / why not the incumbents
    "synergy",              # what existing assets it reuses
    "mvp_scope",            # smallest sellable version
    "monetization",         # how it charges
    "acquisition",          # how buyers find it
    "cost",                 # build+run estimate, 1 (cheap) .. 5 (heavy)
    "difficulty",           # execution difficulty, 1 .. 5
    "risks",                # what kills it
    "validation_experiment",  # cheapest test that produces evidence
    "kill_criteria",        # observable result that means STOP
    "confidence",           # 0..1 belief in the evidence
)
REQUIRED_FIELDS = ("problem", "target_buyer", "mvp_scope",
                   "validation_experiment", "kill_criteria")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vr_candidate (
    id TEXT PRIMARY KEY,
    mode TEXT, title TEXT, state TEXT, card TEXT,
    score REAL, created_at TEXT, created_ts REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS vr_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT, from_state TEXT, to_state TEXT,
    by TEXT, note TEXT, at TEXT, ts REAL
)
"""


class RadarError(Exception):
    """Refusal with an exact reason — mapped to HTTP 409 by the API layer."""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def validate_card(card: dict) -> dict:
    if not isinstance(card, dict):
        raise RadarError("card must be an object")
    unknown = sorted(set(card) - set(CARD_FIELDS))
    if unknown:
        raise RadarError(f"unknown card fields: {unknown} — the vocabulary is closed")
    missing = [f for f in REQUIRED_FIELDS if not str(card.get(f, "")).strip()]
    if missing:
        raise RadarError(f"required card fields missing/empty: {missing}")
    return card


def _num(card: dict, key: str, default: float) -> float:
    v = card.get(key)
    return float(default) if v is None or v == "" else float(v)


def score_card(card: dict) -> float:
    """Blunt, explainable order. Confidence up, cost and difficulty down —
    clamped floors so one optimistic zero cannot pin an idea to the top.
    Same shape as portfolio.score on purpose: comparable everywhere."""
    conf = min(max(_num(card, "confidence", 0.3), 0.0), 1.0)
    cost = max(_num(card, "cost", 3), 0.5)
    diff = max(_num(card, "difficulty", 3), 0.5)
    return round(100.0 * conf / (cost * diff), 4)


def propose(mode: str, title: str, card: dict, *, state: str = IDEA,
            conn=None) -> dict:
    """Record a candidate and STOP. No task, no agent, no spend, no network."""
    if mode not in MODES:
        raise RadarError(f"unknown mode {mode!r}; valid: {MODES}")
    if state not in (IDEA, RESEARCHED, PROPOSED):
        raise RadarError(f"a new candidate cannot start in {state!r}")
    if not (title or "").strip():
        raise RadarError("title required")
    validate_card(card)
    conn, own = _conn(conn)
    try:
        cid = uuid.uuid4().hex[:12]
        sc = score_card(card)
        conn.execute(
            "INSERT INTO vr_candidate (id,mode,title,state,card,score,"
            "created_at,created_ts,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, mode, title[:200], state, json.dumps(card, ensure_ascii=False),
             sc, now_iso(), now_ts(), now_iso()))
        conn.execute(
            "INSERT INTO vr_decision (candidate_id,from_state,to_state,by,note,at,ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, "", state, "radar", "recorded", now_iso(), now_ts()))
        conn.commit()
        return {"id": cid, "mode": mode, "title": title, "state": state,
                "score": sc, "dispatched": False,
                "note": "recorded only — building is an owner decision"}
    finally:
        if own:
            conn.close()


def update_card(candidate_id: str, card: dict, conn=None) -> dict:
    """Research updates the card (pre-decision states only); score follows."""
    validate_card(card)
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT state FROM vr_candidate WHERE id=?",
                           (candidate_id,)).fetchone()
        if not row:
            raise RadarError(f"unknown candidate {candidate_id}")
        if row[0] in OWNER_ONLY_STATES:
            raise RadarError(f"card is frozen in {row[0]} — the decided card is "
                             "the record the owner decided on")
        sc = score_card(card)
        conn.execute("UPDATE vr_candidate SET card=?, score=?, updated_at=? WHERE id=?",
                     (json.dumps(card, ensure_ascii=False), sc, now_iso(), candidate_id))
        conn.commit()
        return {"id": candidate_id, "score": sc}
    finally:
        if own:
            conn.close()


def transition(candidate_id: str, to_state: str, *, by: str, note: str = "",
               conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT state FROM vr_candidate WHERE id=?",
                           (candidate_id,)).fetchone()
        if not row:
            raise RadarError(f"unknown candidate {candidate_id}")
        cur = row[0]
        if to_state not in TRANSITIONS.get(cur, set()):
            raise RadarError(f"illegal transition {cur} -> {to_state}")
        if to_state in OWNER_ONLY_STATES and by != "owner":
            raise RadarError(f"{to_state} is an owner decision; by={by!r} refused")
        conn.execute("UPDATE vr_candidate SET state=?, updated_at=? WHERE id=?",
                     (to_state, now_iso(), candidate_id))
        conn.execute(
            "INSERT INTO vr_decision (candidate_id,from_state,to_state,by,note,at,ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (candidate_id, cur, to_state, by, note[:300], now_iso(), now_ts()))
        conn.commit()
        return {"id": candidate_id, "state": to_state, "by": by}
    finally:
        if own:
            conn.close()


def get(candidate_id: str, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute("SELECT * FROM vr_candidate WHERE id=?",
                           (candidate_id,)).fetchone()
        if not row:
            raise RadarError(f"unknown candidate {candidate_id}")
        d = dict(row)
        d["card"] = json.loads(d["card"] or "{}")
        d["history"] = [dict(r) for r in conn.execute(
            "SELECT from_state AS \"from\", to_state AS \"to\", by, note, at "
            "FROM vr_decision WHERE candidate_id=? ORDER BY id", (candidate_id,))]
        return d
    finally:
        if own:
            conn.close()


def ranked(state: Optional[str] = None, limit: int = 50, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        if state:
            rows = conn.execute("SELECT * FROM vr_candidate WHERE state=? "
                                "ORDER BY score DESC, created_ts ASC LIMIT ?",
                                (state, limit))
        else:
            rows = conn.execute("SELECT * FROM vr_candidate "
                                "ORDER BY score DESC, created_ts ASC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["card"] = json.loads(d["card"] or "{}")
            out.append(d)
        return out
    finally:
        if own:
            conn.close()


# The owner's seed thesis, recorded once so the radar never starts empty.
SEED_TITLE = "GEO/AEO visibility product"
SEED_CARD = {
    "problem": ("AI assistants (ChatGPT, Perplexity, Gemini) recommend businesses "
                "to buyers, and most SMBs have no idea whether the AI recommends "
                "their competitors but not them."),
    "target_buyer": "SMB owners and marketing leads who already buy SEO services",
    "why_now": ("Buyer discovery is shifting from search pages to AI answers; "
                "the measurement tooling gap is still open."),
    "market_evidence": ("Existing SEO clients ask about AI visibility; early "
                        "outreach note: ZERO replies so far — treat that as a "
                        "funnel/deliverability problem to diagnose, not evidence "
                        "of no demand and not something to hide."),
    "differentiation": ("We already operate SEO tooling and client delivery; "
                        "visibility reports reuse the existing crawler/report "
                        "pipeline instead of starting from zero."),
    "synergy": "seo-backend delivery pipeline, existing client base, report engine",
    "mvp_scope": ("One report: for N buyer-intent prompts, which brands do the "
                  "major AI assistants name, and where does the client appear — "
                  "with a before/after tracking loop."),
    "monetization": "monthly per-domain subscription; audit as the entry offer",
    "acquisition": "existing SEO client base first, then cold outreach on the audit",
    "cost": 2,
    "difficulty": 2,
    "risks": ("assistant answers vary per session (measurement noise); platforms "
              "may rate-limit; the category may consolidate fast"),
    "validation_experiment": ("Run the audit for 5 existing clients, send each a "
                              "one-page report, count replies/meetings. Fix "
                              "deliverability before reading zero replies as a "
                              "verdict."),
    "kill_criteria": ("After deliverability is verified working, <2 of 20 "
                      "audited prospects respond across two outreach rounds."),
    "confidence": 0.5,
}


def seed_default(conn=None) -> dict:
    """Idempotent: records the owner's seed thesis once, at IDEA."""
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT id FROM vr_candidate WHERE title=?",
                           (SEED_TITLE,)).fetchone()
        if row:
            return {"id": row[0], "seeded": False, "note": "already present"}
        out = propose(MODE_RECOMBINATION, SEED_TITLE, SEED_CARD, conn=conn)
        out["seeded"] = True
        return out
    finally:
        if own:
            conn.close()
