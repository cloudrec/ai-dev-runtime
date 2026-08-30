"""Adapter contract for the seo-backend thin client (Owner OS 2.0 architecture
decision, 2026-08-15): all core logic lives HERE; seo-backend only consumes
this HTTP surface. These tests pin the contract — paths present in the OpenAPI
schema and response shapes stable — so a core refactor that would break the
adapter fails in THIS repo's suite, before any seo-backend rebuild."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from api import v1


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    from core import job_store
    job_store.init_db()


CONTRACT_PATHS = [
    # runtime jobs (already consumed by the delegator today)
    "/api/v1/jobs", "/api/v1/jobs/{job_id}", "/api/v1/jobs/{job_id}/approve",
    "/api/v1/jobs/{job_id}/cancel",
    # observability
    "/api/v1/runtime/status", "/api/v1/control-plane/observability",
    # agent fabric v1
    "/api/v1/fabric/agents", "/api/v1/fabric/agents/{ref}/status",
    "/api/v1/fabric/agents/{ref}/send", "/api/v1/fabric/agents/{ref}/stop",
    "/api/v1/fabric/agents/{ref}/result", "/api/v1/fabric/start-or-resume",
    "/api/v1/fabric/contracts", "/api/v1/fabric/contracts/{contract_id}",
    "/api/v1/fabric/contracts/{contract_id}/transition",
    # venture radar (task 193)
    "/api/v1/radar/candidates", "/api/v1/radar/candidates/{candidate_id}",
    "/api/v1/radar/candidates/{candidate_id}/card",
    "/api/v1/radar/candidates/{candidate_id}/transition", "/api/v1/radar/seed",
    # business analyzer (task 202)
    "/api/v1/analyzer/cards", "/api/v1/analyzer/cards/{card_id}",
    "/api/v1/analyzer/cards/{card_id}/rescore",
    "/api/v1/analyzer/cards/{card_id}/transition", "/api/v1/analyzer/combine",
    # model router (task 209)
    "/api/v1/router/route", "/api/v1/router/outcome",
    "/api/v1/router/effectiveness", "/api/v1/router/policy",
]


def test_contract_paths_exist_in_openapi():
    app = FastAPI()
    app.include_router(v1.router)
    paths = set(app.openapi()["paths"])
    missing = [p for p in CONTRACT_PATHS if p not in paths]
    assert not missing, f"adapter contract paths missing from OpenAPI: {missing}"


def test_fabric_agents_shape(monkeypatch):
    from core import agent_control
    monkeypatch.setattr(agent_control, "agent_list", lambda: {"agents": [
        {"target": "a:0.0", "is_agent": True, "alive": True, "state": "working",
         "claude_cwd": "/opt/seo"}]})
    body = asyncio.run(v1.fabric_agents(include_terminal=False, _=True))
    assert set(body) == {"agents", "count", "errors"}
    entry = body["agents"][0]
    for key in ("ref", "kind", "project", "cwd", "state", "fabric_state",
                "current_task", "last_activity", "alive", "healthy", "capabilities"):
        assert key in entry, f"adapter contract field missing: {key}"


def test_runtime_status_shape():
    body = asyncio.run(v1.runtime_status(_=True))
    for key in ("active_jobs", "stalled_jobs", "stalled_count",
                "waiting_approval", "recent_failed"):
        assert key in body, f"adapter contract field missing: {key}"


def test_contract_lifecycle_over_api():
    created = asyncio.run(v1.fabric_contract_create(v1.FabricContractCreate(
        contract={"goal": "g", "acceptance_criteria": ["a"]}, task_id=1), _=True))
    assert created["state"] == "CREATED"
    got = asyncio.run(v1.fabric_contract_get(created["id"], _=True))
    assert got["history"][0]["to"] == "CREATED"
    moved = asyncio.run(v1.fabric_contract_transition(
        created["id"], v1.FabricTransition(to_state="WORKING", by="adapter"), _=True))
    assert moved["state"] == "WORKING"


def test_refusals_are_409_with_reason():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(v1.fabric_status("bogus-ref", _=True))
    assert e.value.status_code == 409
    assert "bad agent ref" in str(e.value.detail)


def test_venture_refusals_are_409_with_reason():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(v1.radar_candidate_create(v1.RadarCandidateCreate(
            mode="moonshot", title="x", card={}), _=True))
    assert e.value.status_code == 409 and "unknown mode" in str(e.value.detail)
    with pytest.raises(HTTPException) as e2:
        asyncio.run(v1.analyzer_card_create(v1.AnalyzerCardCreate(
            title="x", card={"source_code": "lifted"}), _=True))
    assert e2.value.status_code == 409
    assert "public behavior only" in str(e2.value.detail)


def test_router_refusals_are_409_with_reason():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        asyncio.run(v1.router_route(v1.RouterRoute(
            task_class="bogus_class", strict=True), _=True))
    assert e.value.status_code == 409
    assert "unknown task_class" in str(e.value.detail)
