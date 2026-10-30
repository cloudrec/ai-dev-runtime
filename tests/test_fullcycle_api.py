"""The GPT tech lead's HTTP surface for the full-cycle inbox (api/v1 /fullcycle/*).
Auth is the router's normal bearer dependency, overridden here; the routes are thin
wrappers, so this proves the wiring and the error mapping, not the engine."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.fullcycle import engine as fe  # noqa: E402
from core.fullcycle import spec as specs  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from api import v1
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._auth] = lambda: True
    specs.create_project("demo", name="demo", root=str(tmp_path), executor="fixture")
    specs.put_spec("demo", {"title": "d", "goal": "g", "acceptance_criteria": ["ok"]}, by="t")
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_review (project_id, task_id, attempt, kind, payload, created_ts)"
                 " VALUES ('demo', 'demo:a', 1, 'review', '{\"task\": \"a\"}', 0)")
    conn.commit()
    conn.close()
    return TestClient(app)


def test_inbox_ack_and_an_idempotent_decision(client):
    (rv,) = client.get("/api/v1/fullcycle/reviews", params={"project_id": "demo"}).json()["reviews"]
    assert rv["kind"] == "review" and rv["decisions"] == ["approve", "reject"]
    hdr = {"X-Runtime-Actor": "chatgpt-tech-lead"}
    assert client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/ack", headers=hdr).json()["acked_by"]
    body = {"decision": "approve", "idempotency_key": "gpt-1"}
    first = client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/decision", json=body, headers=hdr)
    again = client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/decision", json=body, headers=hdr)
    assert first.json()["ok"] is True and again.json()["duplicate"] is True
    assert client.get("/api/v1/fullcycle/reviews").json()["reviews"] == []


def test_bad_decisions_are_400_not_500(client):
    rid = client.get("/api/v1/fullcycle/reviews").json()["reviews"][0]["review_id"]
    url = f"/api/v1/fullcycle/reviews/{rid}/decision"
    assert client.post(url, json={"decision": "deploy", "idempotency_key": "k"}).status_code == 400
    assert client.post(url, json={"decision": "reject", "idempotency_key": "k2"}).status_code == 400
    bad_task = {"decision": "approve", "idempotency_key": "k3",
                "next_tasks": [{"key": "x", "title": "x", "goal": "g",
                                "acceptance_criteria": ["ok"], "push_allowed": True}]}
    r = client.post(url, json=bad_task)
    assert r.status_code == 400 and "unknown field" in r.json()["detail"]
    assert client.post("/api/v1/fullcycle/reviews/999/decision",
                       json={"decision": "approve", "idempotency_key": "k4"}).status_code == 400


def test_project_status(client):
    st = client.get("/api/v1/fullcycle/projects/demo").json()
    assert st["spec_version"] == 1 and st["done"] is False and len(st["pending_reviews"]) == 1
    assert client.get("/api/v1/fullcycle/projects/nope").status_code == 400
