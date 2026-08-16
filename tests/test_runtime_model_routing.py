"""Tests for wiring core/model_router.py (task 209) into real Runtime dispatch
(core/job_executor.py + core/ai_planner.py's new `model` kwarg).

Covers:
  - job kind -> model_router task_class mapping (every kind + unknown kind)
  - risk extraction from job.risk_level / goal / instructions text
  - _route_model records exactly one router_decision row + one model_selection
    artifact per call
  - router disabled (RUNTIME_MODEL_ROUTER=0) -> no-op, zero rows
  - _route_model exception safety: a broken model_router.route() never fails
    or blocks the job
  - task_id lineage: a prior FAILED job's recorded model feeds prior_attempts
  - de-escalation / escalation policy behaviour at the wiring level
  - ai_planner.plan() accepts and forwards a `model` kwarg (signature +
    executor-level capture)

No network / no real provider CLI needed for the unit-level tests. The one
executor-level test stubs `ai_planner.plan` directly (capturing stub), which
is lighter than driving a fake `claude` CLI subprocess for something that
only needs to prove one kwarg is threaded through.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import subprocess
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_model_routing_jobs.db"))

from core import ai_planner, job_executor, job_kinds, job_store, model_router  # noqa: E402


def setup_module(_m):
    try:
        os.remove(os.environ["RUNTIME_DB"])
    except (FileNotFoundError, KeyError):
        pass
    job_store.init_db()


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


def _decision_row_count() -> int:
    path = os.environ["CONTROL_PLANE_DB"]
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(path)
    try:
        try:
            return conn.execute("SELECT COUNT(*) FROM router_decision").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _git(path, *args):
    subprocess.run(["git", "-C", str(path)] + list(args), check=True,
                   capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


# ── kind -> task_class mapping ───────────────────────────────────────────────

@pytest.mark.parametrize("kind,expected", [
    (job_kinds.CODE_CHANGE, "routine_implementation"),
    (job_kinds.OPERATIONAL, "routine_implementation"),
    (job_kinds.CONTENT_PRODUCTION, "docs"),
    (job_kinds.DEPLOYMENT, "release_design"),
    (job_kinds.DATA_HANDOFF, "context_pack"),
    (job_kinds.CONTEXT_RESTORE, "context_pack"),
])
def test_kind_task_class_mapping(kind, expected):
    assert job_executor._task_class_for_kind(kind) == expected


def test_unknown_kind_maps_to_empty_and_falls_through_to_sonnet_via_route():
    assert job_executor._task_class_for_kind("some_unheard_of_kind") == ""
    out = model_router.route("some_unheard_of_kind")
    assert out["model"] == model_router.SONNET
    assert "unknown_class_defaults_to_sonnet" in out["reason"]


# ── risk extraction ───────────────────────────────────────────────────────────

def test_risk_high_risk_level():
    assert job_executor._risk_for_job({"risk_level": "high", "goal": "", "instructions": ""}) == "high"


def test_risk_critical_risk_level():
    assert job_executor._risk_for_job({"risk_level": "critical", "goal": "", "instructions": ""}) == "high"


def test_risk_money_goal():
    job = {"risk_level": "medium", "goal": "process a customer refund", "instructions": ""}
    assert job_executor._risk_for_job(job) == "money"


def test_risk_security_goal():
    job = {"risk_level": "medium", "goal": "", "instructions": "rotate the leaked api key"}
    assert job_executor._risk_for_job(job) == "security"


def test_risk_money_wins_over_security_when_both_present():
    job = {"risk_level": "medium", "goal": "refund a payment made with a stolen credential",
          "instructions": ""}
    assert job_executor._risk_for_job(job) == "money"


def test_risk_plain_goal_is_normal():
    job = {"risk_level": "medium", "goal": "update the README", "instructions": "fix a typo"}
    assert job_executor._risk_for_job(job) == "normal"


def test_risk_never_returns_anything_outside_the_four_values():
    valid = {"high", "money", "security", "normal"}
    samples = [
        {"risk_level": "low", "goal": "refund payout", "instructions": "auth token"},
        {"risk_level": "high", "goal": "", "instructions": ""},
        {"risk_level": "", "goal": "", "instructions": ""},
    ]
    for job in samples:
        assert job_executor._risk_for_job(job) in valid


# ── _route_model: decision + artifact recording ──────────────────────────────

def test_route_model_records_exactly_one_decision_and_artifact(monkeypatch):
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path="/tmp", goal="implement a small change",
                               instructions="", kind=job_kinds.CODE_CHANGE)
    assert _decision_row_count() == 0
    m_id, m_dec, m_name = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert _decision_row_count() == 1
    assert m_dec is not None
    assert m_name == model_router.SONNET
    assert m_id == model_router.MODEL_IDS[model_router.SONNET]

    final = job_store.get_job(job["id"])
    sels = [a for a in (final["artifacts"] or [])
           if isinstance(a, dict) and isinstance(a.get("model_selection"), dict)]
    assert len(sels) == 1
    assert sels[0]["model_selection"]["decision_id"] == m_dec
    assert sels[0]["model_selection"]["model"] == model_router.SONNET
    assert sels[0]["model_selection"]["stage"] == "plan"


def test_route_model_second_call_records_a_second_decision(monkeypatch):
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path="/tmp", goal="implement a small change",
                               instructions="", kind=job_kinds.CODE_CHANGE)
    job_executor._route_model(job, job_kinds.CODE_CHANGE, stage="plan")
    job = job_store.get_job(job["id"])
    job_executor._route_model(job, job_kinds.CODE_CHANGE, stage="repair1")
    assert _decision_row_count() == 2
    final = job_store.get_job(job["id"])
    sels = [a for a in (final["artifacts"] or [])
           if isinstance(a, dict) and isinstance(a.get("model_selection"), dict)]
    assert len(sels) == 2
    assert {s["model_selection"]["stage"] for s in sels} == {"plan", "repair1"}


# ── router disabled ────────────────────────────────────────────────────────

@pytest.mark.parametrize("off_value", ["0", "", "false", "no"])
def test_route_model_disabled_returns_nones_and_zero_rows(monkeypatch, off_value):
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", off_value)
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               kind=job_kinds.CODE_CHANGE)
    result = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert result == (None, None, None)
    assert _decision_row_count() == 0
    final = job_store.get_job(job["id"])
    assert not any(isinstance(a, dict) and "model_selection" in a
                  for a in (final["artifacts"] or []))


# ── exception safety ─────────────────────────────────────────────────────────

def test_route_model_exception_safety(monkeypatch):
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")

    def _raise(*_a, **_k):
        raise RuntimeError("router boom")

    monkeypatch.setattr(model_router, "route", _raise)
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               kind=job_kinds.CODE_CHANGE)
    result = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert result == (None, None, None)
    final = job_store.get_job(job["id"])
    assert final["status"] != "failed"
    logs = final.get("logs") or []
    assert any("model routing unavailable" in (l.get("msg") or "") for l in logs)


# ── lineage: prior FAILED job feeds prior_attempts ───────────────────────────

def test_lineage_attempts_from_prior_failed_job_same_task_id():
    task_id = 900001
    failed_job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                      task_id=task_id, kind=job_kinds.CODE_CHANGE)
    job_store.update_job(failed_job["id"], status="failed", artifacts=[
        {"model_selection": {"stage": "plan", "decision_id": 1,
                             "model": "sonnet", "model_id": "claude-sonnet-5"}}])
    newer_job = job_store.create_job(project_path="/tmp", goal="g2", instructions="",
                                     task_id=task_id, kind=job_kinds.CODE_CHANGE)
    attempts = job_executor._lineage_attempts(newer_job)
    assert attempts == [{"model": "sonnet", "outcome": "failure"}]


def test_lineage_attempts_ignores_other_task_ids_and_non_failed_jobs():
    task_id = 900002
    other_task_failed = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                             task_id=900099, kind=job_kinds.CODE_CHANGE)
    job_store.update_job(other_task_failed["id"], status="failed", artifacts=[
        {"model_selection": {"model": "opus"}}])
    same_task_ok = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                        task_id=task_id, kind=job_kinds.CODE_CHANGE)
    job_store.update_job(same_task_ok["id"], status="completed", artifacts=[
        {"model_selection": {"model": "sonnet"}}])
    newer_job = job_store.create_job(project_path="/tmp", goal="g2", instructions="",
                                     task_id=task_id, kind=job_kinds.CODE_CHANGE)
    assert job_executor._lineage_attempts(newer_job) == []


def test_lineage_attempts_no_task_id_returns_empty():
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               task_id=None, kind=job_kinds.CODE_CHANGE)
    assert job_executor._lineage_attempts(job) == []


# ── de-escalation / escalation policy at the wiring level ───────────────────

def test_deescalation_clear_finding_with_prior_fable_success_routes_sonnet():
    out = model_router.route("clear_finding_implementation",
                             prior_attempts=[{"model": "fable", "outcome": "success"}])
    assert out["model"] == model_router.SONNET


def test_route_model_escalation_ladder_without_reason_stays_on_sonnet(monkeypatch):
    """task 213 hard gate: automated job dispatch never sets escalation_reason,
    so a sonnet-failure ladder trigger (routine load/perf work retrying) must
    NOT reach opus — it de-escalates to sonnet instead of silently spending
    on the expensive tier."""
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               kind=job_kinds.CODE_CHANGE)
    m_id, m_dec, m_name = job_executor._route_model(
        job, job_kinds.CODE_CHANGE,
        extra_attempts=[{"model": "sonnet", "outcome": "failure"}])
    assert m_name == model_router.SONNET
    assert m_id == model_router.MODEL_IDS[model_router.SONNET]
    assert m_dec is not None
    final = job_store.get_job(job["id"])
    sels = [a for a in (final["artifacts"] or [])
           if isinstance(a, dict) and isinstance(a.get("model_selection"), dict)]
    assert sels[-1]["model_selection"]["requested_model"] == model_router.OPUS
    assert sels[-1]["model_selection"]["escalation_valid"] is False


def test_route_model_with_valid_escalation_reason_reaches_opus(monkeypatch):
    """The one legitimate path to opus: the job itself carries a structured,
    valid escalation_reason (an owner/agent explicitly justifying the tier)."""
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               kind=job_kinds.CODE_CHANGE)
    job["escalation_reason"] = {
        "category": "high_ambiguity",
        "evidence": "two sonnet attempts failed with conflicting root causes",
        "expected_benefit": "opus can reconcile the conflicting diagnoses",
    }
    m_id, m_dec, m_name = job_executor._route_model(
        job, job_kinds.CODE_CHANGE,
        extra_attempts=[{"model": "sonnet", "outcome": "failure"}])
    assert m_name == model_router.OPUS
    assert m_id == model_router.MODEL_IDS[model_router.OPUS]
    assert m_dec is not None


@pytest.mark.parametrize("kind,goal,instructions", [
    (job_kinds.OPERATIONAL, "run the routine load test suite", ""),
    (job_kinds.CODE_CHANGE, "apply the concrete post-audit fix", "finding is unambiguous"),
    (job_kinds.CONTENT_PRODUCTION, "write repo inspection docs", ""),
])
def test_routine_dispatch_kinds_never_reach_opus_or_fable(monkeypatch, kind, goal, instructions):
    """task 213: routine implementation / load-perf / docs / repo-inspection /
    concrete-fix dispatch must route to sonnet even when the job carries a
    money/security/high risk_level — no escalation_reason means no expensive
    tier, full stop."""
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path="/tmp", goal=goal, instructions=instructions,
                               kind=kind, risk_level="critical")
    m_id, m_dec, m_name = job_executor._route_model(job, kind)
    assert m_name == model_router.SONNET
    assert m_id == model_router.MODEL_IDS[model_router.SONNET]


# ── ai_planner.plan() `model` kwarg ──────────────────────────────────────────

def test_ai_planner_plan_accepts_model_kwarg():
    sig = inspect.signature(ai_planner.plan)
    assert "model" in sig.parameters
    assert sig.parameters["model"].default is None


def test_executor_plan_stage_passes_routed_model_into_ai_planner_plan(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    captured = {}

    def fake(goal, instructions, pp, allowed, timeout=None, heartbeat_cb=None,
            kind=None, model=None):
        captured["model"] = model
        captured["calls"] = captured.get("calls", 0) + 1
        return {"summary": "s", "risk_level": "low",
               "files": [{"path": "x.txt", "operation": "create", "content": "hi"}],
               "test_commands": []}

    monkeypatch.setattr(ai_planner, "plan", fake)
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    job = job_store.create_job(project_path=str(repo), goal="implement a small change",
                               instructions="", task_id=900500, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False, kind=job_kinds.CODE_CHANGE)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert captured["model"] == model_router.MODEL_IDS[model_router.SONNET]
    assert captured["calls"] == 1
    assert final["status"] == "completed", final.get("error")
    sels = [a for a in (final["artifacts"] or [])
           if isinstance(a, dict) and isinstance(a.get("model_selection"), dict)]
    assert len(sels) == 1
    assert sels[0]["model_selection"]["stage"] == "plan"


def test_executor_plan_stage_router_disabled_passes_model_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    captured = {}

    def fake(goal, instructions, pp, allowed, timeout=None, heartbeat_cb=None,
            kind=None, model=None):
        captured["model"] = model
        return {"summary": "s", "risk_level": "low",
               "files": [{"path": "y.txt", "operation": "create", "content": "hi"}],
               "test_commands": []}

    monkeypatch.setattr(ai_planner, "plan", fake)
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "0")
    job = job_store.create_job(project_path=str(repo), goal="implement a small change",
                               instructions="", task_id=900600, autonomy_level="execute_safe",
                               auto_commit=True, auto_push=False, kind=job_kinds.CODE_CHANGE)
    job_executor.execute(job["id"])
    final = job_store.get_job(job["id"])
    assert captured["model"] is None
    assert final["status"] == "completed", final.get("error")


def test_fallback_planning_preserves_model_selection_artifact(tmp_path, monkeypatch):
    """Live regression (job b34772f4): when the planner fails and the fallback
    plan kicks in, the fallback-metadata append must not clobber the
    model_selection artifact recorded moments earlier by _route_model."""
    import core.job_executor as jx
    from core import ai_planner, job_store

    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "README.md").write_text("x\n")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=proj, check=True)

    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")
    monkeypatch.setattr(ai_planner, "available", lambda: True)

    def broken_plan(*a, **k):
        raise ai_planner.PlannerError("plan missing 'files' list")

    monkeypatch.setattr(ai_planner, "plan", broken_plan)
    job = job_store.create_job(goal="tiny note", project_path=str(proj),
                               autonomy_level="suggest", kind="code_change")
    jx.execute(job["id"])
    j = job_store.get_job(job["id"])
    arts = j.get("artifacts") or []
    assert any(isinstance(a, dict) and "model_selection" in a for a in arts), \
        f"model_selection artifact lost: {arts}"
    assert any(isinstance(a, dict) and a.get("fallback_planning") for a in arts)
