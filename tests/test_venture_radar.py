"""Venture Radar core (task 193) — closed card vocabulary, fail-closed
lifecycle with an owner-only decision gate, durable decision ledger, and the
owner's seed thesis. The radar records and ranks; it never builds."""
import sqlite3

import pytest

from core import venture_radar as vr


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "vr.db"))
    yield c
    c.close()


def card(**over):
    base = {
        "problem": "SMBs cannot see their AI-assistant visibility",
        "target_buyer": "SMB marketing leads",
        "mvp_scope": "one visibility report per domain",
        "validation_experiment": "audit 5 existing clients, count replies",
        "kill_criteria": "<2 of 20 respond after deliverability verified",
        "confidence": 0.5, "cost": 2, "difficulty": 2,
    }
    base.update(over)
    return base


# ── card vocabulary ─────────────────────────────────────────────────────────

def test_card_vocabulary_is_closed(conn):
    with pytest.raises(vr.RadarError, match="unknown card fields"):
        vr.propose(vr.MODE_FRESH_NICHE, "x", card(dispatch_now=True), conn=conn)
    with pytest.raises(vr.RadarError, match="required card fields"):
        vr.propose(vr.MODE_FRESH_NICHE, "x", {"problem": "p"}, conn=conn)
    # every declared field is accepted
    full = card(**{f: card().get(f, "text") for f in ()})
    for f in vr.CARD_FIELDS:
        full.setdefault(f, 1 if f in ("cost", "difficulty", "confidence") else "text")
    assert vr.propose(vr.MODE_RECOMBINATION, "full", full, conn=conn)["dispatched"] is False


def test_unknown_mode_and_decided_start_state_refused(conn):
    with pytest.raises(vr.RadarError, match="unknown mode"):
        vr.propose("moonshot", "x", card(), conn=conn)
    with pytest.raises(vr.RadarError, match="cannot start"):
        vr.propose(vr.MODE_FRESH_NICHE, "x", card(), state=vr.APPROVED, conn=conn)


# ── lifecycle: fail-closed, owner-gated ─────────────────────────────────────

def test_lifecycle_owner_gate_and_ledger(conn):
    cid = vr.propose(vr.MODE_RECOMBINATION, "t", card(), conn=conn)["id"]
    vr.transition(cid, vr.RESEARCHED, by="radar", conn=conn)
    vr.transition(cid, vr.PROPOSED, by="radar", conn=conn)
    # the radar may NOT approve its own idea
    with pytest.raises(vr.RadarError, match="owner decision"):
        vr.transition(cid, vr.APPROVED, by="radar", conn=conn)
    vr.transition(cid, vr.APPROVED, by="owner", note="go", conn=conn)
    with pytest.raises(vr.RadarError, match="owner decision"):
        vr.transition(cid, vr.BUILDING, by="adapter", conn=conn)
    vr.transition(cid, vr.BUILDING, by="owner", conn=conn)
    hist = vr.get(cid, conn=conn)["history"]
    assert [h["to"] for h in hist] == [
        vr.IDEA, vr.RESEARCHED, vr.PROPOSED, vr.APPROVED, vr.BUILDING]
    assert hist[3]["by"] == "owner" and hist[3]["note"] == "go"


def test_illegal_jumps_refused(conn):
    cid = vr.propose(vr.MODE_FRESH_NICHE, "t", card(), conn=conn)["id"]
    with pytest.raises(vr.RadarError, match="illegal transition"):
        vr.transition(cid, vr.APPROVED, by="owner", conn=conn)  # IDEA -> APPROVED
    with pytest.raises(vr.RadarError, match="illegal transition"):
        vr.transition(cid, vr.BUILDING, by="owner", conn=conn)
    # REJECTED is terminal
    vr.transition(cid, vr.RESEARCHED, by="radar", conn=conn)
    vr.transition(cid, vr.PROPOSED, by="radar", conn=conn)
    vr.transition(cid, vr.REJECTED, by="owner", conn=conn)
    with pytest.raises(vr.RadarError, match="illegal transition"):
        vr.transition(cid, vr.PROPOSED, by="radar", conn=conn)


def test_card_frozen_after_owner_decision(conn):
    cid = vr.propose(vr.MODE_FRESH_NICHE, "t", card(), conn=conn)["id"]
    vr.update_card(cid, card(confidence=0.8), conn=conn)  # research may refine
    vr.transition(cid, vr.RESEARCHED, by="radar", conn=conn)
    vr.transition(cid, vr.PROPOSED, by="radar", conn=conn)
    vr.transition(cid, vr.APPROVED, by="owner", conn=conn)
    with pytest.raises(vr.RadarError, match="frozen"):
        vr.update_card(cid, card(confidence=0.1), conn=conn)


# ── scoring: blunt, explainable order ───────────────────────────────────────

def test_score_orders_by_confidence_over_cost_and_difficulty(conn):
    hi = vr.propose(vr.MODE_FRESH_NICHE, "hi",
                    card(confidence=0.8, cost=1, difficulty=1), conn=conn)
    lo = vr.propose(vr.MODE_FRESH_NICHE, "lo",
                    card(confidence=0.2, cost=4, difficulty=4), conn=conn)
    assert hi["score"] > lo["score"]
    order = [c["id"] for c in vr.ranked(conn=conn)]
    assert order.index(hi["id"]) < order.index(lo["id"])
    # a zeroed cost cannot mint an infinite score
    z = vr.score_card(card(confidence=1.0, cost=0, difficulty=0))
    assert z == vr.score_card(card(confidence=1.0, cost=0.5, difficulty=0.5))


# ── the owner's seed thesis ─────────────────────────────────────────────────

def test_seed_is_idempotent_and_keeps_the_funnel_diagnosis(conn):
    first = vr.seed_default(conn=conn)
    assert first["seeded"] is True
    again = vr.seed_default(conn=conn)
    assert again["seeded"] is False and again["id"] == first["id"]
    c = vr.get(first["id"], conn=conn)
    assert c["state"] == vr.IDEA and c["mode"] == vr.MODE_RECOMBINATION
    # the zero-replies signal is preserved as a PROBLEM TO DIAGNOSE, not hidden
    text = (c["card"]["market_evidence"] + c["card"]["validation_experiment"]).lower()
    assert "deliverability" in text and "not" in c["card"]["market_evidence"].lower()
    assert "recommend" in c["card"]["problem"].lower()  # the owner's thesis
