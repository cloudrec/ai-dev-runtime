"""Business Analyzer / Competitor Builder core (task 202).

Canonical Owner OS 2.0 core: seo-backend and any UI consume this over the
/api/v1 adapter contract; nothing here dispatches work, spends money, calls
the network, or contacts anyone. Recording and ranking an opportunity is safe;
build/spend/publish/outreach are OWNER decisions, enforced by the state
machine below.

Two products:

1. Competitor opportunity cards — "this business exists and makes money; could
   we build a better one?" Scored on SEVEN fixed axes (0..5 each, with a
   written rationale per axis, so a score can always be argued with):
   profitability_potential, cloneability, improvement_leverage,
   speed_to_market, capital_intensity (inverted: 5 = cheap), competition_risk
   (inverted: 5 = low risk), strategic_fit.

2. Portfolio combinator — mechanical proposals for combining 2-3 EXISTING
   assets into one offer. Pure function over the asset list it is given.

Hard rule, owner-stated: never copy proprietary code, branding, or
confidential data. Cards describe what a competitor DOES (public behavior),
never how their code works. `validate_card` refuses fields that would carry
lifted material.
"""
from __future__ import annotations

import json
import uuid
from itertools import combinations
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

DRAFT, SCORED, PROPOSED = "DRAFT", "SCORED", "PROPOSED"
APPROVED, REJECTED, BUILDING = "APPROVED", "REJECTED", "BUILDING"

TRANSITIONS = {
    DRAFT: {SCORED},
    SCORED: {PROPOSED},
    PROPOSED: {APPROVED, REJECTED},
    APPROVED: {BUILDING, REJECTED},
    REJECTED: set(),
    BUILDING: set(),
}
# build/spend/publish/outreach all live behind these — owner only
OWNER_ONLY_STATES = {APPROVED, REJECTED, BUILDING}

AXES = (
    "profitability_potential",
    "cloneability",            # how much of the VALUE is reproducible cleanly
    "improvement_leverage",    # how much better we can credibly be
    "speed_to_market",
    "capital_intensity",       # inverted: 5 = cheap to enter
    "competition_risk",        # inverted: 5 = low risk
    "strategic_fit",
)

CARD_FIELDS = (
    "competitor",         # who we are studying (name/domain, public identity)
    "what_they_sell",     # observable offer
    "observed_pricing",   # public pricing evidence
    "evidence",           # where the demand/revenue signal comes from
    "our_angle",          # what we would do differently/better
    "assets_reused",      # which of OUR existing assets this leans on
    "mvp_scope",
    "risks",
    "scores",             # {axis: {"score": 0..5, "why": str}} for all 7 axes
)
REQUIRED_FIELDS = ("competitor", "what_they_sell", "our_angle", "mvp_scope")

# fields that would smuggle in lifted material — refused by name
_FORBIDDEN_FIELDS = ("source_code", "their_code", "branding_assets", "logo",
                     "customer_list", "confidential", "internal_docs", "scraped_db")


class AnalyzerError(Exception):
    """Refusal with an exact reason — mapped to HTTP 409 by the API layer."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ba_card (
    id TEXT PRIMARY KEY,
    title TEXT, state TEXT, card TEXT, score REAL,
    created_at TEXT, created_ts REAL, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ba_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT, from_state TEXT, to_state TEXT,
    by TEXT, note TEXT, at TEXT, ts REAL
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def validate_card(card: dict) -> dict:
    if not isinstance(card, dict):
        raise AnalyzerError("card must be an object")
    banned = sorted(set(k.lower() for k in card) & set(_FORBIDDEN_FIELDS))
    if banned:
        raise AnalyzerError(
            f"forbidden fields {banned}: cards describe public behavior only — "
            "never proprietary code, branding, or confidential data")
    unknown = sorted(set(card) - set(CARD_FIELDS))
    if unknown:
        raise AnalyzerError(f"unknown card fields: {unknown} — the vocabulary is closed")
    missing = [f for f in REQUIRED_FIELDS if not str(card.get(f, "")).strip()]
    if missing:
        raise AnalyzerError(f"required card fields missing/empty: {missing}")
    scores = card.get("scores")
    if scores is not None:
        _validate_scores(scores)
    return card


def _validate_scores(scores: dict) -> dict:
    if not isinstance(scores, dict):
        raise AnalyzerError("scores must be an object of {axis: {score, why}}")
    unknown = sorted(set(scores) - set(AXES))
    if unknown:
        raise AnalyzerError(f"unknown score axes: {unknown}; valid: {list(AXES)}")
    for axis, cell in scores.items():
        if not isinstance(cell, dict) or "score" not in cell:
            raise AnalyzerError(f"axis {axis!r} needs {{score, why}}")
        v = float(cell["score"])
        if not 0 <= v <= 5:
            raise AnalyzerError(f"axis {axis!r} score {v} out of range 0..5")
        if not str(cell.get("why", "")).strip():
            raise AnalyzerError(f"axis {axis!r} has a score without a rationale — "
                                "unarguable numbers are refused")
    return scores


def total_score(scores: dict) -> float:
    """Mean of the present axes, scaled to 0..100. Missing axes count as 0 so
    an unscored card can never outrank a fully argued one."""
    if not scores:
        return 0.0
    s = sum(float(scores[a]["score"]) for a in scores)
    return round(100.0 * s / (5 * len(AXES)), 4)


def record(title: str, card: dict, conn=None) -> dict:
    """Record a competitor opportunity card and STOP."""
    if not (title or "").strip():
        raise AnalyzerError("title required")
    validate_card(card)
    conn, own = _conn(conn)
    try:
        cid = uuid.uuid4().hex[:12]
        sc = total_score(card.get("scores") or {})
        state = SCORED if all(a in (card.get("scores") or {}) for a in AXES) else DRAFT
        conn.execute(
            "INSERT INTO ba_card (id,title,state,card,score,created_at,created_ts,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (cid, title[:200], state, json.dumps(card, ensure_ascii=False), sc,
             now_iso(), now_ts(), now_iso()))
        conn.execute(
            "INSERT INTO ba_decision (card_id,from_state,to_state,by,note,at,ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, "", state, "analyzer", "recorded", now_iso(), now_ts()))
        conn.commit()
        return {"id": cid, "title": title, "state": state, "score": sc,
                "dispatched": False,
                "note": "recorded only — build/spend/publish/outreach need the owner"}
    finally:
        if own:
            conn.close()


def rescore(card_id: str, scores: dict, conn=None) -> dict:
    """Attach/replace the 7-axis scores (pre-decision states only)."""
    _validate_scores(scores)
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT state, card FROM ba_card WHERE id=?",
                           (card_id,)).fetchone()
        if not row:
            raise AnalyzerError(f"unknown card {card_id}")
        if row[0] in OWNER_ONLY_STATES:
            raise AnalyzerError(f"card is frozen in {row[0]}")
        card = json.loads(row[1] or "{}")
        card["scores"] = scores
        sc = total_score(scores)
        state = SCORED if all(a in scores for a in AXES) else row[0]
        conn.execute("UPDATE ba_card SET card=?, score=?, state=?, updated_at=? "
                     "WHERE id=?", (json.dumps(card, ensure_ascii=False), sc, state,
                                    now_iso(), card_id))
        if state != row[0]:
            conn.execute(
                "INSERT INTO ba_decision (card_id,from_state,to_state,by,note,at,ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (card_id, row[0], state, "analyzer", "all axes argued",
                 now_iso(), now_ts()))
        conn.commit()
        return {"id": card_id, "score": sc, "state": state}
    finally:
        if own:
            conn.close()


def transition(card_id: str, to_state: str, *, by: str, note: str = "",
               conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        row = conn.execute("SELECT state FROM ba_card WHERE id=?",
                           (card_id,)).fetchone()
        if not row:
            raise AnalyzerError(f"unknown card {card_id}")
        cur = row[0]
        if to_state not in TRANSITIONS.get(cur, set()):
            raise AnalyzerError(f"illegal transition {cur} -> {to_state}")
        if to_state in OWNER_ONLY_STATES and by != "owner":
            raise AnalyzerError(f"{to_state} is an owner decision; by={by!r} refused")
        conn.execute("UPDATE ba_card SET state=?, updated_at=? WHERE id=?",
                     (to_state, now_iso(), card_id))
        conn.execute(
            "INSERT INTO ba_decision (card_id,from_state,to_state,by,note,at,ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (card_id, cur, to_state, by, note[:300], now_iso(), now_ts()))
        conn.commit()
        return {"id": card_id, "state": to_state, "by": by}
    finally:
        if own:
            conn.close()


def get(card_id: str, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute("SELECT * FROM ba_card WHERE id=?", (card_id,)).fetchone()
        if not row:
            raise AnalyzerError(f"unknown card {card_id}")
        d = dict(row)
        d["card"] = json.loads(d["card"] or "{}")
        d["history"] = [dict(r) for r in conn.execute(
            "SELECT from_state AS \"from\", to_state AS \"to\", by, note, at "
            "FROM ba_decision WHERE card_id=? ORDER BY id", (card_id,))]
        return d
    finally:
        if own:
            conn.close()


def ranked(state: Optional[str] = None, limit: int = 50, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        if state:
            rows = conn.execute("SELECT * FROM ba_card WHERE state=? "
                                "ORDER BY score DESC, created_ts ASC LIMIT ?",
                                (state, limit))
        else:
            rows = conn.execute("SELECT * FROM ba_card "
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


def combine(assets: list, *, max_size: int = 3) -> list:
    """Portfolio combinator: mechanical 2..max_size combinations of EXISTING
    assets, each returned as a draft one-liner for a human (or a later scoring
    pass) to judge. Pure function — no storage, no dispatch, no network.

    `assets` is a list of {"name": str, "capability": str} dicts describing
    what each asset can already do."""
    if max_size < 2:
        raise AnalyzerError("combinations need at least 2 assets")
    named = [a for a in assets
             if isinstance(a, dict) and str(a.get("name", "")).strip()]
    out = []
    for size in (2, 3):
        if size > max_size:
            break
        for combo in combinations(named, size):
            names = [c["name"] for c in combo]
            caps = "; ".join(str(c.get("capability", "")).strip()
                             for c in combo if c.get("capability"))
            out.append({
                "assets": names,
                "title": " + ".join(names),
                "thesis": f"One offer combining: {caps}" if caps
                          else f"One offer combining {', '.join(names)}",
            })
    return out
