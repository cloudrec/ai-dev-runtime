"""Business Analyzer / Competitor Builder core (task 202) — seven argued score
axes, forbidden-material refusal, owner-gated build states, and the pure
portfolio combinator. Records and ranks; never builds, spends, or contacts."""
import sqlite3

import pytest

from core import business_analyzer as ba


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ba.db"))
    yield c
    c.close()


def full_scores(v=3):
    return {a: {"score": v, "why": f"argued {a}"} for a in ba.AXES}


def card(**over):
    base = {
        "competitor": "acme-rank.example",
        "what_they_sell": "rank tracking SaaS",
        "our_angle": "bundle with delivery, undercut on reporting",
        "mvp_scope": "tracker + weekly report",
    }
    base.update(over)
    return base


# ── card and score validation ───────────────────────────────────────────────

def test_forbidden_material_is_refused_by_name(conn):
    for bad in ("source_code", "branding_assets", "customer_list"):
        with pytest.raises(ba.AnalyzerError, match="public behavior only"):
            ba.record("x", card(**{bad: "lifted"}), conn=conn)


def test_axes_are_closed_ranged_and_argued(conn):
    with pytest.raises(ba.AnalyzerError, match="unknown score axes"):
        ba.record("x", card(scores={"vibes": {"score": 5, "why": "w"}}), conn=conn)
    with pytest.raises(ba.AnalyzerError, match="out of range"):
        ba.record("x", card(scores={"cloneability": {"score": 9, "why": "w"}}),
                  conn=conn)
    with pytest.raises(ba.AnalyzerError, match="rationale"):
        ba.record("x", card(scores={"cloneability": {"score": 3}}), conn=conn)


def test_partial_scores_never_outrank_full_ones(conn):
    partial = {"profitability_potential": {"score": 5, "why": "w"}}
    assert ba.total_score(partial) < ba.total_score(full_scores(3))
    assert ba.total_score({}) == 0.0
    assert ba.total_score(full_scores(5)) == 100.0


def test_record_state_reflects_scoring_completeness(conn):
    draft = ba.record("d", card(), conn=conn)
    assert draft["state"] == ba.DRAFT
    scored = ba.record("s", card(scores=full_scores()), conn=conn)
    assert scored["state"] == ba.SCORED and scored["dispatched"] is False


# ── lifecycle: owner-gated build ────────────────────────────────────────────

def test_build_spend_states_need_the_owner(conn):
    cid = ba.record("t", card(), conn=conn)["id"]
    ba.rescore(cid, full_scores(4), conn=conn)
    ba.transition(cid, ba.PROPOSED, by="analyzer", conn=conn)
    with pytest.raises(ba.AnalyzerError, match="owner decision"):
        ba.transition(cid, ba.APPROVED, by="analyzer", conn=conn)
    ba.transition(cid, ba.APPROVED, by="owner", conn=conn)
    with pytest.raises(ba.AnalyzerError, match="owner decision"):
        ba.transition(cid, ba.BUILDING, by="adapter", conn=conn)
    ba.transition(cid, ba.BUILDING, by="owner", conn=conn)
    hist = ba.get(cid, conn=conn)["history"]
    assert [h["to"] for h in hist] == [
        ba.DRAFT, ba.SCORED, ba.PROPOSED, ba.APPROVED, ba.BUILDING]


def test_scores_frozen_after_decision_and_illegal_jumps_refused(conn):
    cid = ba.record("t", card(scores=full_scores()), conn=conn)["id"]
    with pytest.raises(ba.AnalyzerError, match="illegal transition"):
        ba.transition(cid, ba.BUILDING, by="owner", conn=conn)
    ba.transition(cid, ba.PROPOSED, by="analyzer", conn=conn)
    ba.transition(cid, ba.REJECTED, by="owner", conn=conn)
    with pytest.raises(ba.AnalyzerError, match="frozen"):
        ba.rescore(cid, full_scores(5), conn=conn)
    with pytest.raises(ba.AnalyzerError, match="illegal transition"):
        ba.transition(cid, ba.PROPOSED, by="analyzer", conn=conn)  # terminal


# ── portfolio combinator ────────────────────────────────────────────────────

def test_combine_is_pure_and_bounded():
    assets = [
        {"name": "seo-backend", "capability": "rank + crawl reports"},
        {"name": "payment-orchestrator", "capability": "invoicing"},
        {"name": "jobhunter", "capability": "outreach automation"},
    ]
    out = ba.combine(assets)
    titles = {c["title"] for c in out}
    assert "seo-backend + payment-orchestrator" in titles
    assert "seo-backend + payment-orchestrator + jobhunter" in titles
    assert len(out) == 3 + 1  # C(3,2) + C(3,3)
    assert all(set(c) == {"assets", "title", "thesis"} for c in out)
    # nameless entries are skipped, size bounded
    assert ba.combine([{"name": ""}, {"name": "a"}, {"name": "b"}],
                      max_size=2) == [
        {"assets": ["a", "b"], "title": "a + b",
         "thesis": "One offer combining a, b"}]
    with pytest.raises(ba.AnalyzerError):
        ba.combine(assets, max_size=1)
