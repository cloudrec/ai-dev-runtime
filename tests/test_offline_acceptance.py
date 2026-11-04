"""OFFLINE acceptance harness for the autonomous development cycle (2026-11-04).

Every scenario drives the whole loop UNATTENDED, through one driver:

    spec → dispatch → agent completion → engine verification (git + checks) →
    review event → wake decision → companion delivery (verified) → reviewer decision over
    the AUTHENTICATED HTTP API → reject/fix or approve → next-task scheduling → project done

Scenarios: reject → fix → approve; crash → restart → retry; duplicate wakes and
duplicate decisions; a stalled agent (alert → answer → idle window → one resend);
an owner gate that stops the loop until the owner acts; API-credit mode default OFF.
Every scenario also checks: every decision bound to the authenticated principal, every
shared-token spoof refused, the decision audit chain intact, no credential variable in
any recorded event, and that the live control-plane DB was never touched.

REAL: engine, contracts, git, private tmux, temp control-plane + agent-control DBs,
wake_bridge, wake_routes, wake_companion.tick → cdp_composer (claim/latch/verify),
api/v1 with its real `_auth` (test-only token files in tmp_path), the decision audit.
SIMULATED (this is NOT live proof): the LLM agent (fixture script), the ChatGPT page
(fake CDP; live CDP walled off), the reviewer's judgment (a deterministic policy), and —
in the stall scenario only — wall-clock time (engine clock advanced in steps). No paid
API, no network, no production file is written.

Run: python3 -m pytest -q -p no:cacheprovider tests/test_offline_acceptance.py
     python3 tools/offline_acceptance.py --out reports/offline_acceptance   (JSON + MD)
"""
from __future__ import annotations

import json
import os
import time

import pytest

from core import agent_auth_mode as aam
from core import task_contract as tc
from core import wake_bridge as wb
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from tests.test_autonomy_cycle_e2e import OWNEROS_CHAT, PROJECT_CHAT, cycle, h  # noqa: F401
from tests.test_autonomy_e2e_authenticated import (REVIEWER_TOKEN, SHARED, _hdr,  # noqa: F401
                                                   api)
from tests.test_fullcycle_stall import Clock

LIVE_DIR = "/root/ai-dev-runtime"


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    """No credential in the environment; no SQLite DB under the live tree is ever opened
    (every sqlite3.connect is recorded — deterministic, unlike mtimes of DBs PROD itself
    writes every few seconds)."""
    import sqlite3
    for v in aam.API_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    opened, real = [], sqlite3.connect

    def recording(database, *a, **k):
        opened.append(str(database))
        return real(database, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", recording)
    yield opened
    live = [p for p in opened if p != ":memory:" and
            os.path.realpath(p).startswith(LIVE_DIR + os.sep)]
    assert live == [], live


@pytest.fixture
def accept(cycle, tmp_path, monkeypatch):
    """A fresh agent with the given per-task behaviours, on the cycle sandbox."""
    made = []

    def make(behaviours, *, quiet_window=0, allow_restart=True):
        cycle["adapter"].kill()
        a, log = h.make_adapter(str(tmp_path), cycle["root"], behaviours)
        if not allow_restart:
            a.restart = lambda handle: pytest.fail("the engine must never restart here")
        e = fe.Engine("tinycalc", a, max_attempts=3, holder=f"accept-{len(made)}")
        monkeypatch.setattr(fe, "QUIET_CONFIRM_SECS", quiet_window)
        made.append(a)
        return {**cycle, "engine": e, "adapter": a, "log": log, "clock": Clock(e)}
    yield make
    for a in made:
        a.kill()


# ── the unattended driver ─────────────────────────────────────────────────────────
class Reviewer:
    """Deterministic tech-lead policy, acting ONLY on reviews whose wake was verifiably
    delivered, ONLY through the authenticated API. `reject_first` names task keys whose
    first review is rejected (the fix loop); `duplicate` submits every decision twice
    with the same key and fires the companion twice (duplicate wake)."""

    def __init__(self, api, *, reject_first=(), duplicate=False):
        self.api, self.reject_first, self.duplicate = api, set(reject_first), duplicate
        self.delivered, self.decisions, self.spoofs, self.dups = set(), [], [], []
        self._rejected = set()

    def turn(self, c):
        for _ in range(2 if self.duplicate else 1):
            r = c["companion"].tick(wb)
            if r.get("acted") and r.get("ok"):
                self.delivered.add(r["event_id"])
        inbox = self.api.get("/api/v1/fullcycle/reviews", params={"project_id": "tinycalc"},
                             headers=_hdr(SHARED)).json()["reviews"]
        for rv in inbox:
            if rv["event_id"] not in self.delivered:
                continue
            rid, p = rv["review_id"], rv["payload"]
            self.spoofs.append(self.api.post(
                f"/api/v1/fullcycle/reviews/{rid}/decision",
                json={"decision": "cancel" if rv["kind"] == "blocked" else "approve",
                      "idempotency_key": f"spoof:{rid}", "feedback": "x"},
                headers=_hdr(SHARED, **{"X-Runtime-Actor": "client/sandbox-gpt"})
            ).status_code)
            task = p.get("task") or rv["task_id"].split(":", 1)[1]
            if rv["kind"] == "project":
                body = {"decision": "add_tasks", "next_tasks": [h.CLI_TASK]}
            elif rv["kind"] == "blocked":
                body = {"decision": "answer", "feedback": "retry when the agent is idle"}
            elif task in self.reject_first and task not in self._rejected:
                self._rejected.add(task)
                body = {"decision": "reject", "feedback": "add a docstring and re-run"}
            else:
                body = {"decision": "approve"}
            self.api.post(f"/api/v1/fullcycle/reviews/{rid}/ack", headers=_hdr(REVIEWER_TOKEN))
            for n in range(2 if self.duplicate else 1):
                res = self.api.post(f"/api/v1/fullcycle/reviews/{rid}/decision",
                                    json={**body, "idempotency_key": f"gpt:{rid}"},
                                    headers=_hdr(REVIEWER_TOKEN)).json()
                if n:
                    self.dups.append(res)
                else:
                    self.decisions.append((task, rv["kind"], body["decision"], res.get("ok")))


def drive(c, reviewer, *, until=lambda st: st["done"], step=0.0, budget=120, max_iter=200):
    notes, it, end = [], 0, time.time() + budget
    while time.time() < end and it < max_iter:
        it += 1
        notes += [n["what"] for n in c["engine"].tick().get("log", [])]
        reviewer.turn(c)
        st = c["engine"].status()
        if until(st):
            return st, notes, it
        c["clock"].offset += step
        c["sleep"](0.1)
    return c["engine"].status(), notes, it


def sends(c):
    return [(x["task_id"].split(":", 1)[1], x["attempt"]) for x in h.agent_log(c["log"])]


def assert_invariants(c, reviewer, *, decisions):
    """Identity, audit, delivery and credential hygiene — for every scenario."""
    assert reviewer.spoofs and set(reviewer.spoofs) == {403}
    conn, _ = fe.connect()
    try:
        by = {r[0] for r in conn.execute("SELECT decided_by FROM fc_review WHERE"
                                         " status='decided'")}
        audit = conn.execute("SELECT actor, outcome FROM fc_decision_audit WHERE"
                             " action='decide' AND outcome='accepted'").fetchall()
        events = " ".join(r[0] or "" for r in conn.execute("SELECT payload FROM event"))
    finally:
        conn.close()
    assert by == {"api:client/sandbox-gpt"}
    assert len(audit) == decisions and {a for a, _ in audit} == {"api:client/sandbox-gpt"}
    assert fe.verify_decision_audit()["ok"]
    for v in aam.API_ENV_VARS:
        assert v not in events
    assert not wb.pending_wake().get("pending")
    assert {u for u, _ in c["browser"].submissions} <= {PROJECT_CHAT, OWNEROS_CHAT}


def _done_tasks(st):
    return [(t["key"], t["state"]) for t in st["tasks"]]


DONE3 = [("add", tc.VERIFIED_DONE), ("mul", tc.VERIFIED_DONE), ("cli", tc.VERIFIED_DONE)]


# ── scenarios ─────────────────────────────────────────────────────────────────────
def test_A_spec_to_done_with_reject_fix_approve(accept, api, isolation):
    c = accept({})
    rv = Reviewer(api, reject_first={"add"})
    st, notes, it = drive(c, rv)
    assert st["done"] and _done_tasks(st) == DONE3
    assert [(t, k, d) for t, k, d, ok in rv.decisions if ok] == [
        ("add", "review", "reject"), ("add", "review", "approve"), ("mul", "review", "approve"),
        ("__project__", "project", "add_tasks"), ("cli", "review", "approve")]
    assert sends(c) == [("add", 1), ("add", 2), ("mul", 1), ("cli", 1)]
    fix = [x for x in h.agent_log(c["log"]) if x["attempt"] == 2][0]
    assert "Reviewer rejected: add a docstring" in fix["feedback"]   # the fix loop
    assert_invariants(c, rv, decisions=5)
    # the isolation guard saw this scenario's own (temp) DBs: it is not vacuous
    assert any(p.startswith(str(c["tmp"])) for p in isolation)


def test_B_crash_restart_retry(accept, api):
    c = accept({"add": ["crash", "correct"]})
    rv = Reviewer(api)
    st, notes, it = drive(c, rv)
    assert st["done"] and _done_tasks(st) == DONE3
    assert "agent_crashed" in notes and "agent_restart" in notes
    assert sends(c) == [("add", 1), ("add", 2), ("mul", 1), ("cli", 1)]
    assert_invariants(c, rv, decisions=4)


def test_C_duplicate_wakes_and_decisions_change_nothing(accept, api):
    c = accept({})
    rv = Reviewer(api, duplicate=True)
    st, notes, it = drive(c, rv)
    assert st["done"] and _done_tasks(st) == DONE3
    assert rv.dups and all(d.get("duplicate") is True for d in rv.dups)
    assert sends(c) == [("add", 1), ("mul", 1), ("cli", 1)]            # one prompt per task
    conn, _ = cp._c(None)
    try:
        delivered = conn.execute("SELECT event_id, COUNT(*) FROM wake_delivery WHERE"
                                 " delivered=1 GROUP BY event_id").fetchall()
    finally:
        conn.close()
    assert delivered and all(n == 1 for _, n in delivered)             # each wake once
    assert_invariants(c, rv, decisions=4)


def test_D_stalled_agent_alert_answer_idle_window_single_resend(accept, api):
    c = accept({"add": ["hang", "correct"]}, quiet_window=60, allow_restart=False)
    rv = Reviewer(api)
    st, notes, it = drive(c, rv, step=900)
    assert st["done"] and _done_tasks(st) == DONE3
    assert rv.decisions[0][1:] == ("blocked", "answer", True)
    order = [n for n in notes if n in ("attempt_stalled", "idle_unconfirmed", "dispatched")]
    i = order.index("attempt_stalled")
    assert order[i:].index("idle_unconfirmed") < order[i:].index("dispatched")
    assert sends(c) == [("add", 1), ("add", 2), ("mul", 1), ("cli", 1)]
    assert_invariants(c, rv, decisions=5)


def test_E_owner_gate_stops_the_loop_until_the_owner_acts(accept, api):
    c = accept({"add": ["permission"]})
    rv = Reviewer(api)
    st, notes, it = drive(c, rv, until=lambda s: False, budget=8, max_iter=25)
    t = next(x for x in st["tasks"] if x["key"] == "add")
    assert t["state"] == tc.OWNER_DECISION and t["last_reason"] == "agent_permission_prompt"
    assert not st["done"] and rv.decisions == []                       # nothing decided
    assert sends(c) == [("add", 1)]                                    # nothing else sent
    owner = [json.loads(p) for _, _, p, _ in _owner_events()]
    assert [o["reason"] for o in owner] == ["agent_permission_prompt"]  # raised once
    # the OWNER acts: answers the dialog in the pane and resumes the gate
    a = c["adapter"]
    a._tmux("send-keys", "-t", a.target, "1", "Enter")
    task = next(x for x in c["engine"]._tasks() if x["key"] == "add")
    cp.answer_gate(task["gate_id"], "resume")
    st, notes, it = drive(c, rv)
    assert st["done"] and _done_tasks(st) == DONE3
    assert sends(c) == [("add", 1), ("mul", 1), ("cli", 1)]            # same attempt kept
    assert_invariants(c, rv, decisions=4)


def _owner_events():
    conn, _ = cp._c(None)
    try:
        return conn.execute("SELECT id, type, payload, owner_action_required FROM event"
                            " WHERE source='fullcycle' AND type=? ORDER BY id",
                            (fe.OWNER_EVENT,)).fetchall()
    finally:
        conn.close()


def test_F_api_credit_mode_is_off_by_default():
    cfg = aam.load_config(aam._CONFIG)
    assert cfg["enabled"] is False and cfg["agents"] == {}
    r = aam.resolve("any-agent", "/tmp", requested="api", config=cfg,
                    live_sessions=lambda: set())
    assert r == {"ok": False, "reason": "api_mode_disabled"}
    sub = aam.resolve("any-agent", "/tmp")
    assert sub["mode"] == "subscription" and sub["argv"][-1] == "claude"
    for v in aam.API_ENV_VARS:
        assert v in sub["argv"]                                        # stripped, not passed
