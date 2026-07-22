"""Agent Supervisor — auto-confirm only allowlisted safe prompts; verify resume."""
from __future__ import annotations

import pytest

from core import agent_supervisor as sup
from core import agent_control as ac


SAFE_DIALOG = """\
  Bash command
  docker compose ps
  Check which containers are up

Do you want to proceed?
❯ 1. Yes
  2. No
"""
DANGEROUS_DIALOG = """\
  Bash command
  docker compose down --volumes
  Tear down the stack

Do you want to proceed?
❯ 1. Yes
  2. No
"""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AGENT_AUTORESOLVE_SESSIONS", "seo-audit")


def _status(state, tail, target="seo-audit:0.0"):
    return {"target": target, "session": "seo-audit", "state": state,
            "recent_activity": tail, "alive": True, "is_agent": True}


def _wire(monkeypatch, statuses):
    """statuses consumed in order; the last one repeats once exhausted (so a
    busy-loop with a no-op sleep never StopIterations)."""
    box = {"i": 0}
    calls = {"approve": 0}

    def _status_fn(target):
        i = min(box["i"], len(statuses) - 1)
        box["i"] += 1
        return statuses[i]

    monkeypatch.setattr(ac, "agent_status", _status_fn)
    monkeypatch.setattr(ac, "approve_prompt", lambda target: calls.__setitem__("approve", calls["approve"] + 1) or True)
    return calls


def test_not_waiting_returns_none(monkeypatch):
    _wire(monkeypatch, [_status("working", "…(2s")])
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "none"


def test_safe_prompt_allowlisted_is_approved_and_verifies_resume(monkeypatch):
    calls = _wire(monkeypatch, [
        _status("waiting_owner", SAFE_DIALOG),   # initial read
        _status("working", "…(1s · ↓ 2k tokens)"),  # after approval → resumed
    ])
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "approved"
    assert r["resumed"] is True
    assert r["command"] == "docker compose ps"
    assert r["new_state"] == "working"
    assert calls["approve"] == 1
    assert isinstance(r["latency_s"], float)
    # decision persisted
    assert ac.get_prompt_decision("seo-audit:0.0", r["hash"])["decision"] == "approved"


def test_dangerous_prompt_stays_waiting_and_is_not_approved(monkeypatch):
    calls = _wire(monkeypatch, [_status("waiting_owner", DANGEROUS_DIALOG)])
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "left_for_owner"
    assert r["safe"] is False
    assert r["command"] == "docker compose down --volumes"
    assert calls["approve"] == 0                       # NEVER approved


def test_safe_prompt_not_allowlisted_is_left(monkeypatch, tmp_path):
    # Isolate the config so seo-audit is NOT discovered as an auto session.
    empty = tmp_path / "empty.yaml"
    empty.write_text("sessions: {}\n")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_CONFIG", str(empty))
    from core import agent_orchestrator as orch
    orch._config_cache = {}
    monkeypatch.setenv("AGENT_AUTORESOLVE_SESSIONS", "other")
    calls = _wire(monkeypatch, [_status("waiting_owner", SAFE_DIALOG)])
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "left_for_owner"
    assert r["safe"] is True
    assert "allowlist" in r["reason"]
    assert calls["approve"] == 0


def test_dry_run_would_approve_without_keystroke(monkeypatch):
    calls = _wire(monkeypatch, [_status("waiting_owner", SAFE_DIALOG)])
    r = sup.resolve_target("seo-audit:0.0", approve=False, _sleep=lambda s: None)
    assert r["action"] == "would_approve"
    assert calls["approve"] == 0


def test_unextractable_prompt_is_left(monkeypatch):
    _wire(monkeypatch, [_status("waiting_owner", "Do you want to proceed?\n❯ 1. Yes\n 2. No")])
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "left_for_owner"
    assert "extract" in r["reason"]


def test_approved_but_no_resume_is_flagged(monkeypatch):
    # After approval the agent stays waiting_owner for the whole timeout.
    monkeypatch.setenv("AGENT_SUPERVISOR_RESUME_TIMEOUT_SECS", "2")
    import importlib
    importlib.reload(sup)
    monkeypatch.setenv("AGENT_AUTORESOLVE_SESSIONS", "seo-audit")
    statuses = [_status("waiting_owner", SAFE_DIALOG)] + [_status("waiting_owner", SAFE_DIALOG)] * 5
    calls = _wire(monkeypatch, statuses)
    r = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert r["action"] == "approved_no_resume"
    assert r["resumed"] is False
    assert calls["approve"] == 1
    importlib.reload(sup)   # restore default timeout for other tests


def test_allowlist_is_discovered_dynamically_from_config(monkeypatch, tmp_path):
    # No hard-coded names: mode==auto sessions in the orchestrator config are
    # covered automatically; hold/monitor are not. Env is an additive override.
    cfg = tmp_path / "orch.yaml"
    cfg.write_text(
        "sessions:\n"
        "  alpha:\n    mode: auto\n"
        "  bravo:\n    mode: auto\n"
        "  charlie:\n    mode: hold\n"
        "  delta:\n    mode: monitor\n")
    monkeypatch.setenv("AGENT_ORCHESTRATOR_CONFIG", str(cfg))
    monkeypatch.setenv("AGENT_AUTORESOLVE_SESSIONS", "")
    from core import agent_orchestrator as orch
    orch._config_cache = {}
    assert sup._allowlisted_sessions() == {"alpha", "bravo"}      # only auto
    monkeypatch.setenv("AGENT_AUTORESOLVE_SESSIONS", "echo-override")
    assert sup._allowlisted_sessions() == {"alpha", "bravo", "echo-override"}


def test_persistence_survives_restart_semantics(monkeypatch):
    # A decision recorded once is readable again (same sqlite file) — the basis
    # for not re-alerting after a restart.
    ac.record_prompt_decision("seo-audit:0.0", "abc123", "left_for_owner", "denied", "docker restart")
    got = ac.get_prompt_decision("seo-audit:0.0", "abc123")
    assert got["decision"] == "left_for_owner" and got["category"] == "denied"


def test_safe_prompt_resolved_exactly_once_even_if_still_waiting(monkeypatch):
    # If the same safe prompt is still present on a later poll (or the orchestrator
    # loop races the supervisor loop), it must NOT be answered a second time.
    calls = _wire(monkeypatch, [
        _status("waiting_owner", SAFE_DIALOG),
        _status("working", "…(1s · ↓ 2k tokens)"),
    ])
    first = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert first["action"] == "approved" and calls["approve"] == 1
    # Same prompt appears again — persisted decision makes this idempotent.
    _wire(monkeypatch, [_status("waiting_owner", SAFE_DIALOG)])
    # re-wire resets the approve counter; re-attach a shared counter instead.
    calls2 = {"approve": 0}
    monkeypatch.setattr(ac, "approve_prompt",
                        lambda t: calls2.__setitem__("approve", calls2["approve"] + 1) or True)
    second = sup.resolve_target("seo-audit:0.0", approve=True, _sleep=lambda s: None)
    assert second["action"] == "already_resolved"
    assert calls2["approve"] == 0                       # never re-sent the key
    assert second["prior_decision"] == "approved"
