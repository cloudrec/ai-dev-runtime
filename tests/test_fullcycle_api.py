"""The GPT tech lead's HTTP surface for the full-cycle inbox (api/v1 /fullcycle/*).
Auth is the router's normal bearer dependency, overridden here; the routes are thin
wrappers, so this proves the wiring and the error mapping, not the engine."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.fullcycle import engine as fe  # noqa: E402
from core.fullcycle import spec as specs  # noqa: E402


# The real _auth is replaced by one that authenticates exactly like it does: a per-client
# token yields auth_method "client" + client_id; the shared token yields "bearer". The
# test header stands in for WHICH token was presented, never for a declared name.
REVIEWER = {"X-Test-Token": "client:gpt-reviewer"}
SHARED_TOKEN = {"X-Test-Token": "shared"}


def _fake_auth(request: Request) -> bool:
    tok = request.headers.get("x-test-token", "")
    if tok.startswith("client:"):
        request.state.auth_method, request.state.client_id = "client", tok[7:]
    elif tok in ("shared", ""):                     # (no header: the shared token)
        request.state.auth_method = "bearer"
    else:
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    # a declared-name entry on the allowlist must be ignored (it can be typed by anyone
    # holding the shared token); only the authenticated client principal counts
    monkeypatch.setenv("FULLCYCLE_REVIEWERS",
                       "api:client/gpt-reviewer,api:bearer/mcp-token-3,api:unknown/mcp-token-3")
    # load THIS tree's api/v1.py: other test modules put the live tree on sys.path at
    # collection time, and a plain `from api import v1` would then test the live file
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api", "v1.py")
    spec = importlib.util.spec_from_file_location("api_v1_under_test", path)
    v1 = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["api_v1_under_test"] = v1          # pydantic resolves annotations through it
    spec.loader.exec_module(v1)
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._auth] = _fake_auth
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
    hdr = REVIEWER
    assert client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/ack", headers=hdr).json()["acked_by"]
    body = {"decision": "approve", "idempotency_key": "gpt-1"}
    first = client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/decision", json=body, headers=hdr)
    again = client.post(f"/api/v1/fullcycle/reviews/{rv['review_id']}/decision", json=body, headers=hdr)
    assert first.json()["ok"] is True and again.json()["duplicate"] is True
    assert client.get("/api/v1/fullcycle/reviews").json()["reviews"] == []


def test_only_the_authorized_reviewer_may_ack_or_decide(client):
    rid = client.get("/api/v1/fullcycle/reviews", headers=SHARED_TOKEN).json()["reviews"][0]["review_id"]
    body = {"decision": "approve", "idempotency_key": "intruder-1"}
    spoofs = (  # shared token + any declared name, incl. the allowlisted ones, and other clients
        {**SHARED_TOKEN}, {**SHARED_TOKEN, "X-Runtime-Actor": "mcp-token-3"},
        {**SHARED_TOKEN, "X-Runtime-Actor": "client/gpt-reviewer"},
        {**SHARED_TOKEN, "X-Runtime-Actor": "owner"},
        {"X-Test-Token": "client:someone-else"}, {"X-Test-Token": "client:"})
    for hdr in spoofs:
        assert client.post(f"/api/v1/fullcycle/reviews/{rid}/decision", json=body,
                           headers=hdr).status_code == 403
        assert client.post(f"/api/v1/fullcycle/reviews/{rid}/ack", headers=hdr).status_code == 403
    (rv,) = client.get("/api/v1/fullcycle/reviews").json()["reviews"]
    assert rv["acked"] is False                              # nothing was recorded
    assert client.post(f"/api/v1/fullcycle/reviews/{rid}/decision", json=body,
                       headers=REVIEWER).json()["ok"] is True


def test_bad_decisions_are_400_not_500(client):
    rid = client.get("/api/v1/fullcycle/reviews").json()["reviews"][0]["review_id"]
    url = f"/api/v1/fullcycle/reviews/{rid}/decision"
    assert client.post(url, json={"decision": "deploy", "idempotency_key": "k"},
                       headers=REVIEWER).status_code == 400
    assert client.post(url, json={"decision": "reject", "idempotency_key": "k2"},
                       headers=REVIEWER).status_code == 400
    bad_task = {"decision": "approve", "idempotency_key": "k3",
                "next_tasks": [{"key": "x", "title": "x", "goal": "g",
                                "acceptance_criteria": ["ok"], "push_allowed": True}]}
    r = client.post(url, json=bad_task, headers=REVIEWER)
    assert r.status_code == 400 and "unknown field" in r.json()["detail"]
    assert client.post("/api/v1/fullcycle/reviews/999/decision",
                       json={"decision": "approve", "idempotency_key": "k4"},
                       headers=REVIEWER).status_code == 400


def test_project_status(client):
    st = client.get("/api/v1/fullcycle/projects/demo").json()
    assert st["spec_version"] == 1 and st["done"] is False and len(st["pending_reviews"]) == 1
    assert client.get("/api/v1/fullcycle/projects/nope").status_code == 400


def test_decisions_are_audited_with_the_authenticated_principal(client):
    rid = client.get("/api/v1/fullcycle/reviews").json()["reviews"][0]["review_id"]
    url = f"/api/v1/fullcycle/reviews/{rid}/decision"
    # a declared name that LOOKS like the reviewer, and an attempt to rename oneself
    assert client.post(url, json={"decision": "approve", "idempotency_key": "s1"},
                       headers={**SHARED_TOKEN, "X-Runtime-Actor": "client/gpt-reviewer"}
                       ).status_code == 403
    r = client.post(url, json={"decision": "approve", "idempotency_key": "g1"},
                    headers={**REVIEWER, "X-Runtime-Actor": "owner"})
    assert r.json()["ok"] is True
    conn, _ = fe.connect()
    try:
        (by,) = conn.execute("SELECT decided_by FROM fc_review WHERE id=?", (rid,)).fetchone()
        rows = conn.execute("SELECT action, actor, auth, outcome, decision_key FROM"
                            " fc_decision_audit ORDER BY seq").fetchall()
    finally:
        conn.close()
    assert by == "api:client/gpt-reviewer"                 # the header changed nothing
    assert rows == [("decide", "api:client/gpt-reviewer", "client_token", "accepted", "g1")]
    assert fe.verify_decision_audit()["ok"]


def test_no_reviewer_is_authorized_by_default(client, monkeypatch):
    monkeypatch.delenv("FULLCYCLE_REVIEWERS")
    rid = client.get("/api/v1/fullcycle/reviews").json()["reviews"][0]["review_id"]
    assert client.post(f"/api/v1/fullcycle/reviews/{rid}/decision",
                       json={"decision": "approve", "idempotency_key": "d1"},
                       headers=REVIEWER).status_code == 403
