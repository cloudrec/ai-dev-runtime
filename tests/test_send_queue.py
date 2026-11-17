"""Durable send queue for busy agents (2026-11-17). Synthetic panes only."""
from __future__ import annotations

import threading

import pytest

from core import agent_control as ac
from core import send_queue as sq

PANES = {}
BUSY = set()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    cfg = tmp_path / "policy.yaml"
    cfg.write_text("hard_deny_dirs:\n  - /opt/payment-orchestrator\n")
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(cfg))
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    monkeypatch.setenv("SEND_QUEUE_ENABLED", "1")
    monkeypatch.delenv("AGENT_AUTOMATION_TARGETS", raising=False)
    monkeypatch.delenv("AGENT_AUTOMATION_POLICY", raising=False)
    PANES.clear()
    BUSY.clear()
    PANES.update({
        "worker-a": {"target": "worker-a:0.0", "session": "worker-a", "alive": True,
                     "pane_id": "%1", "pid": 101, "cwd": "/opt/worker-a"},
        "payorch-ha-fresh": {"target": "payorch-ha-fresh:0.0", "session": "payorch-ha-fresh",
                             "alive": True, "pane_id": "%2", "pid": 102,
                             "cwd": "/opt/payment-orchestrator"},
        "renamed": {"target": "renamed:0.0", "session": "renamed", "alive": True,
                    "pane_id": "%3", "pid": 103, "cwd": "/opt/blagopay-edge"},
    })

    def fake_pane(target):
        p = PANES.get(target.split(":")[0])
        if not p:
            raise ac.AgentControlError(f"no existing agent at {target!r}")
        return dict(p)
    monkeypatch.setattr(ac, "_pane_is_live_agent", fake_pane)
    monkeypatch.setattr(ac, "_busy_for_send",
                        lambda t: "working" if t.split(":")[0] in BUSY else "")
    calls = []
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: (calls.append(args) or (0, "", "")))
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return calls


GPT = "api:client/chatgpt-tech-lead"


class Sent:
    def __init__(self):
        self.calls = []

    def __call__(self, target, text, **kw):
        self.calls.append((target, text, kw["idempotency_key"]))
        return {"delivered": True, "submitted": True, "target": target}


def test_a_busy_agent_gets_the_message_once_it_is_idle():
    BUSY.add("worker-a")
    r = ac.agent_send("worker-a:0.0", "next task", idempotency_key="k1", actor=GPT,
                      queue_if_busy=True)
    assert r["queued"] and r["send_queue"]["status"] == "queued"
    sent = Sent()
    assert sq.drain(send=sent) == [] and sent.calls == []          # still busy: held
    BUSY.clear()
    assert sq.drain(send=sent) == [{"key": "k1", "status": "delivered"}]
    assert sent.calls == [("worker-a:0.0", "next task", "k1")]
    assert sq.drain(send=sent) == [] and len(sent.calls) == 1      # exactly once
    st = sq.status("k1")
    assert st["status"] == "delivered" and st["receipt"]["delivered"]


def test_without_the_flag_a_busy_agent_still_refuses_as_before():
    BUSY.add("worker-a")
    r = ac.agent_send("worker-a:0.0", "x", idempotency_key="k", actor=GPT)
    assert r["refused"] == "working" and not r.get("send_queue")
    assert sq.status("k")["status"] == "unknown"


def test_idempotency_key_required_and_never_reused_for_other_text():
    assert sq.enqueue("worker-a:0.0", "x", idempotency_key="")["refused"] == \
        "idempotency_key_required"
    assert sq.enqueue("worker-a:0.0", "x", idempotency_key="k", actor=GPT)["queued"]
    assert sq.enqueue("worker-a:0.0", "x", idempotency_key="k", actor=GPT)["duplicate"]
    assert sq.enqueue("worker-a:0.0", "y", idempotency_key="k", actor=GPT)["refused"] == \
        "idempotency_key_reused_with_other_text"


@pytest.mark.parametrize("target", ["payorch-ha-fresh:0.0", "renamed:0.0"])
def test_payment_work_is_never_queued(target):
    r = sq.enqueue(target, "x", idempotency_key=f"k-{target}", actor=GPT)
    assert r["refused"] == "hard_denied_target" and sq.status(f"k-{target}")["status"] == \
        "unknown"


def test_an_owner_proof_is_never_queued():
    BUSY.add("worker-a")
    r = ac.agent_send("worker-a:0.0", "x", idempotency_key="kp", actor=GPT,
                      queue_if_busy=True, owner_proof={"ts": 1, "nonce": "a" * 16,
                                                       "sig": "0" * 64})
    assert not r.get("send_queue") and sq.status("kp")["status"] == "unknown"


def test_policy_is_rechecked_at_delivery(monkeypatch):
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kr", actor=GPT)
    monkeypatch.delenv("AGENT_AUTOMATION_OPEN_CALLERS")             # right revoked meanwhile
    out = sq.drain()                                                # the real agent_send
    assert out == [{"key": "kr", "status": "refused",
                    "reason": "automation_target_not_allowlisted"}]


def test_a_target_that_became_payment_work_is_refused_at_delivery():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kc", actor=GPT)
    PANES["worker-a"]["cwd"] = "/opt/payment-orchestrator/sub"
    assert sq.drain()[0]["reason"] == "hard_denied_target"


def test_a_replaced_agent_never_receives_the_old_message():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="ki", actor=GPT)
    PANES["worker-a"]["pid"] = 999                                  # restarted / reused name
    sent = Sent()
    assert sq.drain(send=sent)[0]["reason"] == "target_identity_changed"
    assert sent.calls == []


def test_a_vanished_target_is_refused():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kg", actor=GPT)
    del PANES["worker-a"]
    assert sq.drain(send=Sent())[0]["reason"] == "target_gone"


def test_expiry_and_cancellation():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="ke", actor=GPT, ttl_secs=60, now=1000.0)
    sq.enqueue("worker-a:0.0", "y", idempotency_key="kx", actor=GPT, now=1000.0)
    assert sq.cancel("kx", by="lead")["cancelled"]
    assert not sq.cancel("kx", by="lead")["cancelled"]
    sent = Sent()
    sq.drain(now=1100.0, send=sent)
    assert sent.calls == []
    assert sq.status("ke")["status"] == "expired" and sq.status("kx")["status"] == "cancelled"


def test_messages_to_one_agent_go_in_order_one_per_pass():
    for i in range(3):
        sq.enqueue("worker-a:0.0", f"m{i}", idempotency_key=f"o{i}", actor=GPT)
    sent = Sent()
    for _ in range(3):
        sq.drain(send=sent)
    assert [c[1] for c in sent.calls] == ["m0", "m1", "m2"]


def test_concurrent_drains_send_once():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kk", actor=GPT)
    sent, gate = Sent(), threading.Barrier(2)

    def run():
        gate.wait()
        sq.drain(send=sent)
    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sent.calls) == 1


def test_a_claim_lost_in_a_crash_is_requeued_and_the_replay_is_deduplicated(env):
    sq.enqueue("worker-a:0.0", "hello", idempotency_key="kz", actor=GPT, now=1000.0)
    first = ac.agent_send("worker-a:0.0", "hello", idempotency_key="kz", actor=GPT,
                          source="x|send_queue")      # the drainer's send, then a crash
    assert first.get("delivered") or first.get("submitted"), first   # sent, then "crash":
    conn = sq._conn()
    conn.execute("UPDATE send_queue SET status='delivering', claimed_ts=1000 "
                 "WHERE idempotency_key='kz'")
    conn.commit()
    typed_before = len([c for c in env if c and c[0] in ("paste-buffer", "send-keys")])
    out = sq.drain(now=1000.0 + sq.CLAIM_STALE_SECS + 5)              # restart, real send
    typed_after = len([c for c in env if c and c[0] in ("paste-buffer", "send-keys")])
    assert out == [{"key": "kz", "status": "delivered"}]
    assert typed_after == typed_before                               # never typed twice


def test_state_survives_a_restart():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="ks", actor=GPT)
    import importlib
    importlib.reload(sq)                                              # a fresh process view
    assert sq.status("ks")["status"] == "queued"


def test_a_row_claimed_by_another_drainer_is_never_sent_twice():
    """Drainer A lists [a, b]; while A sends a, drainer B claims and sends b. A must then
    skip b, because its claim (status queued -> delivering) no longer applies."""
    PANES["worker-b"] = {"target": "worker-b:0.0", "session": "worker-b", "alive": True,
                         "pane_id": "%9", "pid": 109, "cwd": "/opt/worker-b"}
    sq.enqueue("worker-a:0.0", "a", idempotency_key="ra", actor=GPT)
    sq.enqueue("worker-b:0.0", "b", idempotency_key="rb", actor=GPT)
    sent = []
    nested = {"done": False}

    def send(target, text, **kw):
        sent.append(text)
        if not nested["done"]:
            nested["done"] = True
            sq.drain(send=send)                    # drainer B, mid-way through A
        return {"delivered": True}
    sq.drain(send=send)
    assert sorted(sent) == ["a", "b"], sent


# ── 2026-11-17 eighth review ────────────────────────────────────────────────
def test_with_the_queue_off_nothing_is_held(monkeypatch):
    monkeypatch.delenv("SEND_QUEUE_ENABLED")
    BUSY.add("worker-a")
    r = ac.agent_send("worker-a:0.0", "x", idempotency_key="koff", actor=GPT,
                      queue_if_busy=True)
    assert not r["queued"] and r["refused"] == "send_queue_disabled"
    assert sq.status("koff")["status"] == "unknown"


def test_status_reports_expiry_before_any_drain():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kt", actor=GPT, ttl_secs=60, now=1000.0)
    assert sq.status("kt", now=1030.0)["status"] == "queued"
    assert sq.status("kt", now=1100.0)["status"] == "expired"


def test_a_key_already_used_by_a_direct_send_is_not_queued():
    ac.agent_send("worker-a:0.0", "direct", idempotency_key="kd", actor=GPT)
    assert sq.enqueue("worker-a:0.0", "queued text", idempotency_key="kd",
                      actor=GPT)["refused"] == "idempotency_key_already_used"


def test_a_duplicate_that_never_submitted_is_not_a_delivery(monkeypatch):
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kn", actor=GPT)
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: {"submitted": False})

    def dup(target, text, **kw):
        return {"duplicate": True, "delivered": False}
    out = sq.drain(send=dup)
    assert out == [{"key": "kn", "status": "refused", "reason": "idempotency_key_used"}]


def test_an_attempt_neither_submitted_nor_refused_is_closed_not_retried():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="kq", actor=GPT)
    calls = []

    def limbo(target, text, **kw):
        calls.append(1)
        return {"delivered": False, "submitted": False}
    assert sq.drain(send=limbo) == [{"key": "kq", "status": "refused",
                                     "reason": "not_submitted"}]
    sq.drain(send=limbo)
    assert len(calls) == 1


# ── crash at each boundary: no blind replay (2026-11-17) ────────────────────
def _force(key, *, started, claimed=1000.0):
    conn = sq._conn()
    conn.execute("UPDATE send_queue SET status='delivering', claimed_ts=?, "
                 "send_started_ts=? WHERE idempotency_key=?", (claimed, started, key))
    conn.commit()


LATER = 1000.0 + sq.CLAIM_STALE_SECS + 5


def test_crash_after_claim_before_sending_is_requeued_and_sent_once():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="c1", actor=GPT, now=1000.0)
    _force("c1", started=None)
    sent = Sent()
    assert sq.drain(now=LATER, send=sent) == [{"key": "c1", "status": "delivered"}]
    assert len(sent.calls) == 1


def test_crash_after_sending_started_without_a_record_is_unknown_never_replayed():
    sq.enqueue("worker-a:0.0", "x", idempotency_key="c2", actor=GPT, now=1000.0)
    _force("c2", started=1000.0)
    sent = Sent()
    sq.drain(now=LATER, send=sent)
    sq.drain(now=LATER + 100, send=sent)
    assert sent.calls == [] and sq.status("c2")["status"] == "unknown"


def test_crash_after_the_record_settles_from_evidence(monkeypatch):
    sq.enqueue("worker-a:0.0", "x", idempotency_key="c3", actor=GPT, now=1000.0)
    _force("c3", started=1000.0)
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: {"submitted": True})
    sent = Sent()
    sq.drain(now=LATER, send=sent)
    st = sq.status("c3")
    assert sent.calls == [] and st["status"] == "delivered"
    assert st["last_reason"] == "delivered_per_record"


def test_a_crash_inside_send_leaves_unknown(monkeypatch):
    sq.enqueue("worker-a:0.0", "x", idempotency_key="c4", actor=GPT, now=1000.0)

    def boom(*a, **k):
        raise RuntimeError("process killed mid-send")
    with pytest.raises(RuntimeError):
        sq.drain(now=1000.0, send=boom)
    sent = Sent()
    sq.drain(now=LATER, send=sent)
    assert sent.calls == [] and sq.status("c4")["status"] == "unknown"


def test_reconcile_is_explicit_attributed_and_evidence_wins(monkeypatch):
    for k in ("r1", "r2", "r3"):
        sq.enqueue("worker-a:0.0", k, idempotency_key=k, actor=GPT, now=1000.0)
        _force(k, started=1000.0)
    sq.drain(now=LATER, send=Sent())
    assert sq.reconcile("r1", "requeue", by="")["refused"] == "bad_reconcile_request"
    assert sq.reconcile("r1", "requeue", by="tech-lead")["status"] == "queued"
    sent = Sent()
    sq.drain(now=LATER + 1, send=sent)
    assert [c[2] for c in sent.calls] == ["r1"]
    assert sq.reconcile("r2", "abandon", by="tech-lead")["status"] == "cancelled"
    monkeypatch.setattr(ac, "_seen_delivery", lambda k: {"submitted": True})
    assert sq.reconcile("r3", "requeue", by="tech-lead")["status"] == "delivered"
    assert sq.reconcile("r3", "requeue", by="x")["refused"] == "not_unknown"


# ── over HTTP: old clients unchanged, new fields work ────────────────────────
@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import v1
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._auth] = lambda: True
    return TestClient(app)


H = {"X-Runtime-Actor": "chatgpt-tech-lead"}


def test_http_old_client_without_new_fields_behaves_as_before(client):
    BUSY.add("worker-a")
    r = client.post("/api/v1/agents/send", json={"target": "worker-a:0.0", "text": "x",
                                                 "idempotency_key": "h0"}, headers=H).json()
    assert r["refused"] == "working" and "send_queue" not in r


def test_http_queue_status_cancel_and_reconcile(client):
    BUSY.add("worker-a")
    r = client.post("/api/v1/agents/send", json={
        "target": "worker-a:0.0", "text": "x", "idempotency_key": "h1",
        "queue_if_busy": True}, headers=H).json()
    if r.get("refused") == "automation_target_not_allowlisted":
        pytest.skip("caller identity is not an api: actor in this harness")
    assert r["queued"] and not r["delivered"] and not r["submitted"]
    assert client.get("/api/v1/agents/send-queue/h1").json()["status"] == "queued"
    assert client.delete("/api/v1/agents/send-queue/h1", headers=H).json()["cancelled"]
    assert client.post("/api/v1/agents/send-queue/h1/reconcile", json={"outcome": "requeue"},
                       headers=H).json()["refused"] == "not_unknown"


@pytest.mark.parametrize("key", [".", "..", "a/b", ".x", "-x", "k" * 200])
def test_keys_must_be_safe_path_segments(key):
    assert sq.enqueue("worker-a:0.0", "x", idempotency_key=key, actor=GPT)["refused"] == \
        "idempotency_key_invalid"


def test_a_long_unknown_raises_a_loop_watch_problem():
    from core import loop_watch as lw
    sq.enqueue("worker-a:0.0", "x", idempotency_key="u1", actor=GPT, now=1000.0)
    _force("u1", started=1000.0)
    sq.drain(now=LATER, send=Sent())
    assert sq.status("u1")["status"] == "unknown"
    assert lw.send_queue_unknown(None, LATER + 10) == {}
    p = lw.send_queue_unknown(None, LATER + sq.UNKNOWN_ALARM_SECS + 10)
    assert "u1" in p["send_queue_unknown"]
    assert lw.send_queue_unknown in lw.CHECKS
