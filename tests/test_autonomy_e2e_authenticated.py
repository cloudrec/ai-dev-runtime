"""Stage 13: the autonomy loop end to end, unattended, through the AUTHENTICATED reviewer API
and the Stage 12 safety paths (stall → wake → tech-lead answer → idle window → resend).

One driver loop, no manual nudges: every iteration = engine tick → wake companion tick
(real delivery path) → reviewer turn over HTTP (real api/v1 router, real `_auth`, real
`_reviewer_principal`) → advance the ENGINE clock by a fixed step. Nothing is touched by
hand between iterations.

REAL here: everything test_autonomy_cycle_e2e lists as real (temp control-plane DB,
private tmux socket, contracts, git, wake_bridge, wake_routes, cdp_composer,
wake_companion.tick), plus the HTTP API with its real bearer/per-client-token auth
(test-only token + client-token file in tmp_path; no production secret), the stall alert,
the hold/idle-confirmation fence, the untrusted-text labels and the decision audit.
SIMULATED (never counts as live proof): the LLM agent (fixture script; scenario "hang"
then "correct"), the ChatGPT page (fake CDP session, live CDP walled off), the GPT
reviewer's judgment (a deterministic policy that only answers reviews whose wake was
verifiably delivered), and wall-clock time (the engine clock is advanced in steps so a
2 h stall timeout and a 60 s idle window elapse in seconds).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import time

import pytest

from core import task_contract as tc
from core import wake_bridge as wb
from core.fullcycle import engine as fe
from tests.test_autonomy_cycle_e2e import PROJECT_CHAT, cycle, h  # noqa: F401
from tests.test_fullcycle_stall import Clock

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SHARED = "sandbox-shared-runtime-token"           # test-only values
REVIEWER_TOKEN = "sandbox-reviewer-client-token"
STEP = 900                                        # engine-clock seconds per iteration


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_TOKEN", SHARED)
    tokens = tmp_path / "runtime-clients.json"
    tokens.write_text(json.dumps(
        {"sandbox-gpt": hashlib.sha256(REVIEWER_TOKEN.encode()).hexdigest()}))
    os.chmod(tokens, 0o600)
    monkeypatch.setenv("RUNTIME_CLIENT_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("FULLCYCLE_REVIEWERS", "api:client/sandbox-gpt")
    spec = importlib.util.spec_from_file_location("api_v1_stage13",
                                                  os.path.join(_ROOT, "api", "v1.py"))
    v1 = importlib.util.module_from_spec(spec)
    sys.modules["api_v1_stage13"] = v1
    spec.loader.exec_module(v1)                   # reads RUNTIME_TOKEN now (module level)
    assert v1._TOKEN == SHARED
    app = FastAPI()
    app.include_router(v1.router)                 # NO dependency override: real _auth
    return TestClient(app)


@pytest.fixture
def stalled_cycle(cycle, tmp_path, monkeypatch):
    """The sandbox of test_autonomy_cycle_e2e, with an agent whose first attempt at
    `add` hangs silently and whose second attempt is correct."""
    cycle["adapter"].kill()
    a, log = h.make_adapter(str(tmp_path), cycle["root"], {"add": ["hang", "correct"]})
    a.restart = lambda handle: pytest.fail("the engine must never restart on a stall")
    e = fe.Engine("tinycalc", a, max_attempts=3, holder="sandbox-13")
    monkeypatch.setattr(fe, "QUIET_CONFIRM_SECS", 60)   # the production default
    clock = Clock(e)
    yield {**cycle, "engine": e, "adapter": a, "log": log, "clock": clock}
    a.kill()


def _hdr(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def _reviewer_turn(c, api, seen):
    """The tech lead's side for one iteration: let the companion deliver what is pending,
    then answer, over HTTP, only reviews whose wake was verifiably delivered."""
    r = c["companion"].tick(wb)
    if r.get("acted") and r.get("ok"):
        seen["delivered"].add(r["event_id"])
    inbox = api.get("/api/v1/fullcycle/reviews", params={"project_id": "tinycalc"},
                    headers=_hdr(SHARED)).json()["reviews"]
    for rv in inbox:
        if rv["event_id"] not in seen["delivered"]:
            continue
        rid, p = rv["review_id"], rv["payload"]
        # an impostor holding the SHARED token and naming itself the reviewer: refused
        spoof = api.post(f"/api/v1/fullcycle/reviews/{rid}/decision",
                         json={"decision": "cancel" if rv["kind"] == "blocked" else "reject",
                               "idempotency_key": f"spoof:{rid}", "feedback": "x"},
                         headers=_hdr(SHARED, **{"X-Runtime-Actor": "client/sandbox-gpt"}))
        seen["spoof_status"].append(spoof.status_code)
        # whatever the agent wrote reaches the tech lead only fenced as untrusted data
        for field in ("summary", "question"):
            text = p.get(field) or ""
            if "agent_report" in p and p["agent_report"]["fields"].get(field):
                assert "<<<UNTRUSTED AGENT" in text, (field, text[:200])
        assert api.post(f"/api/v1/fullcycle/reviews/{rid}/ack",
                        headers=_hdr(REVIEWER_TOKEN)).status_code == 200
        if rv["kind"] == "project":
            body = {"decision": "add_tasks", "next_tasks": [h.CLI_TASK]}
        elif rv["kind"] == "blocked":
            body = {"decision": "answer", "feedback": "retry when the agent is idle"}
        else:
            body = {"decision": "approve"}
        res = api.post(f"/api/v1/fullcycle/reviews/{rid}/decision",
                       json={**body, "idempotency_key": f"gpt:{rid}"},
                       headers=_hdr(REVIEWER_TOKEN)).json()
        seen["decisions"].append((rv["kind"], p.get("stalled", ""), body["decision"],
                                  res.get("ok")))


def test_unattended_stall_recovery_to_project_done_via_authenticated_reviewer(
        stalled_cycle, api):
    c = stalled_cycle
    seen = {"delivered": set(), "decisions": [], "spoof_status": []}
    end, iterations, notes = time.time() + 120, 0, []
    while time.time() < end:
        iterations += 1
        notes += [n["what"] for n in c["engine"].tick().get("log", [])]
        _reviewer_turn(c, api, seen)
        st = c["engine"].status()
        if st["done"]:
            break
        c["clock"].offset += STEP
        c["sleep"](0.1)
    st = c["engine"].status()
    assert st["done"], (st, seen)

    # the path actually taken, in order
    assert [(t["key"], t["state"]) for t in st["tasks"]] == [
        ("add", tc.VERIFIED_DONE), ("mul", tc.VERIFIED_DONE), ("cli", tc.VERIFIED_DONE)]
    assert [(k, s, d, ok) for k, s, d, ok in seen["decisions"]] == [
        ("blocked", "attempt_timeout", "answer", True),       # stall → wake → TL answer
        ("review", "", "approve", True), ("review", "", "approve", True),
        ("project", "", "add_tasks", True), ("review", "", "approve", True)]
    sends = [(x["task_id"], x["attempt"]) for x in h.agent_log(c["log"])]
    assert sends == [("tinycalc:add", 1), ("tinycalc:add", 2), ("tinycalc:mul", 1),
                     ("tinycalc:cli", 1)]                     # one resend, nothing beside it
    # identity: every spoof refused, every decision bound to the authenticated principal
    assert seen["spoof_status"] and set(seen["spoof_status"]) == {403}
    conn, _ = fe.connect()
    try:
        by = {r[0] for r in conn.execute("SELECT decided_by FROM fc_review WHERE"
                                         " status='decided'")}
        audit = conn.execute("SELECT action, actor, outcome FROM fc_decision_audit"
                             " WHERE action='decide'").fetchall()
    finally:
        conn.close()
    assert by == {"api:client/sandbox-gpt"}
    assert {a for _, a, _ in audit} == {"api:client/sandbox-gpt"}
    assert all(o == "accepted" for _, _, o in audit) and len(audit) == 5
    assert fe.verify_decision_audit()["ok"]
    # delivery: every answered review's wake reached the project chat, verified, once
    assert {u for u, _ in c["browser"].submissions} == {PROJECT_CHAT}
    assert not wb.pending_wake().get("pending")
    assert iterations < 60
    # the Stage 12 safety path really ran: alert, fenced resend, idle window, one resend
    order = [n for n in notes if n in ("attempt_stalled", "idle_unconfirmed", "dispatched")]
    i = order.index("attempt_stalled")
    assert "idle_unconfirmed" in order[i:], order
    assert order[i:].index("idle_unconfirmed") < order[i:].index("dispatched"), order
    print(f"\nE2E iterations={iterations} sends={sends} decisions={seen['decisions']}"
          f" spoofs_refused={len(seen['spoof_status'])} audit_rows={len(audit)}"
          f" path={order}")
