"""The `owner_api` channel, which until now did not exist.

`provenance` names four trusted channels and nothing implemented any of them:
`owner_api` was a string in a set. The only production caller of
`record_owner_decision` is `access_recovery`, which writes one hardcoded answer. So
an owner decision could not be entered for an arbitrary gate, and nine open
`classify_scope` gates had no path to resolution at all.

What `authenticated=True` MEANS is the whole point, and it is what these tests pin:
not a developer asserting trust in code, but the fact that a request carried the
runtime credential and passed `_auth`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import types

import pytest

from api import v1
from core.control_plane import api as cp
from core.control_plane import provenance

TOKEN = "test-runtime-token"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(v1, "_TOKEN", TOKEN, raising=False)
    yield


def _req(method="bearer"):
    """A stand-in Request carrying only what the route reads: state.auth_method."""
    r = types.SimpleNamespace()
    r.state = types.SimpleNamespace(auth_method=method)
    return r


def _gate(agent="some-agent:0.0"):
    g = cp.open_gate(agent_id=agent, reason="unknown-scope agent",
                     kind="classify_scope", correlation_id=f"scope:{agent}")
    return g["id"] if isinstance(g, dict) else g


def _post(gate_ids, answer, method="bearer"):
    return asyncio.run(v1.record_owner_decision(
        v1.OwnerDecisionReq(gate_ids=gate_ids, answer=answer), _req(method), _=True))


# ── the channel now exists and actually resolves ────────────────────────────
def test_an_authenticated_decision_resolves_the_gate():
    g = _gate()
    body = _post([g], "keep as observe_only")
    assert body["answered"] == 1
    res = body["results"][0]
    assert res["resolved"] is True and res["trusted"] is True
    assert g not in {x["id"] for x in cp.get_open_gates()}


def test_the_decision_is_recorded_through_the_owner_api_channel():
    g = _gate()
    body = _post([g], "keep as observe_only")
    d = provenance.get_owner_decision(body["results"][0]["decision_id"])
    assert d["source_channel"] == "owner_api"
    assert d["authenticated"] is True
    assert d["consumption_state"] == "consumed"


def test_the_auth_method_is_recorded_so_hmac_stays_distinguishable():
    g = _gate()
    body = _post([g], "keep as observe_only", method="hmac")
    d = provenance.get_owner_decision(body["results"][0]["decision_id"])
    assert d["actor"] == "owner:hmac", "a signed decision must not look like a bearer one"


# ── the route cannot widen its own trust ────────────────────────────────────
def test_the_caller_cannot_choose_the_channel():
    """`source_channel` is fixed in the route, not a request field, so a caller
    cannot claim a channel the request did not come through."""
    assert "source_channel" not in v1.OwnerDecisionReq.model_fields
    assert "authenticated" not in v1.OwnerDecisionReq.model_fields


def test_an_empty_answer_is_refused():
    g = _gate()
    with pytest.raises(v1.HTTPException) as e:
        _post([g], "   ")
    assert e.value.status_code == 422
    assert g in {x["id"] for x in cp.get_open_gates()}, "the gate must survive a refused answer"


def test_no_gates_is_refused():
    with pytest.raises(v1.HTTPException) as e:
        _post([], "keep")
    assert e.value.status_code == 422


# ── replay and unknown gates ────────────────────────────────────────────────
def test_answering_twice_does_not_resolve_twice():
    g = _gate()
    assert _post([g], "keep")["answered"] == 1
    second = _post([g], "keep")
    assert second["answered"] == 0
    assert second["results"][0]["reason"] == "gate_not_open"


def test_an_unknown_gate_is_reported_not_invented():
    body = _post(["no-such-gate"], "keep")
    assert body["answered"] == 0 and body["results"][0]["reason"] == "gate_not_open"


def test_a_batch_answers_each_gate_with_its_own_decision():
    ids = [_gate(f"a{i}:0.0") for i in range(3)]
    body = _post(ids, "keep as observe_only")
    assert body["answered"] == 3
    assert len({x["decision_id"] for x in body["results"]}) == 3, \
        "each gate must get its own correlated decision"
    assert not [g for g in cp.get_open_gates() if g["id"] in ids]


# ── what "authenticated" is earned by: _auth itself ─────────────────────────
def _auth(authorization=None, x_runtime_timestamp=None, x_runtime_signature=None):
    """Called directly, so every Header param must be passed explicitly — FastAPI's
    `Header(None)` defaults are Header objects, not None, outside a request."""
    r = types.SimpleNamespace(state=types.SimpleNamespace())
    return asyncio.run(v1._auth(r, authorization=authorization,
                                x_runtime_timestamp=x_runtime_timestamp,
                                x_runtime_signature=x_runtime_signature)), r


def test_a_correct_bearer_token_authenticates():
    ok, r = _auth(authorization=f"Bearer {TOKEN}")
    assert ok is True and r.state.auth_method == "bearer"


def test_a_wrong_bearer_token_is_rejected():
    with pytest.raises(v1.HTTPException) as e:
        _auth(authorization="Bearer not-the-token")
    assert e.value.status_code in (401, 403)


def test_no_credential_at_all_is_rejected():
    with pytest.raises(v1.HTTPException):
        _auth()


def test_a_valid_hmac_signature_authenticates():
    ts = str(int(time.time()))
    sig = hmac.new(TOKEN.encode(), ts.encode(), hashlib.sha256).hexdigest()
    ok, r = _auth(x_runtime_timestamp=ts, x_runtime_signature=sig)
    assert ok is True and r.state.auth_method == "hmac"


def test_a_stale_signature_is_rejected_as_replay():
    ts = str(int(time.time()) - 10_000)
    sig = hmac.new(TOKEN.encode(), ts.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(v1.HTTPException) as e:
        _auth(x_runtime_timestamp=ts, x_runtime_signature=sig)
    assert e.value.status_code == 401
