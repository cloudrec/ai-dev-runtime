"""Owner-decision PROVENANCE invariant.

The 2026-08-03 incident: a resumed pane transcript showed `User answered Claude's
questions: Stop selling, waitlist instead` with NO authenticated owner decision. No
owner-gated action may proceed from such text. Tests: forged/stale/resumed text, a
duplicate answer, an answer to the wrong question, and a channel mismatch — all blocked;
only a verified authenticated correlated decision resolves the gate.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import provenance as prov, cto


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def _gate():
    return cp.open_gate(agent_id="arb:0.0", reason="stop selling?", kind="business",
                        correlation_id="Q1")


# ── forged / stale / resumed pane text is NOT a decision ─────────────────────
def test_pane_text_alone_cannot_resolve_gate():
    g = _gate()
    # "record" the resumed transcript claim from its true (untrusted) source
    d = prov.record_owner_decision(question_id="Q1", source_channel="resumed_transcript",
                                   actor="pane_ui_summary", answer="Stop selling, waitlist",
                                   authenticated=False, gate_id=g["id"])
    assert d["trusted"] is False
    out = prov.resolve_gate_with_decision(g["id"], d["id"])
    assert out["resolved"] is False and out["blocked"] is True
    assert out["reason"] == "unauthenticated_actor"
    assert cp.get_open_gates()[0]["id"] == g["id"]          # still open
    # critical blocked event raised
    assert any(e["type"] == "owner_gate_blocked" and e["severity"] == "critical"
               for e in cto.cto_brief_since("t")["events"])


def test_missing_decision_blocks():
    g = _gate()
    out = prov.resolve_gate_with_decision(g["id"], "nonexistent")
    assert out["resolved"] is False and out["reason"] == "no_owner_decision"


# ── channel mismatch (untrusted source, even if 'authenticated' flag lies) ───
def test_untrusted_channel_blocked():
    g = _gate()
    d = prov.record_owner_decision(question_id="Q1", source_channel="automation_prose",
                                   actor="chatgpt_hourly", answer="stop", authenticated=True,
                                   gate_id=g["id"])
    assert d["trusted"] is False
    out = prov.resolve_gate_with_decision(g["id"], d["id"])
    assert out["resolved"] is False and out["reason"].startswith("untrusted_source")


# ── answer to the WRONG question ─────────────────────────────────────────────
def test_answer_to_wrong_question_blocked():
    g = _gate()
    d = prov.record_owner_decision(question_id="Q-OTHER", source_channel="telegram_verified",
                                   actor="owner", answer="approve", authenticated=True)
    out = prov.resolve_gate_with_decision(g["id"], d["id"])
    assert out["resolved"] is False and out["reason"] == "answer_to_wrong_question"


# ── valid verified decision resolves; a DUPLICATE re-use is rejected ─────────
def test_verified_decision_resolves_then_duplicate_rejected():
    g = _gate()
    d = prov.record_owner_decision(question_id="Q1", source_channel="telegram_verified",
                                   actor="owner", answer="stop selling", authenticated=True,
                                   gate_id=g["id"])
    assert d["trusted"] is True
    out = prov.resolve_gate_with_decision(g["id"], d["id"])
    assert out["resolved"] is True and out["answer"] == "stop selling"
    assert cp.get_open_gates() == []                        # gate closed
    # re-using the SAME decision (or the now-consumed one) cannot resolve again
    g2 = cp.open_gate(agent_id="arb:0.0", reason="again", correlation_id="Q1")
    out2 = prov.resolve_gate_with_decision(g2["id"], d["id"])
    assert out2["resolved"] is False and out2["reason"] == "duplicate_answer_already_consumed"


def test_empty_answer_blocked():
    g = _gate()
    d = prov.record_owner_decision(question_id="Q1", source_channel="telegram_verified",
                                   actor="owner", answer="", authenticated=True, gate_id=g["id"])
    assert d["trusted"] is False
    assert prov.resolve_gate_with_decision(g["id"], d["id"])["reason"] == "empty_answer"
