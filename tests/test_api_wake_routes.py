"""The wake route registry is manageable from the chat: list, bind, resolve over the API.

The API layer must add nothing but transport — validation, idempotency and audit live in
core.wake_routes, so a route that the CLI would refuse is refused here too, with a 400 and
no mutation. The endpoint coroutines are exercised directly (the venv deliberately has no
HTTP test client); the OpenAPI surface is asserted from the app schema, which is what the
connector actually consumes.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException

from api import v1
from core import wake_routes as wr

MESS = "https://chatgpt.com/c/mess-work-chat"
PAY = "https://chatgpt.com/c/payments-work-chat"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


def _list():
    return asyncio.run(v1.list_wake_routes(_=True))


def _bind(route_key, url, note=""):
    req = v1.WakeRouteBindReq(route_key=route_key, conversation_url=url, note=note)
    return asyncio.run(v1.bind_wake_route(req, request=None, _=True))


def _resolve(**kw):
    return asyncio.run(v1.resolve_wake_route(_=True, **kw))


def test_list_starts_empty_and_names_the_fallback():
    body = _list()
    assert body["routes"] == [] and body["fallback_route"] == wr.FALLBACK_ROUTE


def test_a_valid_bind_is_applied_audited_and_listed():
    body = _bind("mess", MESS, note="MESS work chat")
    assert body["ok"] is True and body["action"] == "bind" and body["previous"] is None
    listed = _list()["routes"]
    assert [x["conversation"] for x in listed if x["route_key"] == "mess"] == [MESS]
    # the caller identity reached the audit trail (degraded, never absent)
    assert wr.get_route("mess")["bound_by"].startswith("api:")


def test_rebind_returns_previous_and_is_idempotent():
    _bind("mess", MESS)
    r = _bind("mess", PAY)
    assert r["action"] == "rebind" and r["previous"] == MESS
    assert _bind("mess", PAY)["action"] == "unchanged"


def test_an_invalid_url_is_a_400_with_no_mutation():
    for bad in ("https://chat.com/c/abc", "https://chatgpt.com/gpts", "not-a-url"):
        with pytest.raises(HTTPException) as e:
            _bind("mess", bad)
        assert e.value.status_code == 400
        assert e.value.detail["reason"] == "not_a_conversation_url"
    assert _list()["routes"] == []


def test_an_invalid_route_key_is_a_400_with_no_mutation():
    with pytest.raises(HTTPException) as e:
        _bind("MESS CHAT", MESS)
    assert e.value.status_code == 400
    assert e.value.detail["reason"] == "invalid_route_key"
    assert _list()["routes"] == []


def test_rebinding_one_route_does_not_alter_another():
    _bind("mess", MESS)
    _bind("payment-orchestrator", PAY)
    _bind("mess", "https://chatgpt.com/c/mess-rotated")
    routes = {x["route_key"]: x["conversation"] for x in _list()["routes"]}
    assert routes["payment-orchestrator"] == PAY
    assert routes["mess"] == "https://chatgpt.com/c/mess-rotated"


def test_resolve_is_read_only_and_names_its_reason():
    _bind("mess", MESS)
    r = _resolve(project_id="mess")
    assert r["bound"] is True and r["conversation"] == MESS
    assert r["route_reason"] == "explicit_route"
    unbound = _resolve(project_id="nowhere")
    assert unbound["bound"] is False and unbound["reason"] == "no_route_bound"
    assert _list()["routes"] != []          # resolving mutated nothing away


def test_the_tools_are_exposed_under_their_operation_ids():
    app = FastAPI()
    app.include_router(v1.router)
    ids = set()
    for path in app.openapi()["paths"].values():
        for op in path.values():
            ids.add(op.get("operationId"))
    for tool in ("list_wake_routes", "bind_wake_route", "resolve_wake_route"):
        assert tool in ids
