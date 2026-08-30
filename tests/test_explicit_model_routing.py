"""task 220 — explicit Sonnet/Opus/Fable selection, end to end.

Two defects are pinned here, both of which made "run this one on Opus"
impossible in real dispatch even though the policy modules read as if it were
supported:

1. `core.model_router.route()` had no way to be TOLD a model. The only inputs
   were task_class / risk / prior attempts, so a caller who wanted opus could
   only hope the partition landed there — which is why runtime job #81
   (owner_task #220, and #219 before it) dispatched on sonnet.
2. `escalation_reason` was read from the job dict by
   `job_executor._route_model()` but was never a COLUMN on the jobs table, so
   any job re-read from the store (i.e. every real dispatch) arrived without
   it and the task-213 hard gate de-escalated it to sonnet. The in-memory-only
   test in test_runtime_model_routing.py passed precisely because it never
   round-tripped through the store.

The safety properties of task 213 must survive all of this: an explicit ask
for an expensive tier still has to clear the escalation gate, and an explicit
ask for a CHEAPER tier is refused whenever a risk floor or a prior-attempt
escalation is what put the decision where it is.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(),
                                                 "rt_explicit_model_jobs.db"))

from core import job_executor, job_kinds, job_store, model_router  # noqa: E402


def setup_module(_m):
    # conftest.py points RUNTIME_DB at ONE shared temp file for the whole pytest
    # session. Removing it here raced other test modules' still-live background
    # threads (job_executor's heartbeat, or an unjoined dispatch thread) writing
    # to it mid-run -> sqlite3.OperationalError: attempt to write a readonly
    # database in unrelated files. Clearing the ROWS instead leaves the file
    # (and any other module's open connection) intact, while still giving this
    # module the same "starts empty" guarantee the old os.remove() gave it.
    job_store.init_db()
    with job_store._LOCK, job_store._conn() as c:
        c.execute("DELETE FROM jobs")


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("RUNTIME_MODEL_ROUTER", "1")


VALID_OPUS = {
    "category": "architecture",
    "evidence": "two sonnet passes produced contradictory designs",
    "expected_benefit": "one coherent architecture instead of two half ones",
}
VALID_FABLE = {
    "category": "hardest_unresolved",
    "evidence": "opus and sonnet both failed on the same race",
    "expected_benefit": "the last tier that has not tried it yet",
}


# ── router: explicit selection ───────────────────────────────────────────────

def test_explicit_opus_with_valid_reason_is_granted():
    out = model_router.route("routine_implementation", explicit_model="opus",
                             context_pack="delta-pack", escalation_reason=VALID_OPUS)
    assert out["model"] == model_router.OPUS
    assert out["model_id"] == model_router.MODEL_IDS[model_router.OPUS]
    assert out["explicit_model"] == "opus"
    assert out["explicit_granted"] is True
    assert out["escalation_valid"] is True


def test_explicit_fable_with_valid_reason_is_granted():
    out = model_router.route("routine_implementation", explicit_model="fable",
                             context_pack="delta-pack", escalation_reason=VALID_FABLE)
    assert out["model"] == model_router.FABLE
    assert out["explicit_granted"] is True


def test_explicit_opus_without_reason_still_de_escalates_to_sonnet():
    """The task-213 hard gate outranks an explicit ask: naming the tier is not
    the same as justifying it."""
    out = model_router.route("routine_implementation", explicit_model="opus")
    assert out["model"] == model_router.SONNET
    assert out["requested_model"] == model_router.OPUS   # what policy computed
    assert out["explicit_granted"] is True               # the raise happened
    assert out["escalation_valid"] is False              # the gate refused it
    assert "hard gate de-escalates to sonnet" in out["reason"]


def test_explicit_opus_with_wrong_category_for_the_tier_is_refused():
    out = model_router.route("routine_implementation", explicit_model="opus",
                             context_pack="pack", escalation_reason=dict(VALID_FABLE))
    assert out["model"] == model_router.SONNET
    assert out["escalation_valid"] is False


def test_explicit_opus_without_context_pack_is_refused():
    out = model_router.route("routine_implementation", explicit_model="opus",
                             escalation_reason=VALID_OPUS)
    assert out["model"] == model_router.SONNET
    assert out["context_pack_missing"] is True


def test_explicit_sonnet_downgrade_is_honoured_when_nothing_safety_relevant_holds():
    out = model_router.route("architecture", explicit_model="sonnet")
    assert out["model"] == model_router.SONNET
    assert out["explicit_granted"] is True
    assert "(downgrade)" in out["reason"]


def test_explicit_sonnet_downgrade_is_refused_under_a_risk_floor():
    out = model_router.route("architecture", risk="money", explicit_model="sonnet",
                             context_pack="pack", escalation_reason=VALID_OPUS)
    assert out["model"] == model_router.OPUS
    assert out["explicit_granted"] is False
    assert "REFUSED" in out["reason"]


def test_explicit_sonnet_downgrade_is_refused_after_a_sonnet_failure():
    """Cost is not a reason to re-run failed work on the tier that failed it."""
    out = model_router.route("routine_implementation", explicit_model="sonnet",
                             context_pack="pack", escalation_reason=VALID_OPUS,
                             prior_attempts=[{"model": "sonnet", "outcome": "failure"}])
    assert out["model"] == model_router.OPUS
    assert out["explicit_granted"] is False


def test_explicit_model_matching_the_policy_choice_is_a_no_op_but_recorded():
    out = model_router.route("routine_implementation", explicit_model="sonnet")
    assert out["model"] == model_router.SONNET
    assert out["explicit_granted"] is True
    assert "already routed there" in out["reason"]


def test_unknown_explicit_model_is_refused_loudly():
    with pytest.raises(model_router.RouterError):
        model_router.route("routine_implementation", explicit_model="gpt-9")


def test_explicit_selection_is_recorded_in_the_decision_ledger():
    out = model_router.route("routine_implementation", explicit_model="opus",
                             context_pack="pack", escalation_reason=VALID_OPUS,
                             task_ref="test:explicit")
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    try:
        row = conn.execute(
            "SELECT explicit_model, explicit_granted, model FROM router_decision "
            "WHERE id=?", (out["decision_id"],)).fetchone()
    finally:
        conn.close()
    assert row == ("opus", 1, "opus")


def test_route_without_explicit_model_is_unchanged():
    """The pre-task-220 contract still holds for every existing caller."""
    out = model_router.route("routine_implementation")
    assert out["model"] == model_router.SONNET
    assert out["explicit_model"] is None
    assert out["explicit_granted"] is None


# ── durability: the columns that were missing ────────────────────────────────

def test_job_store_round_trips_requested_model_and_escalation_reason():
    job = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                               kind=job_kinds.CODE_CHANGE,
                               requested_model="opus", escalation_reason=VALID_OPUS)
    reread = job_store.get_job(job["id"])
    assert reread["requested_model"] == "opus"
    assert reread["escalation_reason"] == VALID_OPUS


def test_job_store_defaults_stay_none_for_ordinary_jobs():
    job = job_store.get_job(job_store.create_job(project_path="/tmp", goal="g")["id"])
    assert job["requested_model"] is None
    assert job["escalation_reason"] is None


def test_escalation_reason_column_added_to_a_pre_task_220_database(tmp_path, monkeypatch):
    """A live DB predates both columns; init_db must migrate it, not crash."""
    old_db = tmp_path / "legacy_jobs.db"
    conn = sqlite3.connect(str(old_db))
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id INTEGER, "
                 "project_path TEXT, task_id INTEGER, goal TEXT, instructions TEXT, "
                 "constraints TEXT, allowed_paths TEXT, forbidden_paths TEXT, "
                 "autonomy_level TEXT, approval_required INTEGER, auto_commit INTEGER, "
                 "auto_push INTEGER, auto_deploy INTEGER, target_branch TEXT, "
                 "base_branch TEXT, status TEXT, risk_level TEXT, dangerous INTEGER, "
                 "plan TEXT, changed_files TEXT, validation TEXT, tests TEXT, "
                 "git_info TEXT, logs TEXT, error TEXT, artifacts TEXT, created_at TEXT, "
                 "started_at TEXT, finished_at TEXT, updated_at TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(job_store, "_DB", str(old_db))
    job_store.init_db()
    cols = {r[1] for r in sqlite3.connect(str(old_db)).execute("PRAGMA table_info(jobs)")}
    assert {"requested_model", "escalation_reason", "kind", "outcome"} <= cols


# ── dispatch: a job re-read from the store reaches the tier it asked for ─────

def _selection(job_id: str) -> dict:
    final = job_store.get_job(job_id)
    sels = [a["model_selection"] for a in (final["artifacts"] or [])
            if isinstance(a, dict) and isinstance(a.get("model_selection"), dict)]
    return sels[-1]


def test_persisted_explicit_opus_reaches_opus_after_a_store_round_trip():
    created = job_store.create_job(project_path="/tmp", goal="design the bridge",
                                   instructions="", kind=job_kinds.CODE_CHANGE,
                                   requested_model="opus", escalation_reason=VALID_OPUS)
    job = job_store.get_job(created["id"])          # exactly what the executor does
    m_id, m_dec, m_name = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert m_name == model_router.OPUS
    assert m_id == model_router.MODEL_IDS[model_router.OPUS]
    sel = _selection(created["id"])
    assert sel["explicit_model"] == "opus"
    assert sel["escalation_valid"] is True


def test_persisted_explicit_opus_without_reason_lands_on_sonnet():
    created = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                   kind=job_kinds.CODE_CHANGE, requested_model="opus")
    job = job_store.get_job(created["id"])
    _m_id, _dec, m_name = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert m_name == model_router.SONNET
    sel = _selection(created["id"])
    assert sel["explicit_model"] == "opus"
    assert sel["escalation_valid"] is False


def test_persisted_explicit_fable_reaches_fable():
    created = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                   kind=job_kinds.CODE_CHANGE,
                                   requested_model="fable", escalation_reason=VALID_FABLE)
    job = job_store.get_job(created["id"])
    _m_id, _dec, m_name = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert m_name == model_router.FABLE


def test_ordinary_job_without_an_explicit_ask_is_untouched():
    created = job_store.create_job(project_path="/tmp", goal="g", instructions="",
                                   kind=job_kinds.CODE_CHANGE)
    job = job_store.get_job(created["id"])
    _m_id, _dec, m_name = job_executor._route_model(job, job_kinds.CODE_CHANGE)
    assert m_name == model_router.SONNET
    assert _selection(created["id"])["explicit_model"] is None


# ── API surface ──────────────────────────────────────────────────────────────

def test_api_job_create_persists_the_explicit_selection(monkeypatch, tmp_path):
    from api import v1
    monkeypatch.setattr(v1.job_executor, "execute_async", lambda *_a, **_k: None)
    monkeypatch.setattr(v1, "_validate_project_path", lambda p: str(tmp_path))
    req = v1.JobCreate(project_path=str(tmp_path), goal="g", autonomy="prepare",
                       requested_model="opus", escalation_reason=VALID_OPUS)
    view = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        v1.create_job(req, True))
    stored = job_store.get_job(view["id"])
    assert stored["requested_model"] == "opus"
    assert stored["escalation_reason"] == VALID_OPUS


def test_api_job_create_refuses_an_unknown_model(monkeypatch, tmp_path):
    from fastapi import HTTPException

    from api import v1
    monkeypatch.setattr(v1, "_validate_project_path", lambda p: str(tmp_path))
    req = v1.JobCreate(project_path=str(tmp_path), goal="g", requested_model="gpt-9")
    loop = asyncio.get_event_loop_policy().new_event_loop()
    with pytest.raises(HTTPException) as e:
        loop.run_until_complete(v1.create_job(req, True))
    assert e.value.status_code == 422


def test_api_router_route_accepts_explicit_model(monkeypatch):
    from api import v1
    req = v1.RouterRoute(task_class="routine_implementation", explicit_model="opus",
                         context_pack="pack", escalation_reason=VALID_OPUS)
    loop = asyncio.get_event_loop_policy().new_event_loop()
    out = loop.run_until_complete(v1.router_route(req, True))
    assert out["model"] == model_router.OPUS


# ── the retry-path 404: POST /api/v1/smoke was never implemented ─────────────

def test_smoke_route_is_registered_at_the_path_the_caller_uses():
    """/opt/seo/backend/services/runtime_client.py:provider_smoke() POSTs
    `{RUNTIME_URL}/smoke`; RUNTIME_URL is the /api/v1 base. Its absence is the
    HTTP 404 that blocked every runtime retry."""
    from api import v1
    paths = {(r.path, tuple(sorted(r.methods))) for r in v1.router.routes}
    assert ("/api/v1/smoke", ("POST",)) in paths


def test_smoke_endpoint_returns_the_ai_planner_contract(monkeypatch):
    from api import v1
    monkeypatch.setattr(v1.ai_planner, "smoke", lambda model=None, timeout_seconds=None: {
        "ok": True, "provider": "claude-cli", "model": model or "claude-sonnet-5",
        "latency_seconds": 0.2, "tokens": {"input_tokens": 4, "output_tokens": 1},
        "cost_usd": 0.0001, "error": None})
    loop = asyncio.get_event_loop_policy().new_event_loop()
    out = loop.run_until_complete(v1.provider_smoke(v1.SmokeReq(model="claude-opus-5"), True))
    assert out["ok"] is True
    assert out["model"] == "claude-opus-5"
    assert set(out) >= {"ok", "provider", "model", "latency_seconds", "tokens",
                        "cost_usd", "error"}


def test_smoke_endpoint_reports_a_provider_failure_as_a_200_body(monkeypatch):
    """ok=false must stay distinguishable from a transport failure — the retry
    gate reads the body, and a 500 here would look identical to the 404 it just
    replaced."""
    from api import v1
    monkeypatch.setattr(v1.ai_planner, "smoke", lambda model=None, timeout_seconds=None: {
        "ok": False, "provider": "claude-cli", "model": None, "latency_seconds": 0.0,
        "tokens": None, "cost_usd": None, "error": "provider_not_configured"})
    loop = asyncio.get_event_loop_policy().new_event_loop()
    out = loop.run_until_complete(v1.provider_smoke(None, True))
    assert out["ok"] is False
    assert out["error"] == "provider_not_configured"


def test_smoke_endpoint_never_touches_a_project_path():
    """Read-only by construction: the request model carries no path at all."""
    from api import v1
    assert set(v1.SmokeReq.model_fields) == {"model", "timeout_seconds"}
    assert json.dumps(v1.SmokeReq().model_dump()) == '{"model": null, "timeout_seconds": null}'
