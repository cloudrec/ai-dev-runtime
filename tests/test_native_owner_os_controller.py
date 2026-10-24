"""Native Owner OS controller loop: cursor-driven CTO event handling, goal reconcile gating,
durable waiting_provider, ack-after-handling, replay dedupe, startup wiring."""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from core import cto_bridge as cb
from core import native_goals as ng
from core import native_owner_os as no
from core.control_plane import cto
from core.control_plane.api import _c

LISTING = {"source": "claude_agents_json", "sessions": [
    {"name": "aes-canary-work-fd", "kind": "interactive", "cwd": "/root/aes-canary-work",
     "session_id": "s1", "state": "idle", "pid": 1, "tmux_target": "aes-canary:0.0"}]}


class P(cb.Provider):
    name, model = "fake", "m"

    def __init__(self, raise_on=None):
        self.calls, self.raise_on = [], raise_on

    def create_conversation(self, project):
        return f"conv_{project}"

    def decide(self, ctx, conversation_id=None):
        self.calls.append(ctx["event_type"])
        return {"decision": "d", "rationale": "r", "owner_needed": False,
                "message_to_agent": "advice"}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    for k in ("NATIVE_OWNER_OS_ENABLED", "NATIVE_GOALS_ENABLED", "OWNER_OS_CTO_PROVIDER",
              "OWNER_OS_CTO_MODEL", "OPENAI_API_KEY", "OWNER_OS_CTO_DELIVERY_MODE"):
        monkeypatch.delenv(k, raising=False)
    yield


def emit(type_, *, owner=False, agent="aes-canary:0.0", source="work_evidence", **payload):
    return cto.emit(source, type_, project_id="owner-os", agent_id=agent, severity="info",
                    owner_action_required=owner, payload=payload or None, push=False,
                    dedup_window_secs=0)["event_id"]


def rows(sql):
    conn, _ = _c(None)
    cb._schema(conn)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def tick(**kw):
    return no.run_once(listing=LISTING, **kw)


def test_disabled_by_default_loop_returns_immediately():
    assert no.enabled() is False
    asyncio.run(asyncio.wait_for(no.run_loop(), timeout=2))


def test_loop_is_wired_into_api_startup():
    src = (pathlib.Path(__file__).resolve().parents[1] / "api" / "main.py").read_text()
    assert "from core.native_owner_os import run_loop" in src


def test_first_tick_starts_at_bus_head_never_replays_history():
    emit("repeated_failure")
    out = tick(provider=P())
    assert out.get("cursor_initialised") and out["events"] == 0
    assert rows("SELECT COUNT(*) FROM cto_decision") == [(0,)]


@pytest.mark.parametrize("etype,owner,expect", [
    ("architecture_conflict", False, "architecture_conflict"),
    ("milestone_review", False, "milestone_review"),
    ("agent_waiting_input", True, "owner_needed"),
    ("work_stopped_incomplete", False, None),
])
def test_event_mapping(etype, owner, expect):
    tick(provider=P())
    eid = emit(etype, owner=owner, question="why?")
    p = P()
    out = tick(provider=p)
    assert out["acked_to"] == eid and cto.get_cursor(no.CONSUMER) == eid
    got = rows("SELECT event_key,event_type FROM cto_decision")
    assert got == ([(f"cp:{eid}", expect)] if expect else [])
    assert p.calls == ([expect] if expect else [])


def test_own_events_are_never_fed_back():
    tick(provider=P())
    emit(no.OWNER_NEEDED_TYPE, owner=True, source=cb.SOURCE)
    p = P()
    tick(provider=p)
    assert p.calls == [] and rows("SELECT COUNT(*) FROM cto_decision") == [(0,)]


def test_missing_provider_is_durable_waiting_then_decided_when_configured():
    tick(gate="missing_secret:OPENAI_API_KEY")
    emit("repeated_failure")
    tick(gate="missing_secret:OPENAI_API_KEY")
    assert rows("SELECT status,error FROM cto_decision") == [
        ("waiting_provider", "missing_secret:OPENAI_API_KEY")]
    p = P()
    out = tick(provider=p)
    assert out["retried"] == 1 and p.calls == ["repeated_failure"]
    assert rows("SELECT status FROM cto_decision") == [("decided",)]
    assert tick(provider=p)["retried"] == 0 and len(p.calls) == 1


def test_replay_after_cursor_rewind_duplicates_nothing():
    tick(provider=P())
    emit("repeated_failure")
    emit("agent_waiting_input", owner=True)
    p = P()
    tick(provider=p)
    cto.set_cursor(no.CONSUMER, 0)
    tick(provider=p)
    assert len(p.calls) == 2
    assert rows("SELECT COUNT(*) FROM native_outbox") == [(1,)]   # owner_needed: none


def test_poison_event_is_recorded_durably_and_does_not_wedge_the_bus(monkeypatch):
    tick(provider=P())
    bad = emit("repeated_failure")
    good = emit("milestone_review")
    real = cb.handle_event

    def flaky(ev, **kw):
        if ev["key"] == f"cp:{bad}":
            raise RuntimeError("poison")
        return real(ev, **kw)
    monkeypatch.setattr(cb, "handle_event", flaky)
    out = tick(provider=P())
    assert out["errors"] == 1 and out["acked_to"] == good
    assert dict(rows("SELECT event_key,status FROM cto_decision")) == {
        f"cp:{bad}": "handler_error", f"cp:{good}": "decided"}


def test_handling_that_cannot_even_be_recorded_is_not_acked(monkeypatch):
    tick(provider=P())
    eid = emit("repeated_failure")
    monkeypatch.setattr(cb, "handle_event", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("x")))
    monkeypatch.setattr(cb, "_schema", lambda conn: (_ for _ in ()).throw(RuntimeError("db")))
    before = cto.get_cursor(no.CONSUMER)
    tick(provider=P())
    assert cto.get_cursor(no.CONSUMER) == before < eid


def test_goal_reconcile_only_when_goals_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(ng, "install", lambda name, **kw: calls.append(name) or
                        {"reason": "goal_already_active"})
    tick(provider=P())
    assert calls == []
    monkeypatch.setenv("NATIVE_GOALS_ENABLED", "1")
    out = tick(provider=P())
    assert calls == ["aes-canary"]                    # explicit config only
    assert out["goals"] == [{"session": "aes-canary", "reason": "goal_already_active"}]


def test_fallback_delivery_runs_only_in_agent_control_mode(monkeypatch):
    seen = []
    monkeypatch.setattr(cb, "deliver_pending_agent_control",
                        lambda listing, **kw: seen.append(cb.delivery_mode()) or [])
    tick(provider=P())
    assert seen == ["native_controller"]              # called, and it no-ops in this mode
    assert cb.delivery_mode() == "native_controller"
