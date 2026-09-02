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


def test_merely_being_idle_is_not_a_question():
    """`idle_prompt` fires whenever a pane SITS at the prompt. Measured 2026-08-30: 18 of
    19 native waiting-input events were idle_prompt and 11 became delivered wakes — about
    a dozen owner interruptions an hour saying only "an agent is idle". It is recorded,
    because idleness is what the supervisor acts on, but it never rings the doorbell."""
    etype, sev, oar = _map("Notification", notification_type="idle_prompt",
                           message="Claude is waiting for your input")
    assert etype == "agent_turn_stopped" and oar is False and sev == "info"
    from core import wake_bridge as wb
    assert etype not in wb.WAKE_EVENT_TYPES
    assert etype in wb.ROUTINE_EVENT_TYPES


def test_the_supervisor_can_still_act_on_an_idle_prompt():
    """Demoting it must not blind the supervisor: idle is exactly its trigger."""
    from core import native_supervisor as ns
    etype, _, _ = _map("Notification", notification_type="idle_prompt")
    assert ns.decide(etype, {})["action"] == "continue"


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


# ── the second door for a provider usage limit (2026-09-01) ─────────────────
# Part 49 gave an exhausted provider window its own class, because the pane-tail
# path read the banner as BOTH a crash and a finish. It fixed one door. The same
# banner also arrives here, as a StopFailure, and this mapping never read the
# message: every StopFailure became a critical, owner-actionable crash.
#
# Measured over 24 h on this host: 131 of 138 `agent_process_failed` criticals
# carried the banner — 95% of the most severe alert class in the system,
# describing agents that were alive, had not crashed, had not completed, and
# needed nothing from an owner.

BANNER = ("You've hit your weekly limit · resets 7pm (Europe/Berlin) "
          "/usage-credits to finish what you're working on")


def test_the_live_banner_is_not_a_crash():
    etype, sev, oar = _map("StopFailure", last_assistant_message=BANNER)
    assert etype == "agent_externally_blocked"
    assert sev == "info" and oar is False


def test_the_banner_does_not_wake_the_owner():
    """The whole point: nobody can act on a quota reset, so it must not ring."""
    etype, _, oar = _map("StopFailure", last_assistant_message=BANNER)
    assert oar is False
    from core import wake_bridge as wb
    assert etype not in wb.WAKE_EVENT_TYPES


def test_a_real_stop_failure_is_still_a_critical_crash():
    """The cheap way to end false crash alarms is to stop reporting crashes."""
    etype, sev, oar = _map("StopFailure", error_details={"code": 500},
                           last_assistant_message="API Error: Connection lost mid-response.")
    assert etype == "agent_process_failed" and sev == "critical" and oar is True


def test_a_stop_failure_with_no_message_at_all_stays_critical():
    etype, sev, oar = _map("StopFailure", error_details={"code": 500})
    assert etype == "agent_process_failed" and sev == "critical" and oar is True


def test_the_banner_is_recognised_in_error_details_too():
    """Which field carries the text is the runtime's choice, not ours."""
    etype, _, oar = _map("StopFailure", error_details={"message": BANNER})
    assert etype == "agent_externally_blocked" and oar is False


def test_a_warning_that_the_limit_is_APPROACHING_is_not_exhaustion():
    """Part 49 drew this line deliberately: a working agent must never be parked
    by a warning."""
    etype, sev, _ = _map("StopFailure",
                         last_assistant_message="You are approaching your weekly limit")
    assert etype == "agent_process_failed" and sev == "critical"


def test_the_classifier_fails_closed(monkeypatch):
    """If the shared vocabulary cannot be consulted, a StopFailure stays critical.
    Losing a real crash costs strictly more than repeating a false alarm."""
    import builtins
    from hooks import owneros_hook as h
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "core.agent_watch":
            raise ImportError("no vocabulary today")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert h._is_provider_limit({"last_assistant_message": BANNER}) is False
    assert h._map("StopFailure", {"last_assistant_message": BANNER})[0] == "agent_process_failed"


def test_both_doors_agree_on_the_same_banner():
    """One vocabulary, deliberately not duplicated: the pane path and the hook path
    must never disagree about the same text."""
    from core import agent_watch as aw
    from hooks import owneros_hook as h
    # state="idle": the pane has STOPPED and the banner is why. An active state
    # deliberately outranks tail text in classify(), so a working agent is never
    # parked by scrollback.
    assert aw.classify(alive=True, is_agent=True, state="idle",
                       tail=BANNER)["cls"] == "provider_limit"
    assert h._is_provider_limit({"last_assistant_message": BANNER}) is True
    assert aw._EVENT_FOR["provider_limit"][0] == \
        h._map("StopFailure", {"last_assistant_message": BANNER})[0]


# ── context exhaustion is not a crash (event 20289, 2026-09-02) ───────────
# Observed live: severity `critical`, type `agent_process_failed`, agent
# `session:b8999cd0-54e`, message "Prompt is too long" — raised while that session
# was alive, and it went on to compact and keep working. Same false-alarm shape the
# provider-limit branch already removed, different cause.

CONTEXT_MSG = "Prompt is too long"


def test_a_full_context_is_not_a_dead_process():
    from hooks import owneros_hook as h
    etype, sev, oar = h._map("StopFailure", {"last_assistant_message": CONTEXT_MSG})
    assert etype == "agent_externally_blocked"
    assert sev == "info"
    assert oar is False, "a context reset asks nothing of an owner"


def test_a_genuine_failure_is_still_critical():
    """The control case. Narrowing the false alarm must not cost a real crash."""
    from hooks import owneros_hook as h
    for msg in ("Traceback (most recent call last): MemoryError",
                "process exited with code 137",
                "the file is too long to read"):
        etype, sev, oar = h._map("StopFailure", {"last_assistant_message": msg})
        assert (etype, sev, oar) == ("agent_process_failed", "critical", True), msg
    # No reason text at all is the most dangerous case: it must stay critical.
    assert h._map("StopFailure", {})[1] == "critical"


def test_context_and_provider_limits_stay_distinct():
    """Not folded into one vocabulary on purpose: a quota window reopens on a clock,
    a full context is the harness asking for a reset. `_classify` must never call
    context exhaustion `provider_usage_window_exhausted`."""
    from core import agent_watch as aw
    from hooks import owneros_hook as h
    assert h._is_context_limit({"last_assistant_message": CONTEXT_MSG}) is True
    assert h._is_provider_limit({"last_assistant_message": CONTEXT_MSG}) is False
    assert h._is_context_limit({"last_assistant_message": BANNER}) is False
    assert not aw._PROVIDER_LIMIT_RE.search(CONTEXT_MSG)
    assert aw._CONTEXT_LIMIT_RE.search(CONTEXT_MSG)


def test_context_limit_is_read_from_every_reason_field():
    from hooks import owneros_hook as h
    api = "input length and `max_tokens` exceed context limit"
    assert h._is_context_limit({"message": api}) is True
    assert h._is_context_limit({"error_details": {"error": api}}) is True
    assert h._map("StopFailure", {"error_details": {"error": api}})[1] == "info"


def test_context_limit_fails_closed_when_the_vocabulary_is_unreachable(monkeypatch):
    """An unreadable classifier must never be the thing that silences a real crash."""
    import builtins
    from hooks import owneros_hook as h
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "core.agent_watch":
            raise ImportError("no vocabulary today")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert h._is_context_limit({"last_assistant_message": CONTEXT_MSG}) is False
    assert h._map("StopFailure",
                  {"last_assistant_message": CONTEXT_MSG})[0] == "agent_process_failed"


# ── the fallback branches that have never fired (2026-09-01) ───────────────
# The native-first audit measured which hooks actually arrive on this host over
# 24 h: `Stop` 639, `Notification` 303 (every one of them `idle_prompt`),
# `StopFailure` 136, `SubagentStop` 124. `TaskCompleted` and `TeammateIdle` are
# registered and fired ZERO times, as did the `agent_needs_input` and
# `agent_completed` notification subtypes.
#
# The audit's conclusion was to KEEP them: they cost nothing per event that never
# arrives, and deleting them would trade a real fallback — for a host that does
# use teammates or tasks — for no gain. What they lacked was proof they are
# correct if they ever do fire, which is what a fallback is for. `TaskCompleted`
# was already covered; `TeammateIdle` was not, so a branch nobody exercises could
# have rotted unnoticed.

def test_a_teammate_going_idle_is_a_record_not_a_doorbell():
    """Never fires here. If it ever does, it must not page the owner — `TeammateIdle`
    is the same every-turn trap as `Stop` and `idle_prompt` in a third costume."""
    etype, severity, oar = _map("TeammateIdle", teammate_name="reviewer")
    assert etype == "agent_turn_stopped"
    assert oar is False and severity == "info"
    from core import wake_bridge as wb
    assert etype not in wb.WAKE_EVENT_TYPES
    assert etype in wb.ROUTINE_EVENT_TYPES


def test_the_never_seen_branches_still_map_to_known_classes():
    """A fallback that emits a class the wake bridge has never heard of would be
    recorded and then silently ignored."""
    from core import wake_bridge as wb
    known = set(wb.WAKE_EVENT_TYPES) | set(wb.ROUTINE_EVENT_TYPES)
    for ev, payload in (("TaskCompleted", {"task_id": "t", "task_subject": "s"}),
                        ("TeammateIdle", {"teammate_name": "reviewer"}),
                        ("Notification", {"notification_type": "agent_needs_input"}),
                        ("Notification", {"notification_type": "agent_completed"})):
        etype, _sev, _oar = _map(ev, **payload)
        assert etype in known, f"{ev}/{payload} maps to an unknown class {etype}"
