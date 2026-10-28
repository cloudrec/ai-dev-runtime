"""The native supervisor asks the send policy BEFORE it sends.

Under `AGENT_AUTOMATION_POLICY=enforce` with only the controller allowlisted, every
continuation the supervisor attempted was refused inside `agent_control._deliver` — 304
in the 24 h to 2026-10-28, re-tried by the idle sweep every 300 s per target. The
pre-check asks the same `automation_decision` first. It may only refuse: `_deliver`
stays the enforcing choke point, and the refusal stays in the audit.
"""
from __future__ import annotations

import json
import time

import pytest

from core import agent_control as ac
from core import native_supervisor as ns

TARGET = "cp-canary:0.0"
CWD = "/root/cp-canary-v2"
REFUSED = "not_delivered:automation_target_not_allowlisted"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "enforce")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "owner-os-supervisor-clean@api")
    monkeypatch.setattr(ns, "_TARGETS_RAW", TARGET)
    yield


def _agent(target=TARGET, cwd=CWD, state="idle", session=None):
    return {"target": target, "session": session or target.split(":")[0],
            "claude_cwd": cwd, "cwd": cwd, "is_agent": True, "alive": True,
            "state": state, "pending": None}


def _hook_event(conn, cwd=CWD):
    cur = conn.execute(
        "INSERT INTO event (ts,ts_epoch,source,type,agent_id,severity,payload) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t", time.time(), "claude_hook", "agent_turn_stopped", "session:s1", "info",
         json.dumps({"source": "claude_hook", "cwd": cwd, "session_id": "s1"})))
    conn.commit()
    return cur.lastrowid


def _quiet(conn, target=TARGET, secs=None, now=None):
    now = now or time.time()
    conn.execute("CREATE TABLE IF NOT EXISTS agent_watch_state ("
                 "target TEXT PRIMARY KEY, cls TEXT, digest TEXT, at TEXT, ts REAL,"
                 "notified_cls TEXT, notified_digest TEXT, notified_at TEXT,"
                 "notified_ts REAL, emissions INTEGER DEFAULT 0, miss_count INTEGER,"
                 "digest_since REAL)")
    conn.execute("INSERT OR REPLACE INTO agent_watch_state(target,cls,digest_since) "
                 "VALUES(?,?,?)", (target, "idle",
                                   now - (secs or ns.IDLE_SWEEP_QUIET_SECS + 60)))
    conn.commit()


@pytest.fixture()
def real_send(monkeypatch):
    """`scan()` with its PRODUCTION defaults; only the pane write is replaced, by a fake
    that enforces the same policy `_deliver` does, so a skipped pre-check is visible."""
    calls = []

    def fake_agent_send(target, text, idempotency_key=None, actor=None, source=None):
        calls.append({"target": target, "idem": idempotency_key, "source": source})
        refusal, by = ac.automation_decision(target.split(":")[0], target, actor,
                                             action="agent_send", cwd=CWD)
        if refusal:
            return {"delivered": False, "refused": refusal}
        return {"delivered": True, "submitted": True, "agent_created": False,
                "authorized_by": by}
    monkeypatch.setattr(ac, "agent_send", fake_agent_send)
    return calls


def _scan(conn, agents, now=None):
    return ns.scan(conn=conn, agents=agents, safe_fn=lambda t: True,
                   step_text="continue with the next safe step", now=now)


def _rows(conn, table):
    return conn.execute(f"SELECT target, action, reason, ok, detail FROM {table} "
                        "ORDER BY ts_epoch").fetchall()


def test_event_path_refusal_is_known_before_sending(real_send):
    conn, _ = ns._conn()
    eid = _hook_event(conn)
    r = _scan(conn, [_agent()])
    assert real_send == [], "no send attempted for a refusal the policy already knows"
    row = conn.execute("SELECT target, action, reason, ok, detail FROM native_supervision "
                       "WHERE event_id=?", (eid,)).fetchone()
    assert row[:4] == (TARGET, ns.POLICY_REFUSED_ACTION, REFUSED, 0), \
        "same refusal reason, still ok=0 — the audit says what it said before"
    assert json.loads(row[4]) == {"precheck": True}
    assert r["skipped"][-1]["why"] == "policy_refused:automation_target_not_allowlisted"
    # the event is consumed exactly as a refused send consumed it
    _scan(conn, [_agent()])
    assert conn.execute("SELECT COUNT(*) FROM native_supervision").fetchone()[0] == 1


def test_allowlisted_supervisor_still_sends_and_policy_change_applies_next_turn(
        real_send, monkeypatch):
    conn, _ = ns._conn()
    _hook_event(conn)
    _scan(conn, [_agent()])
    assert real_send == []
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS",
                       f"owner-os-supervisor-clean@api,{TARGET}@native_supervisor")
    eid = _hook_event(conn)          # the next real turn boundary — never backed off
    r = _scan(conn, [_agent()])
    assert len(real_send) == 1 and real_send[0]["idem"] == f"nativesup:{eid}"
    assert r["acted"] and r["acted"][0]["delivered"] is True


def test_payment_cwd_is_refused_before_sending_even_if_allowlisted(real_send, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "renamed:0.0@native_supervisor")
    conn, _ = ns._conn()
    refusal = ns.policy_precheck("renamed:0.0",
                                 _agent("renamed:0.0", cwd="/opt/payment-orchestrator"))
    assert refusal == "hard_denied_target"


def test_precheck_never_authorizes_on_error(monkeypatch, real_send):
    """Undetermined -> "" -> the real send runs and `_deliver` decides (here: refuses)."""
    def boom(*a, **k):
        raise RuntimeError("policy unreadable")
    real_decision = ac.automation_decision
    monkeypatch.setattr(ac, "automation_decision", boom)
    assert ns.policy_precheck(TARGET, _agent()) == ""
    monkeypatch.setattr(ac, "automation_decision", real_decision)
    conn, _ = ns._conn()
    _hook_event(conn)
    ns.scan(conn=conn, agents=[_agent()], safe_fn=lambda t: True,
            step_text="continue with the next safe step", policy_fn=lambda t, a: "")
    assert len(real_send) == 1
    row = conn.execute("SELECT action, reason, ok FROM native_supervision").fetchone()
    assert row == ("continue", REFUSED, 0), "the choke point still refused it"


def test_precheck_is_the_grant_free_in_process_caller(monkeypatch):
    """Grants cover only `api:` callers; asking as native_supervisor can never pick one up."""
    seen = {}

    def spy(session, action, actor, now=None, cwd=None):
        seen["actor"] = actor
        return "gdeadbeef0000"
    from core import automation_grants as ag
    monkeypatch.setattr(ag, "allows_send", spy)
    # the allowlist does not match, so the grant lookup is consulted — as OUR actor,
    # which the real `allows_send` refuses for every non-`api:` caller
    ns.policy_precheck(TARGET, _agent())
    assert seen["actor"] == "native_supervisor"



def test_idle_sweep_refusal_is_recorded_once_then_backed_off(real_send):
    conn, _ = ns._conn()
    now = time.time()
    _quiet(conn, now=now)
    calls = []

    def policy(t, a):
        calls.append(t)
        return ns.policy_precheck(t, a)
    for k in range(6):                        # 6 sweeps over 30 min
        t = now + k * ns.IDLE_SWEEP_QUIET_SECS
        _quiet(conn, now=t)
        ns.scan(conn=conn, agents=[_agent()], safe_fn=lambda x: True,
                step_text="continue with the next safe step", now=t, policy_fn=policy,
                send_fn=ac.agent_send)
    assert real_send == [], "no idle send for a known refusal"
    rows = _rows(conn, "native_supervision_idle")
    assert [r[1:4] for r in rows] == [(ns.POLICY_REFUSED_ACTION, REFUSED, 0)], rows
    assert calls == [TARGET], "re-asked only after the backoff"
    # after the backoff, asked again (one more row, still no send)
    t = now + ns.POLICY_REFUSED_BACKOFF_SECS + 1
    _quiet(conn, now=t)
    ns.scan(conn=conn, agents=[_agent()], safe_fn=lambda x: True,
            step_text="continue with the next safe step", now=t, policy_fn=policy,
            send_fn=ac.agent_send)
    assert len(_rows(conn, "native_supervision_idle")) == 2 and real_send == []


def test_idle_sweep_default_wiring_uses_the_precheck(real_send):
    conn, _ = ns._conn()
    _quiet(conn)
    _scan(conn, [_agent()])
    assert real_send == []
    assert _rows(conn, "native_supervision_idle")[0][1] == ns.POLICY_REFUSED_ACTION


def test_idle_sweep_allowlisted_target_is_still_continued(real_send, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", f"{TARGET}@native_supervisor")
    conn, _ = ns._conn()
    _quiet(conn)
    r = _scan(conn, [_agent()])
    assert len(real_send) == 1 and r["acted"][0]["via"] == "idle_sweep"


def test_policy_refusal_is_not_a_continue_for_the_anti_loop_cap(real_send):
    """A pre-check refusal sends nothing, so it must not count toward MAX_CONSECUTIVE."""
    conn, _ = ns._conn()
    for _ in range(ns.MAX_CONSECUTIVE + 2):
        _hook_event(conn)
    _scan(conn, [_agent()])
    last, n = ns._recent_for_target(conn, TARGET, time.time())
    assert n == 0
    assert conn.execute("SELECT COUNT(*) FROM native_supervision_gate").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["session", "cwd"])
def test_precheck_refuses_nothing_without_the_resolved_pane(missing):
    """Review 2026-10-28: `_deliver` resolves session and cwd from the live pane; without
    them the pre-check cannot know it would agree, so it leaves the decision to `_deliver`."""
    a = _agent()
    if missing == "session":
        a["session"] = ""
    else:
        a["claude_cwd"] = a["cwd"] = None
    assert ns.policy_precheck(TARGET, a) == ""
