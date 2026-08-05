"""Owner-facing status view — sourced from durable state, inventing nothing.

The view exists so the owner can see which agents are working / idle / blocked and WHY,
without reading ledgers. Two properties matter more than formatting:

  * every reason must be traceable to something another component durably wrote;
  * a reason must belong to THIS blocker. On its first live run the view labelled
    arbitrage2 "BLOCKED" citing an unrelated `unverified_owner_decision` gate from other
    work, because it attached any open gate for the agent. A wrong reason is worse than
    none.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core import owner_status as osx


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    cp = str(tmp_path / "cp.db")
    ac = str(tmp_path / "ac.db")
    monkeypatch.setenv("CONTROL_PLANE_DB", cp)
    monkeypatch.setenv("AGENT_CONTROL_DB", ac)
    monkeypatch.setattr(osx, "CP_DB", cp)
    monkeypatch.setattr(osx, "AC_DB", ac)
    conn = sqlite3.connect(cp)
    conn.execute("""CREATE TABLE governor_blocker (target TEXT, stage TEXT,
        fingerprint TEXT, first_seen TEXT, last_seen TEXT, fields TEXT)""")
    conn.execute("""CREATE TABLE owner_gate (id TEXT, work_item_id TEXT, agent_id TEXT,
        reason TEXT, kind TEXT, state TEXT, correlation_id TEXT, answer TEXT,
        opened_at TEXT, notified_at TEXT, answered_at TEXT)""")
    conn.commit()
    conn.close()
    yield cp


def _blocker(cp, target, stage, fields, gate_kind="owner_payload_missing",
             gate_state="open", corr=None):
    conn = sqlite3.connect(cp)
    conn.execute("INSERT INTO governor_blocker VALUES (?,?,?,?,?,?)",
                 (target, stage, "fp", "2026-08-05T10:00:00", "2026-08-05T10:30:00",
                  json.dumps(fields)))
    conn.execute("INSERT INTO owner_gate VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("g1", "", target, f"NEEDS_OWNER_PAYLOAD at {stage}", gate_kind,
                  gate_state, corr if corr is not None else f"gov:{target}:{stage}",
                  "", "2026-08-05T10:00:00", None, None))
    conn.commit()
    conn.close()


def test_blocked_agent_reports_its_exact_missing_fields(_isolated):
    _blocker(_isolated, "mess-qa-automation:0.0", "stage_06",
             ["folders: titles", "polls: row copy"])
    b = osx._why_blocked("mess-qa-automation:0.0")
    assert b["stage"] == "stage_06"
    assert b["missing_fields"] == ["folders: titles", "polls: row copy"]
    assert b["gate"]["kind"] == "owner_payload_missing"


def test_an_unrelated_open_gate_is_not_attached_to_a_blocker(_isolated):
    """The live bug: any open gate for the agent was treated as the blocker's reason."""
    _blocker(_isolated, "arbitrage2-opus:0.0", "-", [],
             gate_kind="unverified_owner_decision",
             corr="something:else:entirely")
    b = osx._why_blocked("arbitrage2-opus:0.0")
    assert b is not None
    assert b["gate"] is None, "a gate from other work must never explain this blocker"


def test_an_answered_gate_no_longer_blocks(_isolated):
    _blocker(_isolated, "x:0.0", "stage_1", ["a"], gate_state="answered")
    assert osx._why_blocked("x:0.0")["gate"] is None


def test_no_blocker_row_means_no_blocker(_isolated):
    assert osx._why_blocked("never-seen:0.0") is None


def test_unreadable_databases_yield_no_false_reasons(monkeypatch, tmp_path):
    """A missing/empty store must produce NO reason at all — never a guessed one.
    (sqlite happily creates an empty file, so the meaningful check is a missing TABLE.)"""
    missing = str(tmp_path / "missing.db")
    monkeypatch.setattr(osx, "CP_DB", missing)
    assert osx._why_blocked("anything:0.0") is None
    assert osx._rows(missing, "SELECT * FROM governor_blocker") == []


def test_render_states_the_reason_and_the_needs(_isolated, monkeypatch):
    st = {"generated_at": "2026-08-05T00:00:00",
          "agents": [{"target": "mess-qa-automation:0.0", "status": "blocked",
                      "why": "NEEDS_OWNER_PAYLOAD at stage_06",
                      "queue_pointer": "stage_06",
                      "missing_fields": ["folders: titles"]},
                     {"target": "cp-canary:0.0", "status": "working",
                      "why": "agent is executing"}],
          "open_owner_gates": [{"kind": "owner_payload_missing", "c": 2}]}
    out = osx.render(st)
    assert "BLOCKED" in out and "NEEDS_OWNER_PAYLOAD at stage_06" in out
    assert "needs: folders: titles" in out
    assert "queue: stage_06" in out
    assert "owner_payload_missing=2" in out


def test_render_never_proposes_work(_isolated):
    """The view reports state. It must not suggest a next step for any project."""
    st = {"generated_at": "t", "agents": [
        {"target": "a:0.0", "status": "idle", "why": "at rest; no durable blocker recorded"}],
        "open_owner_gates": []}
    out = osx.render(st).lower()
    for verb in ("you should", "next step:", "recommend", "todo", "try "):
        assert verb not in out, verb
