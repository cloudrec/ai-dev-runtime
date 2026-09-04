#!/usr/bin/env python3
"""Native Claude Code lifecycle hooks -> durable Owner OS events.

WHY THIS EXISTS. Owner OS learns that a managed agent stopped by SCRAPING its tmux pane:
capture the text, classify it, wait for a dwell, guess. That path is what most of
2026-08-30 was spent repairing — prose that matched no detector, a background shell
masking a finished turn, an inventory flicker re-announcing an unchanged pane. Claude
Code itself knows all of these facts exactly, and this build (2.1.251) exposes them as
hooks. A hook is ground truth; a scraped pane is an inference.

DESIGN RULES, each learned from a defect this session:

  * `Stop` fires at the END OF EVERY TURN, not when an agent finally goes idle. Mapping it
    to a wake would page the owner after every reply. It is recorded as a ROUTINE turn
    boundary — durable structure, never a doorbell — and the existing quiescence rule
    still decides what counts as a stop worth waking for.
  * Only three things wake: the agent asking for input, a task completing, and a failure.
    Those are the classes Owner OS already routes and rate-limits, so nothing new has to
    be taught to the wake bridge.
  * A hook must NEVER break the session it observes. Every path exits 0, writes nothing to
    stdout, and swallows every exception. A supervisor that can crash its worker is worse
    than no supervisor.
  * Dedupe is by (session, event, distinguishing payload) so a retried or duplicated hook
    delivery cannot double-wake.

This is ADDITIVE. Nothing here removes or disables the tmux/quiescence path, which stays
as the fallback for crashes, older Claude versions, a missing peer socket, or a hook that
fails to fire at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, "/root/ai-dev-runtime")

_RUNTIME_ENV = "/root/ai-dev-runtime/configs/.env"


def _load_runtime_env() -> None:
    """A hook runs as a bare process with none of the service environment.

    Without this the wake bridge reads `WAKE_BRIDGE_ENABLED` as unset, decides it is
    disabled, and `cto.emit` records the event durably but mints NO wake decision — which
    is exactly what the first live run showed: two real `agent_waiting_input` events from
    native Notification hooks, both with "no decision". The event log was right and the
    doorbell never rang.

    Only keys that are not already set are filled in, so an explicit environment (tests,
    an operator running the hook by hand) always wins.
    """
    try:
        with open(_RUNTIME_ENV, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:  # noqa: BLE001 — a missing env file must not break the session
        pass

def _failure_text(payload: dict) -> str:
    """Everything in a failure payload that could carry a reason, as one string."""
    parts = [str(payload.get("last_assistant_message") or ""),
             str(payload.get("message") or "")]
    details = payload.get("error_details")
    if details:
        parts.append(details if isinstance(details, str)
                     else json.dumps(details, sort_keys=True))
    return " ".join(p for p in parts if p)


def _matches(payload: dict, attr: str) -> bool:
    """Does the failure text match the named `core.agent_watch` vocabulary?

    The vocabulary is deliberately NOT duplicated here: it lives in
    `core.agent_watch`, which already owns these classes, so a banner reworded
    upstream is taught to both doors at once.

    Fails CLOSED. If the classifier cannot be consulted for any reason, the caller
    keeps the critical mapping — an unreadable message must never be the thing that
    turns a real crash silent. Losing a crash costs strictly more than repeating a
    false alarm this fix already narrows.
    """
    try:
        import core.agent_watch as _aw
        text = _failure_text(payload)
        return bool(text.strip()) and bool(getattr(_aw, attr).search(text))
    except Exception:  # noqa: BLE001 — see "fails CLOSED" above
        return False


def _is_provider_limit(payload: dict) -> bool:
    """Does this failure say only that a provider window ran out?"""
    return _matches(payload, "_PROVIDER_LIMIT_RE")


def _is_context_limit(payload: dict) -> bool:
    """Does this failure say only that the context window filled up?

    Event 20289 was this session: severity `critical`, type `agent_process_failed`,
    message "Prompt is too long" — raised while the session was alive and went on to
    compact and keep working. A full context is the harness asking for a reset, not a
    dead process, and paging an owner for it is the same false alarm the provider-limit
    branch already removed. Distinct from that one on purpose: see `_CONTEXT_LIMIT_RE`.
    """
    return _matches(payload, "_CONTEXT_LIMIT_RE")


# Event mapping. LEFT: what Claude Code tells us. RIGHT: the Owner OS class that already
# has routing, a lane and a rate limit. Nothing invents a new wake class.
#
# `agent_turn_stopped` and `agent_subagent_stopped` are RECORDS, not doorbells — they are
# in ROUTINE_EVENT_TYPES so `is_significant` refuses them a wake by name.
_ROUTINE = "info"

def _map(ev: str, payload: dict):
    """(owner-os event type, severity, owner_action_required) or None to ignore."""
    if ev == "Stop":
        return ("agent_turn_stopped", _ROUTINE, False)
    if ev == "SubagentStop":
        return ("agent_subagent_stopped", _ROUTINE, False)
    if ev == "StopFailure":
        # A provider usage window is exhausted. Part 49 gave this its own class after
        # the pane-scraping path read the same banner as BOTH a crash and a finish —
        # but it fixed only that path. The banner arrives here too, as a StopFailure,
        # and this door mapped every StopFailure to a critical owner-actionable crash
        # without ever reading the message. Measured over 24 h: 131 of 138
        # `agent_process_failed` criticals carried it. That is 95% of the most severe
        # alert class in the system describing agents that were alive, had not
        # crashed, had not completed, and needed nothing from an owner — the window
        # resets on its own.
        if _is_provider_limit(payload) or _is_context_limit(payload):
            return ("agent_externally_blocked", "info", False)
        # The turn ended in an error the session could not recover from.
        return ("agent_process_failed", "critical", True)
    if ev == "TaskCompleted":
        return ("task_completed", "high", False)
    if ev == "TeammateIdle":
        return ("agent_turn_stopped", _ROUTINE, False)
    if ev == "Notification":
        # The only branch that can produce an actionable wake, and only for the
        # notification types that genuinely mean a human is being asked.
        t = (payload.get("notification_type") or "").strip()
        if t == "agent_needs_input":
            # The agent is ASKING. That is an owner-facing question.
            return ("agent_waiting_input", "high", True)
        if t == "idle_prompt":
            # "Claude is waiting for your input" fires whenever a pane SITS at the prompt.
            # Idle is not a question, and mapping it to an actionable wake paged the owner
            # for every quiet agent: measured 2026-08-30, 18 of 19 native waiting-input
            # events were idle_prompt and 11 of them became delivered wakes — roughly a
            # dozen owner interruptions an hour saying only "an agent is idle".
            #
            # It is still worth RECORDING: idleness is precisely what the supervisor acts
            # on, and an agent that never ends a turn may emit this when it emits no Stop.
            # So it becomes the same routine turn-boundary record — useful, never a
            # doorbell. This is the `Stop`-fires-every-turn trap in another costume.
            return ("agent_turn_stopped", _ROUTINE, False)
        if t == "agent_completed":
            return ("task_completed", "high", False)
        return None
    return None


def _identity(payload: dict) -> dict:
    """Who this is about, in Owner OS's own terms."""
    cwd = (payload.get("cwd") or os.getcwd() or "").rstrip("/")
    project = cwd.rsplit("/", 1)[-1] if cwd else ""
    # A hook knows its SESSION; the tmux world knows targets. Carry both, and let the
    # registry resolve the route — routing stays centralized, no per-worker URLs.
    agent = (payload.get("teammate_name") or os.environ.get("OWNEROS_AGENT_TARGET")
             or f"session:{(payload.get('session_id') or '')[:12]}")
    return {"project": project, "agent": agent, "cwd": cwd,
            "session_id": payload.get("session_id") or ""}


_TRIGGER = "/root/ai-dev-runtime/tools/native_supervise_once.py"


def _trigger_supervisor() -> None:
    """Kick one supervision pass now, without waiting for the poll and without blocking."""
    try:
        import subprocess
        subprocess.Popen(
            ["/root/ai-dev-runtime/venv/bin/python", _TRIGGER],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:  # noqa: BLE001 — the polled tick remains the fallback
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — a malformed payload must not break the session
        return 0
    try:
        ev = payload.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
        mapped = _map(ev, payload)
        if not mapped:
            return 0
        etype, severity, oar = mapped
        ident = _identity(payload)

        # Distinguishing content per class, so a repeat of the SAME fact dedupes but a new
        # fact does not. Mirrors the digest discipline the pane watcher already uses.
        distinct = {
            "Stop": (payload.get("last_assistant_message") or "")[-400:],
            "SubagentStop": (payload.get("last_assistant_message") or "")[-400:],
            "StopFailure": json.dumps(payload.get("error_details") or {}, sort_keys=True)[:400],
            "TaskCompleted": f"{payload.get('task_id','')}:{payload.get('task_subject','')}",
            "TeammateIdle": payload.get("teammate_name") or "",
            "Notification": f"{payload.get('notification_type','')}:{(payload.get('message') or '')[:200]}",
        }.get(ev, "")
        digest = hashlib.sha256(f"{ident['session_id']}|{ev}|{distinct}".encode()).hexdigest()[:16]

        body = {
            "source": "claude_hook",
            "hook_event": ev,
            "session_id": ident["session_id"],
            "cwd": ident["cwd"],
            "digest": digest,
            # Structured lifecycle detail, straight from the runtime rather than scraped.
            "last_assistant_message": (payload.get("last_assistant_message") or "")[:600],
            "background_tasks": payload.get("background_tasks"),
            "session_crons": payload.get("session_crons"),
            "stop_hook_active": payload.get("stop_hook_active"),
            "notification_type": payload.get("notification_type"),
            "message": (payload.get("message") or "")[:300],
            "task_id": payload.get("task_id"),
            "task_subject": payload.get("task_subject"),
            "teammate_name": payload.get("teammate_name"),
            "error_details": payload.get("error_details"),
        }
        body = {k: v for k, v in body.items() if v not in (None, "", {})}

        _load_runtime_env()
        # REDACT before anything is persisted. Every free-text field above comes
        # straight from the runtime — `last_assistant_message` is 600 characters of model
        # output, `message` and `error_details` are whatever the session was handling —
        # and a pane is exactly as likely to hold a credential as a Windows one is, which
        # is why `windows_bridge` already redacts everything a device returns before it is
        # stored. This path had no redaction at all, and it is the path that persisted
        # `token=` and `password=` values into `agent_turn_stopped` payloads.
        #
        # The digest above is deliberately computed on the RAW text: it is a SHA-256
        # prefix, it leaks nothing, and dedupe must stay stable regardless of what the
        # redactor rewrites.
        #
        # FAIL CLOSED. If the redactor itself raises, the free-text fields are dropped
        # rather than emitted raw — an event missing its excerpt is a small loss, an
        # event carrying a credential is not.
        try:
            from core.agent_control import redact_obj
            body = redact_obj(body)
        except Exception:  # noqa: BLE001
            body = {k: v for k, v in body.items()
                    if k not in ("last_assistant_message", "message", "error_details",
                                 "task_subject")}
            body["redaction"] = "unavailable — free text withheld"
        from core.control_plane import cto
        _emitted = cto.emit("claude_hook", etype, project_id=ident["project"], agent_id=ident["agent"],
                 severity=severity, owner_action_required=oar, payload=body,
                 action_taken=f"{ident['agent']} [{ident['project'] or 'unmapped'}]: {ev}",
                 correlation_id=f"claudehook:{ident['session_id'][:12]}",
                 dedup_key=f"claudehook:{ident['session_id'][:12]}:{ev}:{digest}",
                 dedup_window_secs=900)
        # EVENT-DRIVEN, not polled. The supervisor used to learn about a stop on the
        # companion's next tick, which cost tens of seconds for no reason: the fact
        # arrived here, in this process, the instant the turn ended. Hand it straight on.
        #
        # DETACHED, because a hook that blocks blocks the session it observes — the one
        # thing this bridge must never do. The child runs in its own session with its
        # output discarded, so nothing it does can reach back into the agent's terminal,
        # and a failure to spawn is swallowed like every other failure here: the
        # companion's tick is still there as the fallback path.
        if ev == "Stop" and etype == "agent_turn_stopped":
            _trigger_supervisor()
    except Exception:  # noqa: BLE001 — observation must never break the observed session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
