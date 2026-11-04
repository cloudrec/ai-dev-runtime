"""The reviewer and signing paths over a real socket: uvicorn on loopback, test tokens in
tmp_path, tmp DBs. LOCAL INTEGRATION, not a live E2E: no ChatGPT, no agent, no route.

Closes what TestClient cannot show: that uvicorn itself hands the raw target to
_signed_target, and that shared credentials cannot act as a reviewer over HTTP.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

fastapi = pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")
from fastapi import FastAPI  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = "shared-test-token-not-a-secret"
REVIEWER = "reviewer-test-token-not-a-secret"
STRANGER = "stranger-test-token-not-a-secret"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_TOKEN", SHARED)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    tokens = tmp_path / "runtime-clients.json"
    tokens.write_text(json.dumps({"sandbox-gpt": hashlib.sha256(REVIEWER.encode()).hexdigest(),
                                  "stranger": hashlib.sha256(STRANGER.encode()).hexdigest()}))
    os.chmod(tokens, 0o600)
    monkeypatch.setenv("RUNTIME_CLIENT_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("FULLCYCLE_REVIEWERS", "api:client/sandbox-gpt")
    spec = importlib.util.spec_from_file_location("api_v1_http", os.path.join(ROOT, "api", "v1.py"))
    v1 = importlib.util.module_from_spec(spec)
    sys.modules["api_v1_http"] = v1
    spec.loader.exec_module(v1)
    app = FastAPI()
    app.include_router(v1.router)
    seen = []

    @app.get("/echo/{name:path}", dependencies=[fastapi.Depends(v1._auth)])
    def echo(name: str):
        seen.append(name)
        return {"name": name}

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
    yield f"http://127.0.0.1:{port}", v1, seen
    srv.should_exit = True
    t.join(5)


def _call(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _v2(v1, method, path, query="", body=b"", ts=None):
    ts = str(int(ts or time.time()))
    msg = v1.hmac_v2_message(ts, method, path, query, body)
    return {"X-Runtime-Timestamp": ts, "X-Runtime-Signature-Version": "2",
            "X-Runtime-Signature": hmac.new(SHARED.encode(), msg, hashlib.sha256).hexdigest()}


def test_uvicorn_hands_the_raw_target_to_the_signature(server):
    base, v1, seen = server
    path = "/echo/x%3Fevil"
    assert _call(base + path, headers=_v2(v1, "GET", path))[0] == 200
    assert seen == ["x?evil"]


def test_a_signature_for_the_decoded_prefix_does_not_cover_the_encoded_path(server):
    base, v1, seen = server
    assert _call(base + "/echo/x%3Fevil", headers=_v2(v1, "GET", "/echo/x"))[0] == 401
    assert seen == []


def test_a_signed_query_is_bound_over_a_socket(server):
    base, v1, _ = server
    headers = _v2(v1, "GET", "/echo/x", "a=1")
    assert _call(base + "/echo/x?a=2", headers=headers)[0] == 401
    assert _call(base + "/echo/x?a=1", headers=_v2(v1, "GET", "/echo/x", "a=1"))[0] == 200


def test_a_v2_signature_works_once_over_a_socket(server):
    base, v1, _ = server
    headers = _v2(v1, "GET", "/echo/once")
    assert _call(base + "/echo/once", headers=headers)[0] == 200
    assert _call(base + "/echo/once", headers=headers)[0] == 401


@pytest.mark.parametrize("token,actor,code", [
    (SHARED, "api:client/sandbox-gpt", 403), (STRANGER, "api:client/sandbox-gpt", 403),
    ("wrong", "api:client/sandbox-gpt", 401)])
def test_only_the_listed_client_token_reaches_the_review(server, token, actor, code):
    base, _, _ = server
    headers = {"Authorization": f"Bearer {token}", "X-Runtime-Actor": actor}
    assert _call(f"{base}/api/v1/fullcycle/reviews/1/ack", "POST", headers)[0] == code


def test_the_listed_client_token_gets_past_auth(server):
    base, _, _ = server
    status, body = _call(f"{base}/api/v1/fullcycle/reviews/1/ack", "POST",
                         {"Authorization": f"Bearer {REVIEWER}"})
    assert status == 400 and "no such review" in body
