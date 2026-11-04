"""The seo connector's real runtime_client against the real runtime API over a socket:
uvicorn on loopback, tmp DBs, test tokens, one seeded review. LOCAL INTEGRATION, not a live
E2E: no ChatGPT, no agent, no MCP layer. Skipped when the seo branch is not on this host.

Point SEO_BACKEND at the backend dir of the seo branch that has RUNTIME_FULLCYCLE_TOKEN.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import sys
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402

from core.fullcycle import engine as fe  # noqa: E402
from core.fullcycle import spec as specs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEO_CLIENT = os.path.join(os.environ.get("SEO_BACKEND", "/opt/seo-fullcycle-token/backend"),
                          "services", "runtime_client.py")
SHARED = "shared-test-token-not-a-secret"
REVIEWER = "reviewer-test-token-not-a-secret"
STRANGER = "stranger-test-token-not-a-secret"
ACTOR = "api:client/sandbox-gpt"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # pydantic resolves annotations through it
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seo(tmp_path, monkeypatch):
    if not os.path.exists(SEO_CLIENT):
        pytest.skip("seo branch not on this host")
    monkeypatch.setenv("RUNTIME_TOKEN", SHARED)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    tokens = tmp_path / "runtime-clients.json"
    tokens.write_text(json.dumps({"sandbox-gpt": hashlib.sha256(REVIEWER.encode()).hexdigest(),
                                  "stranger": hashlib.sha256(STRANGER.encode()).hexdigest()}))
    os.chmod(tokens, 0o600)
    monkeypatch.setenv("RUNTIME_CLIENT_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("FULLCYCLE_REVIEWERS", ACTOR)
    v1 = _load("api_v1_seo_roundtrip", os.path.join(ROOT, "api", "v1.py"))
    app = FastAPI()
    app.include_router(v1.router)
    specs.create_project("demo", name="demo", root=str(tmp_path), executor="fixture")
    specs.put_spec("demo", {"title": "d", "goal": "g", "acceptance_criteria": ["ok"]}, by="t")
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_review (project_id, task_id, attempt, kind, payload, created_ts)"
                 " VALUES ('demo', 'demo:a', 1, 'review', '{\"task\": \"a\"}', 0)")
    conn.commit()
    conn.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    assert srv.started
    rc = _load("seo_runtime_client_roundtrip", SEO_CLIENT)
    rc.RUNTIME_URL = f"http://127.0.0.1:{port}/api/v1"
    rc.RUNTIME_TOKEN = SHARED
    rc.RUNTIME_FULLCYCLE_TOKEN = REVIEWER
    yield rc
    srv.should_exit = True
    t.join(5)
    sys.modules.pop("seo_runtime_client_roundtrip", None)


def _audit_rows():
    conn, _ = fe.connect()
    try:
        return conn.execute("SELECT action, actor, auth FROM fc_decision_audit").fetchall()
    finally:
        conn.close()


def _decide(rc, key):
    return asyncio.run(rc.fullcycle_post("/reviews/1/decision",
                                         {"decision": "approve", "idempotency_key": key}))


def test_the_listed_client_lists_acks_and_decides_and_the_audit_names_it(seo):
    listed = asyncio.run(seo.fullcycle_get("/reviews"))
    assert [r["review_id"] for r in listed["reviews"]] == [1]
    assert asyncio.run(seo.fullcycle_post("/reviews/1/ack", {}))["acked_by"] == ACTOR
    assert _decide(seo, "key-12345678")["ok"] is True
    rows = _audit_rows()
    assert rows and all(r[1] == ACTOR and r[2] == "client_token" for r in rows)


def test_a_replayed_key_changes_nothing_and_a_second_key_is_refused(seo):
    assert _decide(seo, "key-12345678")["duplicate"] is False
    assert _decide(seo, "key-12345678")["duplicate"] is True
    assert _decide(seo, "key-87654321")["reason"] == "review_already_decided"


@pytest.mark.parametrize("token,code,needle", [
    (SHARED, 403, "repeats RUNTIME_TOKEN"),
    ("", 403, "RUNTIME_FULLCYCLE_TOKEN is not set"),
    (STRANGER, 403, "not listed in FULLCYCLE_REVIEWERS"),
    ("unknown-test-token", 401, "did not accept RUNTIME_FULLCYCLE_TOKEN"),
])
def test_nobody_else_decides_and_each_refusal_names_the_side_to_fix(seo, token, code, needle):
    seo.RUNTIME_FULLCYCLE_TOKEN = token
    r = _decide(seo, "key-12345678")
    assert r["status_code"] == code and needle in r["detail"]
    assert _audit_rows() == []


def test_a_refused_client_token_does_not_fall_back_for_reads(seo):
    seo.RUNTIME_FULLCYCLE_TOKEN = "unknown-test-token"
    assert asyncio.run(seo.fullcycle_get("/reviews"))["status_code"] == 401
