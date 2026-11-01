"""Stage 14 gaps left unproven by the Stage 12/13 tests (reports/
OWNER_OS_AUTONOMY_STAGE14_AUDIT_READINESS_2026-11-01.md §2):

  Q2  the attempt number when the owner resumes a hold whose resend was prepared but never
      delivered, and that an owner attestation never carries over to a later stall;
  Q6  the HMAC auth path cannot authorize a full-cycle decision either;
  Q7  the additive migration on a populated pre-branch DB with a concurrent reader;
  and the CDP_PORT knob that lets an isolated live test use its own browser.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import sys
import time

import pytest

from core import task_contract as tc
from core.control_plane import api as cp
from core.fullcycle import engine as fe
from tests.test_fullcycle_e2e import _events, fc, h  # noqa: F401
from tests.test_fullcycle_stall import HOUR2, _answer, _hung_add, _stall_reviews

_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _task(e, key="add"):
    return next(t for t in e._tasks() if t["key"] == key)


def _hold_gates():
    return [json.loads(p) for _, _, p, _ in _events(fe.OWNER_EVENT)
            if json.loads(p).get("reason", "").startswith(fe.HOLD_GATE_PREFIX)]


# ── Q2 ─────────────────────────────────────────────────────────────────────────
def _escalated_after_refused_send(fc, monkeypatch, behaviours=None):
    """Stall → answer → fence opens → the send is refused busy (prepared, never
    delivered) → the agent looks busy → the hold escalates to the owner."""
    e, a, log, root, clock = _hung_add(fc, behaviours)
    quiet = {"v": True}
    monkeypatch.setattr(a, "quiescent", lambda handle: quiet["v"])
    real_submit = a.submit
    refuse = {"on": True}

    def submit(env, key):
        if refuse["on"]:
            return {"ok": False, "already": False, "reason": "refused:working",
                    "handle": {"task_id": env.task_id, "attempt": env.attempt}}
        return real_submit(env, key)
    monkeypatch.setattr(a, "submit", submit)
    clock.offset = HOUR2 + 1
    e.tick()
    assert _answer(e, "answer", key="q2-a")["ok"]
    e.tick()                                         # prepared attempt 2, refused busy
    t = _task(e)
    assert t["attempt"] == 2 and t["handle"]["prev"]["attempt"] == 1
    quiet["v"] = False
    e.tick()                                         # hold starts
    clock.offset += fe.STALL_HOLD_TIMEOUT + 1
    e.tick()
    assert len(_hold_gates()) == 1
    return e, a, log, clock, quiet, refuse


def test_q2_owner_resume_after_an_undelivered_send_keeps_the_attempt_number(fc, monkeypatch):
    e, a, log, clock, quiet, refuse = _escalated_after_refused_send(fc, monkeypatch)
    refuse["on"] = False
    cp.answer_gate(_task(e)["gate_id"], "resume")
    for _ in range(3):
        e.tick()
    assert [x["attempt"] for x in h.agent_log(log)] == [1, 2]    # 2, not 3
    assert _task(e)["attempt"] == 2


def test_q2_an_owner_attestation_never_covers_a_later_stall(fc, monkeypatch):
    e, a, log, clock, quiet, refuse = _escalated_after_refused_send(
        fc, monkeypatch, {"add": ["hang", "hang", "correct"]})
    refuse["on"] = False
    cp.answer_gate(_task(e)["gate_id"], "resume")
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 2, 15)
    clock.offset += HOUR2 + 1                        # attempt 2 stalls too
    e.tick()
    (rv,) = _stall_reviews(e)
    assert rv["attempt"] == 2
    fe.ack_review(rv["review_id"], by="stub-gpt")
    assert fe.submit_decision(rv["review_id"], "answer", key="q2-b", by="stub-gpt",
                              feedback="again")["ok"]
    for _ in range(4):                               # still busy: the old attestation
        e.tick()                                     # must not open this fence
    assert [x["attempt"] for x in h.agent_log(log)] == [1, 2]
    clock.offset += fe.STALL_HOLD_TIMEOUT + 1
    e.tick()
    assert len(_hold_gates()) == 2                   # a NEW owner gate for the new review


# ── Q6 ─────────────────────────────────────────────────────────────────────────
@pytest.fixture
def hmac_api(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("RUNTIME_TOKEN", "sandbox-shared")
    monkeypatch.setenv("FULLCYCLE_REVIEWERS", "api:client/sandbox-gpt,api:hmac/sandbox-gpt")
    spec = importlib.util.spec_from_file_location("api_v1_stage14",
                                                  os.path.join(_ROOT, "api", "v1.py"))
    v1 = importlib.util.module_from_spec(spec)
    sys.modules["api_v1_stage14"] = v1
    spec.loader.exec_module(v1)
    app = fastapi.FastAPI()
    app.include_router(v1.router)
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_review (project_id, task_id, attempt, kind, payload, created_ts)"
                 " VALUES ('demo', 'demo:a', 1, 'review', '{}', 0)")
    rid = conn.execute("SELECT MAX(id) FROM fc_review").fetchone()[0]
    conn.commit()
    conn.close()
    return TestClient(app), rid


def _signed(**extra):
    ts = str(int(time.time()))
    sig = hmac.new(b"sandbox-shared", ts.encode(), hashlib.sha256).hexdigest()
    return {"X-Runtime-Timestamp": ts, "X-Runtime-Signature": sig, **extra}


def test_q6_a_valid_hmac_signature_cannot_decide_or_ack(hmac_api):
    client, rid = hmac_api
    assert client.get("/api/v1/fullcycle/reviews", headers=_signed()).status_code == 200
    for hdr in (_signed(), _signed(**{"X-Runtime-Actor": "sandbox-gpt"}),
                _signed(**{"X-Runtime-Actor": "client/sandbox-gpt"})):
        assert client.post(f"/api/v1/fullcycle/reviews/{rid}/decision",
                           json={"decision": "approve", "idempotency_key": "h1"},
                           headers=hdr).status_code == 403
        assert client.post(f"/api/v1/fullcycle/reviews/{rid}/ack",
                           headers=hdr).status_code == 403
    conn, _ = fe.connect()
    try:
        assert conn.execute("SELECT status FROM fc_review WHERE id=?", (rid,)).fetchone()[0] \
            == "pending"
    finally:
        conn.close()


# ── Q7 ─────────────────────────────────────────────────────────────────────────
def test_q7_migration_of_a_populated_pre_branch_db_with_a_live_reader(tmp_path, monkeypatch):
    db = tmp_path / "cp.db"
    monkeypatch.setenv("CONTROL_PLANE_DB", str(db))
    old_schema = fe._SCHEMA.replace(", unknown_polls INTEGER DEFAULT 0", "")
    old_schema = old_schema[:old_schema.index("CREATE TABLE IF NOT EXISTS fc_escalation")]
    seed = sqlite3.connect(db)
    seed.execute("PRAGMA journal_mode=WAL")
    for stmt in old_schema.strip().split(";"):
        if stmt.strip():
            seed.execute(stmt)
    seed.executemany("INSERT INTO fc_task (id, project_id, key, seq, body, contract_id)"
                     " VALUES (?,?,?,?,?,?)",
                     [(f"p:t{i}", "p", f"t{i}", i, "{}", f"c{i}") for i in range(5000)])
    seed.executemany("INSERT INTO fc_review (project_id, task_id, attempt, kind, payload)"
                     " VALUES (?,?,?,?,?)",
                     [("p", f"p:t{i}", 1, "review", "{}") for i in range(2000)])
    seed.commit()
    assert "unknown_polls" not in {r[1] for r in seed.execute("PRAGMA table_info(fc_task)")}
    reader = sqlite3.connect(db, timeout=10)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM fc_task").fetchone()[0] == 5000  # open read txn
    t0 = time.time()
    conn, _ = fe.connect()                           # the promotion's first connect
    took = time.time() - t0
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fc_task)")}
        assert "unknown_polls" in cols
        assert conn.execute("SELECT COUNT(*) FROM fc_task WHERE unknown_polls=0").fetchone()[0] \
            == 5000
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN"
                                           " ('table','trigger')")} >= {
            "fc_escalation", "fc_decision_audit", "fc_decision_audit_no_update",
            "fc_decision_audit_no_delete"}
        assert len(fe.pending_reviews("p", conn=conn)) == 2000
        fe.connect(conn)                             # idempotent second run
    finally:
        conn.close()
        reader.rollback()
        reader.close()
    assert took < 5, took


# ── CDP_PORT ───────────────────────────────────────────────────────────────────
def _composer_port(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CDP_PORT", raising=False)
    else:
        monkeypatch.setenv("CDP_PORT", value)
    spec = importlib.util.spec_from_file_location(
        f"cdp_port_probe_{value}", os.path.join(_ROOT, "tools", "cdp_composer.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CDP_PORT


@pytest.mark.parametrize("value,port", [(None, 9222), ("", 9222), ("19222", 19222),
                                        ("abc", 0), ("80", 0), ("70000", 0)])
def test_cdp_port_knob_never_falls_back_to_the_live_browser(monkeypatch, value, port):
    assert _composer_port(monkeypatch, value) == port
