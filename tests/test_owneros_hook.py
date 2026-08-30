"""Native Claude Code lifecycle hooks -> durable Owner OS events.

Owner OS learned that an agent stopped by SCRAPING its tmux pane and classifying the text.
Most of 2026-08-30 was spent repairing that path: prose matching no detector, a background
shell masking a finished turn, an inventory flicker re-announcing an unchanged pane. Claude
Code knows every one of those facts exactly and exposes them as hooks. These tests pin the
translation, and above all pin that observing a session can never break it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HOOK = "/root/ai-dev-runtime/hooks/owneros_hook.py"
sys.path.insert(0, "/root/ai-dev-runtime")


def _map(ev, **payload):
    from hooks.owneros_hook import _map
    return _map(ev, payload)


def _run(payload: dict):
    """The hook as the runtime actually invokes it: JSON on stdin."""
    return subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30)


# ── the mapping: only three things may ever wake ─────────────────────────────
def test_a_turn_boundary_is_a_record_not_a_doorbell():
    """`Stop` fires at the END OF EVERY TURN. Mapping it to a wake would page the owner
    after every reply — the single most important thing this file pins."""
    etype, severity, oar = _map("Stop", last_assistant_message="done")
    assert etype == "agent_turn_stopped"
    assert oar is False and severity == "info"
    from core import wake_bridge as wb
    assert etype not in wb.WAKE_EVENT_TYPES, "a turn boundary must never be wake-capable"
    assert etype in wb.ROUTINE_EVENT_TYPES


def test_subagent_stop_is_also_only_a_record():
    etype, _, oar = _map("SubagentStop", last_assistant_message="sub done")
    assert etype == "agent_subagent_stopped" and oar is False
    from core import wake_bridge as wb
    assert etype in wb.ROUTINE_EVENT_TYPES


def test_needing_input_wakes():
    etype, sev, oar = _map("Notification", notification_type="agent_needs_input",
                           message="Do you want to proceed?")
    assert etype == "agent_waiting_input" and oar is True and sev == "high"


def test_completion_and_failure_wake():
    assert _map("Notification", notification_type="agent_completed")[0] == "task_completed"
    assert _map("TaskCompleted", task_id="t1", task_subject="s")[0] == "task_completed"
    etype, sev, oar = _map("StopFailure", error_details={"code": 500})
    assert etype == "agent_process_failed" and sev == "critical" and oar is True


def test_unrecognised_notifications_are_ignored_entirely():
    """Anything outside the three meaningful types adds no event at all — the matcher is
    narrow on purpose, so this bridge cannot become a chatter source."""
    assert _map("Notification", notification_type="tool_permission") is None
    assert _map("Notification", notification_type="") is None
    assert _map("PreToolUse") is None
    assert _map("UserPromptSubmit", prompt="hi") is None


def test_every_wake_capable_mapping_is_a_class_owner_os_already_routes():
    """No new wake class is invented, so routing, lanes and rate limits apply unchanged."""
    from core import wake_bridge as wb
    for ev, kw in (("Notification", {"notification_type": "agent_needs_input"}),
                   ("Notification", {"notification_type": "agent_completed"}),
                   ("TaskCompleted", {"task_id": "t"}),
                   ("StopFailure", {})):
        m = _map(ev, **kw)
        assert m and m[0] in wb.WAKE_EVENT_TYPES, (ev, m)


# ── it must never break the session it observes ──────────────────────────────
def test_malformed_input_exits_clean():
    r = subprocess.run([sys.executable, HOOK], input="not json at all",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_empty_input_exits_clean():
    r = subprocess.run([sys.executable, HOOK], input="", capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0


def test_an_unknown_event_exits_clean_and_silent():
    r = _run({"hook_event_name": "SomethingNewInAFutureVersion", "session_id": "s"})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_a_recognised_event_still_writes_nothing_to_stdout():
    """stdout is a channel the runtime reads. This bridge must stay silent on it."""
    r = _run({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/opt/x",
              "last_assistant_message": "ok"})
    assert r.returncode == 0 and r.stdout.strip() == ""


# ── identity and dedupe ──────────────────────────────────────────────────────
def test_identity_derives_project_from_cwd_and_never_guesses_a_route():
    from hooks.owneros_hook import _identity
    i = _identity({"cwd": "/opt/diamond/auction", "session_id": "abc123def456789"})
    assert i["project"] == "auction"
    assert "conversation" not in i and "chatgpt" not in json.dumps(i).lower(), \
        "a worker must never carry a raw ChatGPT URL; routing stays central"


def test_a_teammate_name_is_preferred_as_the_agent_identity():
    from hooks.owneros_hook import _identity
    assert _identity({"teammate_name": "worker-a", "cwd": "/opt/x"})["agent"] == "worker-a"


def test_the_hook_loads_the_runtime_env_so_the_wake_bridge_is_actually_enabled(monkeypatch):
    """First live run: two real agent_waiting_input events from native Notification hooks,
    both recorded durably and both with NO wake decision — the bridge read
    WAKE_BRIDGE_ENABLED as unset in the bare hook process. The event log was right and the
    doorbell never rang."""
    import importlib
    monkeypatch.delenv("WAKE_BRIDGE_ENABLED", raising=False)
    mod = importlib.import_module("hooks.owneros_hook")
    mod._load_runtime_env()
    assert os.environ.get("WAKE_BRIDGE_ENABLED"), "the bridge must be enabled in-process"


def test_an_explicit_environment_always_wins_over_the_file(monkeypatch):
    """Tests and hand-runs must never be silently overridden by the service config."""
    import importlib
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "0")
    mod = importlib.import_module("hooks.owneros_hook")
    mod._load_runtime_env()
    assert os.environ["WAKE_BRIDGE_ENABLED"] == "0"


def test_a_missing_env_file_does_not_break_the_hook(monkeypatch):
    import importlib
    mod = importlib.import_module("hooks.owneros_hook")
    monkeypatch.setattr(mod, "_RUNTIME_ENV", "/nonexistent/path/.env")
    mod._load_runtime_env()   # must not raise


# ── event-driven: the hook hands the fact straight on ───────────────────────
def test_a_turn_boundary_triggers_a_supervision_pass(monkeypatch):
    """The supervisor used to learn about a stop on the companion's next tick, costing
    tens of seconds for no reason: the fact arrived in the hook process the instant the
    turn ended."""
    import importlib
    mod = importlib.import_module("hooks.owneros_hook")
    fired = []
    monkeypatch.setattr(mod, "_trigger_supervisor", lambda: fired.append(1))
    monkeypatch.setattr(mod, "_load_runtime_env", lambda: None)

    class _Cto:
        @staticmethod
        def emit(*a, **k):
            return {"event_id": 1}

    import sys as _s
    monkeypatch.setitem(_s.modules, "core.control_plane.cto", _Cto)
    monkeypatch.setattr(_s, "stdin", type("F", (), {"read": staticmethod(lambda: json.dumps(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": "/opt/x",
         "last_assistant_message": "done"}))})())
    mod.main()
    assert fired == [1], "a turn boundary must kick supervision immediately"


def test_a_notification_does_not_trigger_a_supervision_pass(monkeypatch):
    """Only a turn boundary is the supervisor's business; a question is not, so it must
    not spawn a process per notification."""
    import importlib
    mod = importlib.import_module("hooks.owneros_hook")
    fired = []
    monkeypatch.setattr(mod, "_trigger_supervisor", lambda: fired.append(1))
    monkeypatch.setattr(mod, "_load_runtime_env", lambda: None)

    class _Cto:
        @staticmethod
        def emit(*a, **k):
            return {"event_id": 2}

    import sys as _s
    monkeypatch.setitem(_s.modules, "core.control_plane.cto", _Cto)
    monkeypatch.setattr(_s, "stdin", type("F", (), {"read": staticmethod(lambda: json.dumps(
        {"hook_event_name": "Notification", "notification_type": "agent_needs_input",
         "session_id": "s", "cwd": "/opt/x"}))})())
    mod.main()
    assert fired == []


def test_the_trigger_never_blocks_or_raises(monkeypatch):
    """A hook that blocks blocks the session it observes — the one thing this bridge must
    never do. A spawn failure is swallowed; the companion tick remains the fallback."""
    import importlib
    mod = importlib.import_module("hooks.owneros_hook")

    def _boom(*a, **k):
        raise OSError("cannot fork")

    import subprocess as _sp
    monkeypatch.setattr(_sp, "Popen", _boom)
    mod._trigger_supervisor()   # must not raise
