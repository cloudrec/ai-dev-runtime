"""Autonomous Agent Orchestrator — state derivation, persistence, policy."""
from __future__ import annotations

import time

import pytest

from core import agent_orchestrator as orch


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "orch.db"))
    monkeypatch.delenv("ORCH_BUDGET_LOCKED", raising=False)
    monkeypatch.setenv("ORCH_BUDGET_LOCK_FILE", str(tmp_path / "nolock"))
    orch._config_cache = {"allowed_roots": ["/opt", "/root/ai-dev-runtime", "/root/safeguard-remote-agent"]}


def _agent(target="seo-audit:0.0", state="working", cwd="/opt/seo", tail=""):
    return {"target": target, "session": target.split(":")[0], "is_agent": True, "alive": True,
            "state": state, "claude_cwd": cwd, "cwd": cwd, "recent_activity": tail, "_tail": tail}


AUTO = {"mode": "auto", "project": "seo", "root": "/opt/seo", "approved_goal": "SEO Stage 4"}
HOLD_SG = {"mode": "hold", "project": "safeguard", "root": "/root/safeguard-remote-agent",
           "hold_state": "externally_blocked"}

SAFE_PROMPT = ("  Bash command\n  docker compose run --rm backend pytest -q\n"
               "  Run the tests\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No")
BRANCH_PROMPT = ("  Bash command\n  git checkout -b feat/seo-stage-4\n"
                 "  Create the feature branch\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No")
DANGEROUS_PROMPT = ("  Bash command\n  docker compose restart backend\n"
                    "  Restart\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No")


# ── state derivation (9 states) ─────────────────────────────────────────────
def test_working_state():
    assert orch.derive(_agent(state="working"), AUTO)["state"] == "working"


def test_idle_state(monkeypatch):
    monkeypatch.setattr(orch, "_completion_evidence", lambda *a, **k: None)
    assert orch.derive(_agent(state="idle"), AUTO)["state"] == "idle"


def test_dead_maps_to_failed():
    assert orch.derive(_agent(state="dead"), AUTO)["state"] == "failed"
    assert orch.derive(_agent(state="stale"), AUTO)["state"] == "failed"


def test_externally_blocked():
    d = orch.derive(_agent(state="externally_blocked"), HOLD_SG)
    assert d["state"] == "externally_blocked" and d["blocker_category"] == "external"


def test_parked_mode():
    assert orch.derive(_agent(state="working"), {"mode": "parked"})["state"] == "parked"


def test_paused_by_budget(monkeypatch):
    monkeypatch.setenv("ORCH_BUDGET_LOCKED", "1")
    assert orch.derive(_agent(state="working"), AUTO)["state"] == "paused_by_budget"


def test_completed_with_report_evidence(monkeypatch):
    monkeypatch.setattr(orch, "_completion_evidence",
                        lambda *a, **k: {"report_path": "reports/DONE.md", "modified_at": "2026-07-20T08:25:00+00:00"})
    d = orch.derive(_agent(state="idle"), AUTO)
    assert d["state"] == "completed" and d["report_path"] == "reports/DONE.md"


# ── waiting → safe_approval vs owner ────────────────────────────────────────
def test_safe_prompt_auto_becomes_waiting_safe_approval():
    d = orch.derive(_agent(state="waiting_owner", tail=SAFE_PROMPT), AUTO)
    assert d["state"] == "waiting_safe_approval"
    assert d["command"] == "docker compose run --rm backend pytest -q"


def test_branch_creation_prompt_is_safe():
    d = orch.derive(_agent(state="waiting_owner", tail=BRANCH_PROMPT), AUTO)
    assert d["state"] == "waiting_safe_approval"
    assert d["command"] == "git checkout -b feat/seo-stage-4"


def test_dangerous_prompt_stays_waiting_owner_with_decision():
    d = orch.derive(_agent(state="waiting_owner", tail=DANGEROUS_PROMPT), AUTO)
    assert d["state"] == "waiting_owner"
    assert d["decision"]["action"] == "docker compose restart backend"
    assert d["decision"]["reply_choices"]
    assert d["blocker_category"] == "denied"


def test_hold_session_safe_prompt_is_not_auto_resolved():
    # A hold session (Safe Guard) with even a safe prompt is NOT waiting_safe_approval.
    d = orch.derive(_agent(target="safeguard:0.0", state="waiting_owner", tail=SAFE_PROMPT), HOLD_SG)
    assert d["state"] == "waiting_owner"       # monitored, left for owner


def test_cwd_outside_project_is_not_auto_safe():
    d = orch.derive(_agent(state="waiting_owner", cwd="/tmp/rogue", tail=SAFE_PROMPT), AUTO)
    assert d["state"] == "waiting_owner"       # context validation failed


# ── review ladder ───────────────────────────────────────────────────────────
def test_review_ladder_local_tier_records_cost_zero():
    v = orch.review_command("git status", "/opt/seo", ["/opt/seo"])
    assert v["tier"] == "local_policy" and v["safe"] is True and v["cost_usd"] == 0.0


def test_review_ladder_model_tier_never_upgrades_unsafe(monkeypatch):
    monkeypatch.setenv("ORCH_REVIEW_MODEL_ENABLED", "1")
    v = orch.review_command("docker compose restart backend", "/opt/seo", ["/opt/seo"])
    assert v["safe"] is False       # model advisory can never make it safe


# ── persistence ─────────────────────────────────────────────────────────────
def test_record_roundtrip_survives():
    orch._upsert({"agent_key": "x:0", "session": "x", "project": "p", "approved_goal": "g",
                  "current_task": "t", "phase": "stage-4", "state": "working",
                  "last_fresh_activity_ts": time.time(), "prompt_hash": "h", "blocker_category": None,
                  "completion_evidence": None, "report_path": None, "approved_next_task": "next",
                  "notification_state": None, "retry_count": 2, "decision": {"a": 1}})
    r = orch.get_record("x:0")
    assert r["project"] == "p" and r["phase"] == "stage-4" and r["retry_count"] == 2
    assert r["decision"] == {"a": 1}


# ── refresh_and_resolve: existing agents only, hold respected ───────────────
def test_refresh_resolves_auto_safe_and_holds_others(monkeypatch):
    agents = [
        _agent("seo-audit:0.0", state="waiting_owner", cwd="/opt/seo", tail=SAFE_PROMPT),
        _agent("safeguard:0.0", state="externally_blocked", cwd="/root/safeguard-remote-agent"),
    ]
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": agents})
    monkeypatch.setattr(orch.ac, "_pane_tail", lambda k, n=40: dict((a["target"], a["_tail"]) for a in agents).get(k, ""))
    monkeypatch.setattr(orch, "_session_cfg", lambda s: AUTO if s == "seo-audit" else HOLD_SG)
    monkeypatch.setattr(orch, "_completion_evidence", lambda *a, **k: None)
    resolved_calls = {"n": 0}

    def fake_resolve(target, approve=True):
        resolved_calls["n"] += 1
        return {"action": "approved", "resumed": True, "latency_s": 1.2}
    import core.agent_supervisor as supmod
    monkeypatch.setattr(supmod, "resolve_target", fake_resolve)

    res = orch.refresh_and_resolve(approve=True)
    assert res["ok"] is True and res["agents"] == 2
    assert len(res["resolved"]) == 1                    # only seo-audit safe prompt
    assert resolved_calls["n"] == 1
    # safeguard recorded externally_blocked, never resolved
    sg = orch.get_record("safeguard:0.0")
    assert sg["state"] == "externally_blocked"
    seo = orch.get_record("seo-audit:0.0")
    assert seo["notification_state"] == "auto_continued"


def test_refresh_creates_no_agents(monkeypatch):
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": []})
    res = orch.refresh_and_resolve(approve=True)
    assert res["agents"] == 0 and res["resolved"] == []


# ── V2 supervision: completed phases must not sit unattended ────────────────
def test_describe_next_phase_is_honest():
    assert "awaiting owner-approved text" in orch._describe_next_phase({"id": "p2", "title": "P2"})
    assert "ready to auto-advance" in orch._describe_next_phase(
        {"id": "p2", "title": "P2", "approved_task_text": "do it"})
    assert orch._describe_next_phase(None) is None


def test_supervise_completed_without_next_text_escalates_once():
    cfg = {"mode": "auto", "advance_phases": True, "project": "seo",
           "phases": [{"id": "s4", "title": "s4"}, {"id": "s5", "title": "s5"}]}
    rec = {"agent_key": "seo-audit:0.0", "project": "seo", "report_path": "reports/DONE.md"}
    nxt = orch._next_phase(cfg)
    sup = orch._supervise_completed("seo-audit", cfg, rec, {}, nxt)
    assert sup["notification_state"] == "phase_complete_needs_owner"
    assert sup["escalation"]["decision"]["reply_choices"]
    # already asked (same report) → no re-escalation
    sup2 = orch._supervise_completed("seo-audit", cfg, rec,
                                     {"notification_state": "phase_complete_needs_owner",
                                      "report_path": "reports/DONE.md"}, nxt)
    assert "escalation" not in sup2


def test_supervise_completed_with_next_text_is_advancing():
    cfg = {"mode": "auto", "advance_phases": True, "project": "seo",
           "phases": [{"id": "s4", "title": "s4"},
                      {"id": "s5", "title": "s5", "approved_task_text": "do s5"}]}
    rec = {"agent_key": "seo-audit:0.0", "project": "seo", "report_path": "r"}
    sup = orch._supervise_completed("seo-audit", cfg, rec, {}, orch._next_phase(cfg))
    assert sup["notification_state"] == "advancing" and "escalation" not in sup


def test_supervise_final_phase_no_escalation():
    cfg = {"mode": "auto", "advance_phases": True, "phases": [{"id": "only", "title": "only"}]}
    sup = orch._supervise_completed("x", cfg, {"agent_key": "x:0"}, {}, orch._next_phase(cfg))
    assert sup["notification_state"] == "phase_complete_final" and "escalation" not in sup
