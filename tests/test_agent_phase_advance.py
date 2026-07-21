"""Cross-phase auto-progress — every guard, idempotency, dispatch+verify."""
from __future__ import annotations

import pytest

from core import agent_phase_advance as pa
from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "pa.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))


CFG = {"mode": "auto", "advance_phases": True, "root": "/opt/seo", "project": "seo",
       "phases": [
           {"id": "stage-4", "title": "s4", "acceptance": {"must_not_contain": ["FAILED"]}},
           {"id": "stage-5", "title": "s5", "approved_task_text": "Do stage 5 (owner-approved exact text)"},
       ]}


def _rec(state="completed", phase="stage-4", blocker=None, report="reports/DONE.md",
         session="seo-audit", key="seo-audit:0.0"):
    return {"session": session, "agent_key": key, "state": state, "phase": phase,
            "blocker_category": blocker, "report_path": report, "project": "seo"}


def _mock_report(monkeypatch, text=""):
    monkeypatch.setattr(ac, "agent_report_read", lambda root, path: {"content": text})


# ── guards ──────────────────────────────────────────────────────────────────
def test_disabled_project_never_advances(monkeypatch):
    _mock_report(monkeypatch)
    cfg = {**CFG, "advance_phases": False}
    assert pa.advance_if_ready("seo-audit", cfg, _rec(), budget_locked=False)["action"] == "none"


def test_hold_session_never_advances(monkeypatch):
    _mock_report(monkeypatch)
    cfg = {**CFG, "mode": "hold"}
    r = pa.advance_if_ready("safeguard", cfg, _rec(session="safeguard"), budget_locked=False)
    assert r["action"] == "none" and "hold" in r["reason"]


def test_not_completed_never_advances(monkeypatch):
    _mock_report(monkeypatch)
    assert pa.advance_if_ready("seo-audit", CFG, _rec(state="working"), budget_locked=False)["action"] == "none"


@pytest.mark.parametrize("blocker", ["external", "credential", "owner", "denied"])
def test_blocked_categories_never_advance(monkeypatch, blocker):
    _mock_report(monkeypatch)
    r = pa.advance_if_ready("seo-audit", CFG, _rec(blocker=blocker), budget_locked=False)
    assert r["action"] == "none"


def test_budget_gate_blocks(monkeypatch):
    _mock_report(monkeypatch)
    r = pa.advance_if_ready("seo-audit", CFG, _rec(), budget_locked=True)
    assert r["action"] == "none" and "budget" in r["reason"]


def test_no_approved_text_never_invents(monkeypatch):
    _mock_report(monkeypatch)
    # completed phase is stage-5 (last, no next) OR next has no text
    cfg = {**CFG, "phases": [{"id": "stage-4", "title": "s4"}, {"id": "stage-5", "title": "s5"}]}
    r = pa.advance_if_ready("seo-audit", cfg, _rec(phase="stage-4"), budget_locked=False)
    assert r["action"] == "no_recorded_next_phase"


def test_acceptance_failure_blocks_advance(monkeypatch):
    _mock_report(monkeypatch, text="Report ... FAILED acceptance")
    r = pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), budget_locked=False)
    assert r["action"] == "acceptance_failed"


# ── dispatch + verify + idempotency ─────────────────────────────────────────
def test_happy_path_dispatches_once_and_verifies_working(monkeypatch):
    _mock_report(monkeypatch, text="all good, duplicate_created=false")
    sent = {"n": 0}
    monkeypatch.setattr(ac, "agent_send",
                        lambda target, text, idempotency_key=None: sent.__setitem__("n", sent["n"] + 1)
                        or {"delivered": True})
    monkeypatch.setattr(ac, "agent_status", lambda target: {"state": "working"})
    r = pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), budget_locked=False, _sleep=lambda s: None)
    assert r["action"] == "advanced" and r["verified_working"] is True
    assert sent["n"] == 1 and r["next_phase"] == "stage-5"
    # idempotent: a second run does not re-dispatch
    r2 = pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), budget_locked=False, _sleep=lambda s: None)
    assert r2["action"] == "already_dispatched" and sent["n"] == 1


def test_dispatch_but_no_working_flags_escalation(monkeypatch):
    _mock_report(monkeypatch, text="ok")
    monkeypatch.setattr(ac, "agent_send", lambda target, text, idempotency_key=None: {"delivered": True})
    monkeypatch.setattr(ac, "agent_status", lambda target: {"state": "idle"})   # never enters working
    monkeypatch.setenv("PHASE_ADVANCE_VERIFY_SECS", "1")
    import importlib
    importlib.reload(pa)
    r = pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), budget_locked=False, _sleep=lambda s: None)
    assert r["action"] == "advanced" and r["verified_working"] is False and r["escalate"] is True
    importlib.reload(pa)


def test_dry_run_would_dispatch_without_sending(monkeypatch):
    _mock_report(monkeypatch, text="ok")
    calls = {"n": 0}
    monkeypatch.setattr(ac, "agent_send", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    r = pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), dispatch=False, budget_locked=False)
    assert r["action"] == "would_dispatch" and calls["n"] == 0


# ── rollback ────────────────────────────────────────────────────────────────
def test_rollback_marks_not_dispatched(monkeypatch):
    _mock_report(monkeypatch, text="ok")
    monkeypatch.setattr(ac, "agent_send", lambda target, text, idempotency_key=None: {"delivered": True})
    monkeypatch.setattr(ac, "agent_status", lambda target: {"state": "working"})
    pa.advance_if_ready("seo-audit", CFG, _rec(phase="stage-4"), budget_locked=False, _sleep=lambda s: None)
    out = pa.rollback("seo-audit", "stage-5")
    assert out["rolled_back"] is True
    assert pa.get_advance("seo-audit", "stage-5")["status"] == "rolled_back"


# ── sweep bounded + gated ───────────────────────────────────────────────────
def test_sweep_disabled_by_env(monkeypatch):
    monkeypatch.setenv("AGENT_PHASE_ADVANCE_ENABLED", "0")
    import importlib
    importlib.reload(pa)
    assert pa.sweep([_rec()], lambda s: CFG)["enabled"] is False
    importlib.reload(pa)
