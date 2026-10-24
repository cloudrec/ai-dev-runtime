"""Native Claude Code integration: `claude agents --json` state, `/goal` policy, CTO bridge."""
from __future__ import annotations

import json

import pytest

from core import cto_bridge as cb
from core import native_agents as na
from core import native_goals as ng
from core.control_plane.api import _c

ROW = {"pid": 500301, "cwd": "/root/aes-canary-work", "kind": "interactive",
       "startedAt": 1, "sessionId": "da1abad1-8937-42db-bf4a-fd89527b77ef",
       "name": "aes-canary-work-fd", "status": "idle"}
PAY = {"pid": 42, "cwd": "/opt/payment-orchestrator", "kind": "interactive", "startedAt": 1,
       "sessionId": "0d6e1f2a-0000-0000-0000-000000000000", "name": "cash-shift-report-admin",
       "status": "idle"}
INV = {"agents": [{"target": "aes-canary:0.0", "session": "aes-canary", "claude_pid": 500301,
                   "is_agent": True, "state": "idle", "cwd": "/root/aes-canary-work"},
                  {"target": "payorch-ha-fresh:1.1", "session": "payorch-ha-fresh",
                   "claude_pid": 42, "is_agent": True, "state": "idle",
                   "cwd": "/opt/payment-orchestrator"}]}


def runner(rows=None, rc=0, raw=None):
    def run(argv):
        if argv[1:] == ["--version"]:
            return 0, b"2.1.310 (Claude Code)\n", b""
        if argv[1:] == ["agents", "--help"]:
            return 0, b"  --json  Print active sessions ... as a JSON array and exit", b""
        return rc, raw if raw is not None else json.dumps(rows or []).encode(), b""
    return run


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    for k in ("NATIVE_GOALS_ENABLED", "NATIVE_GOALS_DENY", "OWNER_OS_CTO_PROVIDER",
              "OWNER_OS_CTO_DELIVERY_MODE", "NATIVE_OWNER_OS_ENABLED",
              "OWNER_OS_CTO_MODEL", "OPENAI_API_KEY", "OWNER_OS_CTO_DENY_AGENTS"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── state: claude agents --json ─────────────────────────────────────────────
def test_capabilities_detected_from_installed_cli():
    assert na.capabilities(runner()) == {"version": "2.1.310", "agents_json": True}


def test_native_listing_is_primary_and_joins_tmux_by_claude_pid():
    out = na.list_sessions(runner([ROW, PAY]), inventory=lambda: INV)
    assert out["source"] == na.SOURCE_NATIVE
    s = na.find("aes-canary", out)
    assert s["name"] == "aes-canary-work-fd" and s["tmux_target"] == "aes-canary:0.0"
    assert s["state"] == "idle" and s["session_id"] == ROW["sessionId"]
    assert na.find("aes-canary-work-fd", out) is s


def test_ambiguous_pid_join_attaches_no_pane():
    inv = {"agents": INV["agents"] + [dict(INV["agents"][0], target="other:0.0")]}
    out = na.list_sessions(runner([ROW]), inventory=lambda: inv)
    assert out["sessions"][0]["tmux_target"] is None


@pytest.mark.parametrize("kw,reason", [
    ({"rc": 1}, "agents_json_rc_1"),
    ({"raw": b"not json"}, "agents_json_invalid"),
    ({"raw": b'{"a": 1}'}, "agents_json_invalid"),
    ({"rows": [{"name": "x"}]}, "agents_json_invalid"),
])
def test_unavailable_or_invalid_output_falls_back_to_agent_control(kw, reason):
    out = na.list_sessions(runner(**kw), inventory=lambda: INV)
    assert out["source"] == na.SOURCE_FALLBACK and out["fallback_reason"].startswith(reason)
    assert {s["tmux_target"] for s in out["sessions"]} == {"aes-canary:0.0",
                                                           "payorch-ha-fresh:1.1"}


def test_oversized_output_falls_back(monkeypatch):
    monkeypatch.setattr(na, "MAX_BYTES", 10)
    out = na.list_sessions(runner([ROW]), inventory=lambda: INV)
    assert out["fallback_reason"] == "agents_json_output_too_large"


def test_unknown_state_is_never_read_as_idle():
    out = na.list_sessions(runner([dict(ROW, status="??")]), inventory=lambda: INV)
    assert out["sessions"][0]["state"] == "unknown"


def test_background_rows_use_state_and_short_id():
    bg = {"id": "3deb87db", "sessionId": "s-1", "name": "bg", "kind": "background",
          "state": "blocked", "cwd": "/root", "startedAt": 1}
    s = na.list_sessions(runner([bg]), inventory=lambda: {"agents": []})["sessions"][0]
    assert s["state"] == "blocked" and s["short_id"] == "3deb87db"


# ── /goal policy ────────────────────────────────────────────────────────────
COND = "notes.txt ends with `step 6 done`"


def listing(state="idle"):
    return na.list_sessions(runner([dict(ROW, status=state), PAY]), inventory=lambda: INV)


def test_config_is_explicit_and_payorch_is_hard_denied(tmp_path):
    p = tmp_path / "g.yaml"
    p.write_text("sessions:\n  aes-canary: {enabled: true, condition: 'x done'}\n"
                 "  payorch-ha-fresh: {enabled: true, condition: 'y'}\n"
                 "  off: {enabled: false, condition: 'z'}\n  empty: {enabled: true}\n"
                 "  clearer: {enabled: true, condition: clear}\n")
    assert ng.load_config(str(p)) == {"aes-canary": {"condition": "x done"}}


def test_repo_config_only_names_the_canary():
    assert set(ng.load_config()) == {"aes-canary"}


def test_install_disabled_by_default():
    sends = []
    r = ng.install("aes-canary", listing=listing(), config={"aes-canary": {"condition": COND}},
                   send=lambda *a: sends.append(a))
    assert r["reason"] == "native_goals_disabled" and sends == []


@pytest.mark.parametrize("name,cfg,lst,reason", [
    ("payorch-ha-fresh", {"payorch-ha-fresh": {"condition": COND}}, None, "session_hard_denied"),
    ("aes-canary", {}, None, "no_goal_policy_for_session"),
    ("aes-canary", None, "busy", "session_not_idle:busy"),
    ("aes-canary", None, "fallback", "native_state_unavailable"),
])
def test_install_refusals(monkeypatch, name, cfg, lst, reason):
    monkeypatch.setenv("NATIVE_GOALS_ENABLED", "1")
    sends = []
    lstg = {"source": na.SOURCE_FALLBACK, "sessions": []} if lst == "fallback" else \
        listing(lst or "idle")
    r = ng.install(name, listing=lstg, config=cfg if cfg is not None else
                   {"aes-canary": {"condition": COND}}, send=lambda *a: sends.append(a))
    assert r["reason"] == reason and sends == []


def test_install_once_then_replay_never_resends(monkeypatch, tmp_path):
    monkeypatch.setenv("NATIVE_GOALS_ENABLED", "1")
    monkeypatch.setattr(ng, "active_goal", lambda *a, **k: None)
    sends = []

    def send(target, text, key):
        sends.append((target, text, key))
        return {"submitted": True}
    cfg = {"aes-canary": {"condition": COND}}
    r = ng.install("aes-canary", listing=listing(), config=cfg, send=send)
    assert r["ok"] and sends == [("aes-canary:0.0", f"/goal {COND}",
                                  f"goal:aes-canary:{ng.condition_hash(COND)}")]
    r2 = ng.install("aes-canary", listing=listing(), config=cfg, send=send)
    assert r2["reason"] == "goal_install_already_latched" and len(sends) == 1


def test_send_exception_is_latched_not_retried(monkeypatch):
    monkeypatch.setenv("NATIVE_GOALS_ENABLED", "1")
    monkeypatch.setattr(ng, "active_goal", lambda *a, **k: None)
    calls = []

    def boom(*a):
        calls.append(a)
        raise RuntimeError("died")
    cfg = {"aes-canary": {"condition": COND}}
    assert ng.install("aes-canary", listing=listing(), config=cfg, send=boom)["ok"] is False
    ng.install("aes-canary", listing=listing(), config=cfg, send=boom)
    assert len(calls) == 1


def test_already_active_goal_is_not_resent(monkeypatch):
    monkeypatch.setenv("NATIVE_GOALS_ENABLED", "1")
    monkeypatch.setattr(ng, "active_goal", lambda *a, **k: COND)
    sends = []
    r = ng.install("aes-canary", listing=listing(), config={"aes-canary": {"condition": COND}},
                   send=lambda *a: sends.append(a))
    assert r["reason"] == "goal_already_active" and sends == []


def _cmd(args):
    return json.dumps({"type": "user", "message": {"role": "user", "content":
                       f"<command-name>/goal</command-name>\n<command-message>goal"
                       f"</command-message>\n<command-args>{args}</command-args>"}})


def test_active_goal_read_from_transcript_last_wins_and_clear(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([json.dumps({"type": "user", "message": {"content": "hello"}}),
                            _cmd("first"), _cmd("second  goal")]) + "\n")
    assert ng.active_goal("", "", path=str(p)) == "second goal"
    p.write_text(p.read_text() + _cmd("clear") + "\n")
    assert ng.active_goal("", "", path=str(p)) is None
    assert ng.active_goal("", "", path=str(tmp_path / "missing.jsonl")) is None


def _att(**kw):
    return json.dumps({"type": "attachment", "attachment": {"type": "goal_status", **kw}})


def _local(args):
    return json.dumps({"type": "system", "subtype": "local_command", "content":
                       f"<command-name>/goal</command-name>\n<command-message>goal"
                       f"</command-message>\n<command-args>{args}</command-args>"})


def test_goal_state_matches_live_2_1_310_records(tmp_path):
    """Shapes observed live on aes-canary: user-typed /goal, goal_status per Stop-hook
    evaluation (failed:true when the evaluator gives up), /goal clear as local_command."""
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([_cmd("ship it"), _att(met=False, condition="ship it")]) + "\n")
    assert ng.goal_state("", "", path=str(p))["active"] is True
    p.write_text(p.read_text() + _att(met=False, failed=True, iterations=5) + "\n")
    st = ng.goal_state("", "", path=str(p))
    assert st["active"] is False and st["failed"] is True and st["iterations"] == 5
    p.write_text(p.read_text() + _cmd("again") + "\n" + _att(met=True) + "\n")
    st = ng.goal_state("", "", path=str(p))
    assert st["active"] is False and st["met"] is True
    p.write_text(p.read_text() + _cmd("third") + "\n" + _local("clear") + "\n")
    assert ng.active_goal("", "", path=str(p)) is None


def test_repo_condition_is_end_state_with_owner_escape():
    cond = ng.load_config()["aes-canary"]["condition"]
    assert "step 6 done" in cond and "OWNER_NEEDED" in cond
    assert "no shell" not in cond.lower()


def test_status_reports_policy_location_and_goal(monkeypatch):
    monkeypatch.setattr(ng, "goal_state", lambda *a, **k: {"active": True, "condition": COND,
                                                           "met": False, "failed": False})
    rows = ng.status(listing(), {"aes-canary": {"condition": COND}})
    assert rows == [{"session": "aes-canary", "located": True,
                     "native_name": "aes-canary-work-fd", "tmux_target": "aes-canary:0.0",
                     "state": "idle", "goal_active": True, "goal_matches_config": True,
                     "goal_met": False, "goal_failed": False, "source": na.SOURCE_NATIVE}]


# ── CTO bridge ──────────────────────────────────────────────────────────────
class FakeProvider(cb.Provider):
    name, model = "fake", "fake-model"

    def __init__(self, out=None, err=None, conversations=True):
        self.calls, self.out, self.err, self.created = [], out, err, []
        self.conversations = conversations

    def create_conversation(self, project):
        if not self.conversations:
            return None
        self.created.append(project)
        return f"conv_{project}_{len(self.created)}"

    def decide(self, ctx, conversation_id=None):
        self.calls.append((ctx, conversation_id))
        if self.err:
            raise cb.ProviderError(self.err)
        return dict(self.out or {"decision": "split the change", "rationale": "r",
                                 "owner_needed": False, "message_to_agent": "do X"})


def EV(**kw):
    return {"key": "evt:1", "type": "repeated_failure", "project": "owner-os",
            "agent": "aes-canary:0.0", "goal": COND, "state": "idle",
            "question": "tests keep failing", "diff_summary": "core/x.py +3 -1", **kw}


def _rows(sql, args=()):
    conn, _ = _c(None)
    cb._schema(conn)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_non_reasoning_events_never_reach_the_provider():
    p = FakeProvider()
    assert cb.handle_event(EV(type="work_stopped_incomplete"), provider=p)["handled"] is False
    assert p.calls == []


def test_decision_recorded_and_message_queued_once_even_on_replay():
    p = FakeProvider()
    r = cb.handle_event(EV(), provider=p)
    assert r["status"] == "decided" and r["message_queued"] is True
    assert cb.handle_event(EV(), provider=p)["reason"] == "duplicate_event_key"
    assert len(p.calls) == 1
    assert _rows("SELECT to_agent,message,state FROM native_outbox") == [
        ("aes-canary:0.0", "do X", "pending")]
    assert _rows("SELECT policy_class,action FROM decision") == [("cto_advice",
                                                                  "split the change")]
    msgs = cb.pending_messages()
    assert cb.mark_sent(msgs[0]["msg_key"], "SendMessage ok") is True
    assert cb.mark_sent(msgs[0]["msg_key"], "again") is False
    assert cb.pending_messages() == []


def test_one_durable_conversation_per_project():
    p = FakeProvider()
    a1 = cb.handle_event(EV(key="e1"), provider=p)
    a2 = cb.handle_event(EV(key="e2"), provider=p)
    b1 = cb.handle_event(EV(key="e3", project="payments-ui"), provider=p)
    assert a1["conversation_id"] == a2["conversation_id"] == "conv_owner-os_1"
    assert b1["conversation_id"] == "conv_payments-ui_2"
    assert p.created == ["owner-os", "payments-ui"]
    assert [c for _, c in p.calls] == ["conv_owner-os_1", "conv_owner-os_1",
                                       "conv_payments-ui_2"]
    # survives a fresh provider instance (restart): stored, not re-created
    p2 = FakeProvider()
    assert cb.handle_event(EV(key="e4"), provider=p2)["conversation_id"] == "conv_owner-os_1"
    assert p2.created == []
    assert _rows("SELECT project,conversation_id FROM cto_conversation ORDER BY project") == [
        ("owner-os", "conv_owner-os_1"), ("payments-ui", "conv_payments-ui_2")]


def test_provider_without_conversations_stays_stateless():
    p = FakeProvider(conversations=False)
    assert cb.handle_event(EV(), provider=p)["conversation_id"] is None
    assert _rows("SELECT COUNT(*) FROM cto_conversation") == [(0,)]


def test_context_is_compact_and_bounded():
    p = FakeProvider()
    cb.handle_event(EV(question="q" * 10000, recent_decisions=["d"] * 50), provider=p)
    ctx = p.calls[0][0]
    assert len(ctx["question"]) == 2000 and len(ctx["recent_decisions"]) == 5
    assert set(ctx) == {"event_type", "project", "agent", "goal", "state", "question",
                        "git_diff_summary", "recent_decisions", "affected_agents"}


def test_owner_needed_event_is_not_forwarded_and_not_re_notified():
    owner = []
    r = cb.handle_event(EV(type="owner_needed"), provider=FakeProvider(),
                        emit_owner=lambda c, why: owner.append(why))
    assert r["owner_needed"] is True and r["message_queued"] is False
    assert owner == [] and cb.pending_messages() == []    # the event notified the owner


def test_provider_escalation_is_owner_needed_once_never_forwarded():
    owner = []
    out = {"decision": "ship to prod", "rationale": "", "owner_needed": True,
           "message_to_agent": "go ahead and deploy"}
    r = cb.handle_event(EV(), provider=FakeProvider(out),
                        emit_owner=lambda c, why: owner.append(why))
    assert r["owner_needed"] is True and r["message_queued"] is False
    assert owner == ["ship to prod"] and cb.pending_messages() == []


def test_denied_agent_gets_no_message():
    r = cb.handle_event(EV(agent="payorch-ha-fresh:1.1"), provider=FakeProvider())
    assert r["message_queued"] is False and cb.pending_messages() == []


def test_missing_provider_waits_durably_then_is_decided_once_configured():
    r = cb.handle_event(EV(), gate="missing_secret:OPENAI_API_KEY")
    assert r["status"] == "waiting_provider" and r["error"] == "missing_secret:OPENAI_API_KEY"
    assert cb.retry_waiting(gate="missing_secret:OPENAI_API_KEY") == []   # still no provider
    p = FakeProvider()
    done = cb.retry_waiting(provider=p)
    assert [d["status"] for d in done] == ["decided"] and len(p.calls) == 1
    assert cb.retry_waiting(provider=p) == [] and len(p.calls) == 1
    assert _rows("SELECT COUNT(*) FROM native_outbox") == [(1,)]


def test_provider_errors_retry_up_to_the_cap():
    p = FakeProvider(err="request_failed:URLError")
    assert cb.handle_event(EV(), provider=p)["status"] == "provider_error"
    cb.retry_waiting(provider=p)
    assert cb.retry_waiting(provider=p)[0]["status"] == "provider_failed"
    assert cb.retry_waiting(provider=p) == [] and len(p.calls) == cb.MAX_ATTEMPTS
    assert cb.pending_messages() == []


def test_crash_latched_row_is_never_retried_automatically():
    conn, _ = _c(None)
    cb._schema(conn)
    conn.execute("INSERT INTO cto_decision (event_key,event_type,status,context) VALUES "
                 "('evt:9','repeated_failure','latched','{}')")
    conn.commit()
    conn.close()
    p = FakeProvider()
    assert cb.retry_waiting(provider=p) == [] and p.calls == []


@pytest.mark.parametrize("env,gate", [
    ({}, "cto_provider_disabled"),
    ({"OWNER_OS_CTO_PROVIDER": "other"}, "cto_provider_unknown:other"),
    ({"OWNER_OS_CTO_PROVIDER": "openai"}, "missing_config:OWNER_OS_CTO_MODEL"),
    ({"OWNER_OS_CTO_PROVIDER": "openai", "OWNER_OS_CTO_MODEL": "m"},
     "missing_secret:OPENAI_API_KEY"),
])
def test_provider_config_gates(monkeypatch, env, gate):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cb.provider_from_env() == (None, gate)


class _Resp:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.data).encode()


def test_openai_adapter_uses_conversations_and_responses(monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_PROVIDER", "openai")
    monkeypatch.setenv("OWNER_OS_CTO_MODEL", "configured-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    seen = []

    def opener(req, timeout):
        body = json.loads(req.data)
        seen.append((req.full_url, req.get_header("Authorization"), body))
        if req.full_url.endswith("/conversations"):
            return _Resp({"id": "conv_abc", "object": "conversation"})
        return _Resp({"output": [{"content": [{"type": "output_text", "text":
                     '```json\n{"decision": "pause", "rationale": "why", '
                     '"owner_needed": false, "message_to_agent": "pause"}\n```'}]}]})
    prov, gate = cb.provider_from_env()
    assert gate == "" and "sk-test-secret" not in repr(prov)
    prov._open = opener
    r1 = cb.handle_event(EV(key="o1"), provider=prov)
    r2 = cb.handle_event(EV(key="o2"), provider=prov)
    urls = [u for u, _, _ in seen]
    assert urls == ["https://api.openai.com/v1/conversations",
                    "https://api.openai.com/v1/responses",
                    "https://api.openai.com/v1/responses"]
    assert all(a == "Bearer sk-test-secret" for _, a, _ in seen)
    assert seen[0][2] == {"metadata": {"owner_os_project": "owner-os"}}
    for _, _, body in seen[1:]:
        assert body["conversation"] == "conv_abc" and body["model"] == "configured-model"
        assert body["instructions"] == cb.OpenAIResponsesProvider.INSTRUCTIONS
    assert r1["conversation_id"] == r2["conversation_id"] == "conv_abc"
    assert r1["status"] == r2["status"] == "decided"


def test_openai_conversation_without_id_is_a_provider_error():
    prov = cb.OpenAIResponsesProvider(model="m", api_key="k",
                                      opener=lambda req, timeout: _Resp({}))
    assert cb.handle_event(EV(), provider=prov)["status"] == "provider_error"
    assert _rows("SELECT COUNT(*) FROM cto_conversation") == [(0,)]


def test_openai_adapter_failure_never_leaks_the_key():
    def opener(req, timeout):
        raise OSError(f"boom {req.get_header('Authorization')}")
    prov = cb.OpenAIResponsesProvider(model="m", api_key="sk-leak", opener=opener)
    with pytest.raises(cb.ProviderError) as ei:
        prov.decide({})
    assert "sk-leak" not in str(ei.value) and ei.value.__cause__ is None
    r = cb.handle_event(EV(), provider=prov)
    assert "sk-leak" not in json.dumps(r) and "sk-leak" not in json.dumps(
        _rows("SELECT * FROM cto_decision"))


@pytest.mark.parametrize("text", ["", "no json here", '{"rationale": "x"}'])
def test_unparseable_provider_output_is_an_error(text):
    with pytest.raises(cb.ProviderError):
        cb.parse_output(text)


# ── fallback delivery (OWNER_OS_CTO_DELIVERY_MODE=agent_control) ─────────────
def _queue(agent="aes-canary:0.0", key="d1"):
    cb.handle_event(EV(key=key, agent=agent), provider=FakeProvider())


@pytest.mark.parametrize("mode", [None, "native_controller", "off", "bogus"])
def test_fallback_delivery_is_off_unless_explicitly_agent_control(monkeypatch, mode):
    if mode:
        monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", mode)
    _queue()
    sends = []
    assert cb.deliver_pending_agent_control(listing(), send=lambda *a: sends.append(a)) == []
    assert sends == [] and len(cb.pending_messages()) == 1


def test_fallback_delivers_once_to_idle_target_and_marks_sent_only_on_proof(monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    _queue()
    sends = []

    def send(target, text, key):
        sends.append((target, text, key))
        return {"submitted": True, "delivered": True, "idempotency_key": key}
    out = cb.deliver_pending_agent_control(listing(), send=send)
    assert [o["result"] for o in out] == ["sent"]
    assert sends == [("aes-canary:0.0", cb.ADVICE_PREFIX + "do X", "cto:d1")]
    assert cb.deliver_pending_agent_control(listing(), send=send) == [] and len(sends) == 1


@pytest.mark.parametrize("res", [{"submitted": False, "refused": "working"},
                                 {"submitted": True, "delivered": False}, "raise"])
def test_fallback_unproven_delivery_stays_pending(monkeypatch, res):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    _queue()

    def send(*a):
        if res == "raise":
            raise RuntimeError("x")
        return dict(res)
    out = cb.deliver_pending_agent_control(listing(), send=send)
    assert out[0]["result"] == "pending_unproven" and len(cb.pending_messages()) == 1


def test_fallback_busy_target_is_left_pending(monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    _queue()
    sends = []
    out = cb.deliver_pending_agent_control(listing("busy"), send=lambda *a: sends.append(a))
    assert out[0]["result"] == "pending_busy" and sends == []


def test_fallback_never_delivers_to_payorch(monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")
    conn, _ = _c(None)
    cb._schema(conn)          # a row that bypassed the queue-time deny must still be stopped
    conn.execute("INSERT INTO native_outbox (msg_key,event_key,to_agent,message,created_at) "
                 "VALUES ('cto:x','x','payorch-ha-fresh:1.1','do it','t')")
    conn.commit()
    conn.close()
    sends = []
    out = cb.deliver_pending_agent_control(listing(), send=lambda *a: sends.append(a))
    assert out == [{"msg_key": "cto:x", "result": "denied"}] and sends == []
    assert _rows("SELECT state FROM native_outbox") == [("denied",)]
