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


# ── V3: owner-submitted exact next-phase text ───────────────────────────────
def test_set_phase_text_records_and_merges(monkeypatch):
    orch._config_cache = {"sessions": {"seo-audit": {"mode": "auto", "phases": [
        {"id": "stage-4", "title": "s4"}, {"id": "stage-5", "title": "s5"}]}}}
    out = orch.set_phase_text("seo-audit", "stage-5", "Prepare a dry-run plan; do not release.")
    assert out["recorded"] is True
    assert orch.get_phase_text("seo-audit", "stage-5").startswith("Prepare")
    # _session_cfg overlays the recorded text onto the phase
    cfg = orch._session_cfg("seo-audit")
    s5 = next(p for p in cfg["phases"] if p["id"] == "stage-5")
    assert s5["approved_task_text"].startswith("Prepare")


def test_set_phase_text_rejects_unknown_session():
    orch._config_cache = {"sessions": {}}
    with pytest.raises(ValueError):
        orch.set_phase_text("ghost", "p", "text")


def test_set_phase_text_rejects_unknown_phase():
    orch._config_cache = {"sessions": {"seo-audit": {"phases": [{"id": "stage-4"}]}}}
    with pytest.raises(ValueError):
        orch.set_phase_text("seo-audit", "stage-99", "text")


@pytest.mark.parametrize("bad", [
    "Publish to LinkedIn now",
    "activate premium plan",
    "send email to users",
    "rotate secret and update credential",
    "charge the customer payment",
])
def test_set_phase_text_rejects_external_side_effects(bad):
    orch._config_cache = {"sessions": {"seo-audit": {"phases": [{"id": "stage-5"}]}}}
    with pytest.raises(ValueError):
        orch.set_phase_text("seo-audit", "stage-5", bad)


# ── Commander hardening: decision_type / exact blocker text ─────────────────
def test_decision_type_financial():
    assert orch.decision_type("charge the customer 20 usd via stripe", "denied") == "financial"


def test_decision_type_external():
    assert orch.decision_type("git push origin main", "denied") == "external"
    assert orch.decision_type("docker compose restart backend", "denied") == "external"


def test_decision_type_internal():
    # a non-external, non-financial, merely-unrecognised local command.
    assert orch.decision_type("frobnicate --local ./data", "denied") == "internal"


def test_waiting_owner_carries_exact_blocker_text_not_just_category():
    d = orch.derive(_agent(state="waiting_owner", tail=DANGEROUS_PROMPT), AUTO)
    assert d["decision_type"] == "external"
    # exact command must appear in the blocker text, not just the generic category.
    assert "docker compose restart backend" in d["blocker_text"]
    assert d["blocker_text"] != d["blocker_category"]


def test_internal_waiting_owner_does_not_escalate(monkeypatch):
    INTERNAL_PROMPT = ("  Bash command\n  frobnicate --local ./data\n  Do a local thing\n\n"
                       "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    agent = _agent(state="waiting_owner", tail=INTERNAL_PROMPT)
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": [agent]})
    monkeypatch.setattr(orch.ac, "_pane_tail", lambda *a, **k: INTERNAL_PROMPT)
    monkeypatch.setattr(orch, "_session_cfg", lambda s: AUTO)
    out = orch.refresh_and_resolve(approve=True)
    assert out["escalations"] == []           # internal → no Telegram escalation
    rec = orch.get_record("seo-audit:0.0")
    assert rec["notification_state"] == "owner_review_internal"


def test_external_waiting_owner_escalates_once(monkeypatch):
    agent = _agent(state="waiting_owner", tail=DANGEROUS_PROMPT)
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": [agent]})
    monkeypatch.setattr(orch.ac, "_pane_tail", lambda *a, **k: DANGEROUS_PROMPT)
    monkeypatch.setattr(orch, "_session_cfg", lambda s: AUTO)
    out = orch.refresh_and_resolve(approve=True)
    assert len(out["escalations"]) == 1
    assert out["escalations"][0]["decision_type"] == "external"
    # second sweep on the same prompt does not re-escalate.
    out2 = orch.refresh_and_resolve(approve=True)
    assert out2["escalations"] == []


# ── Commander hardening: reliable completion detection ──────────────────────
def test_active_exec_markers_block_false_completion(monkeypatch):
    monkeypatch.setattr(orch.ac, "agent_report",
                        lambda *a, **k: {"reports": [{"path": "reports/DONE.md",
                                                      "modified_at": orch.datetime.now(orch.timezone.utc).isoformat()}]})
    # agent looks idle but the pane still shows it executing → NOT completed.
    a = _agent(state="idle", tail="… esc to interrupt")
    assert orch._completion_evidence("seo-audit", "/opt/seo", a) is None


def test_completion_when_idle_and_fresh_report(monkeypatch):
    now = orch.datetime.now(orch.timezone.utc).isoformat()
    monkeypatch.setattr(orch.ac, "agent_report",
                        lambda *a, **k: {"reports": [{"path": "reports/DONE.md", "modified_at": now}]})
    a = _agent(state="idle", tail="done. summary written.")
    ev = orch._completion_evidence("seo-audit", "/opt/seo", a)
    assert ev and ev["report_path"] == "reports/DONE.md"


def test_stale_report_before_last_activity_is_not_completion(monkeypatch):
    # report is fresh-within-window but PREDATES the agent's last real activity.
    old = orch.datetime.now(orch.timezone.utc).isoformat()
    monkeypatch.setattr(orch.ac, "agent_report",
                        lambda *a, **k: {"reports": [{"path": "reports/OLD.md", "modified_at": old}]})
    orch._upsert({"agent_key": "seo-audit:0.0", "session": "seo-audit", "project": "seo",
                  "state": "working", "last_fresh_activity_ts": orch._now_ts() + 60})
    a = _agent(state="idle", tail="")
    assert orch._completion_evidence("seo-audit", "/opt/seo", a) is None


# ── Commander hardening: verified continuation after limits reset ───────────
def test_budget_reset_working_marks_resumed(monkeypatch):
    agent = _agent(state="working")
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": [agent]})
    monkeypatch.setattr(orch.ac, "_pane_tail", lambda *a, **k: "")
    monkeypatch.setattr(orch, "_session_cfg", lambda s: AUTO)
    orch._upsert({"agent_key": "seo-audit:0.0", "session": "seo-audit", "project": "seo",
                  "state": "paused_by_budget"})
    out = orch.refresh_and_resolve(approve=True)
    rec = orch.get_record("seo-audit:0.0")
    assert rec["notification_state"] == "resumed_after_budget"
    assert out["escalations"] == []           # self-resumed → no alert


def test_budget_reset_idle_marks_stalled_without_escalation(monkeypatch):
    agent = _agent(state="idle")
    monkeypatch.setattr(orch.ac, "agent_list", lambda: {"agents": [agent]})
    monkeypatch.setattr(orch.ac, "_pane_tail", lambda *a, **k: "")
    monkeypatch.setattr(orch, "_session_cfg", lambda s: AUTO)
    monkeypatch.setattr(orch, "_completion_evidence", lambda *a, **k: None)
    from core import agent_context_budget as _ctxb        # isolate from the real /opt/seo handoff
    monkeypatch.setattr(_ctxb, "detect_surfaceable_event", lambda *a, **k: None)
    orch._upsert({"agent_key": "seo-audit:0.0", "session": "seo-audit", "project": "seo",
                  "state": "paused_by_budget"})
    out = orch.refresh_and_resolve(approve=True)
    rec = orch.get_record("seo-audit:0.0")
    assert rec["notification_state"] == "stalled_after_budget"
    assert "no auto re-dispatch" in (rec["blocker_text"] or "")
    assert out["escalations"] == []           # internal → surfaced, not alerted


# ── run_loop registers itself for deploy-skew detection (event 11073) ──────
# ai-runtime.service, which owns this loop, was never restarted across three
# straight agent_control.py fixes on 2026-08-28 - each fix's deploy step only
# restarted the SEPARATE owner-os-wake-companion.service. Each tick must
# heartbeat wake_bridge's worker registry so that gap becomes self-diagnosing
# via worker_skew() instead of silently recurring.
class _StopLoop(Exception):
    pass


def test_run_loop_registers_the_orchestrator_worker_each_tick(monkeypatch):
    import asyncio
    from core import wake_bridge as wb

    monkeypatch.setattr(orch, "ENABLED", True)
    monkeypatch.setattr(orch, "load_config", lambda: None)
    monkeypatch.setattr(orch, "refresh_and_resolve", lambda approve=True: {})
    from core import direct_agent_lifecycle as _dal
    monkeypatch.setattr(_dal, "ENABLED", False)

    calls = []
    monkeypatch.setattr(wb, "register_worker", lambda w: calls.append(w))

    async def _raise_sleep(_secs):
        raise _StopLoop

    monkeypatch.setattr(asyncio, "sleep", _raise_sleep)

    with pytest.raises(_StopLoop):
        asyncio.run(orch.run_loop())

    assert calls == ["agent_orchestrator"]
