"""Rendering of runtime notification events into Telegram message text.

This module is intentionally free of I/O so it can be unit tested and reused by
the deployed runtime-poller notification path.  Only ``runtime.job.completed``
rendering is defined here in a rich form; every other event type is passed
through unchanged so existing warning/decision messages keep their wording.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

COMPLETED_EVENT = "runtime.job.completed"

# Ordered (label, payload key) pairs rendered for a completed event.
_COMPLETED_FIELDS = (
    ("Runtime job", "job_id"),
    ("Task", "task_title"),
    ("Outcome", "outcome"),
    ("Project", "project"),
    ("Branch", "branch"),
    ("Commit", "commit"),
    ("Tests", "tests_result"),
    ("Validation", "validation"),
    ("Reason", "reason"),
    ("Duration", "duration"),
    ("Next action", "next_action"),
)

#: Outcomes that must never be announced as a finished implementation. A plan
#: reported as "completed" is what let OWNER-111 look done while the Release
#: Controller did not exist.
_NOT_IMPLEMENTED_OUTCOMES = {"fallback_plan_only"}

_OUTCOME_HEADLINE = {
    "implemented": ("✅", "Runtime job completed"),
    "operational_complete": ("✅", "Runtime job completed"),
    "content_complete": ("✅", "Runtime job completed"),
    "deployment_prepared": ("✅", "Runtime job completed (deployment prepared)"),
    "data_handoff_complete": ("✅", "Runtime job completed (handoff prepared)"),
    "context_restored": ("✅", "Runtime job completed (context restored)"),
    "fallback_plan_only": ("⚠️", "Runtime job produced a PLAN ONLY — NOT implemented"),
    "failed": ("❌", "Runtime job failed"),
}


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_duration(value: Any) -> Optional[str]:
    """Accept seconds (int/float) or a pre-formatted string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        total = int(round(float(value)))
        if total < 0:
            return None
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    return _clean(value)


def _short_commit(value: Any) -> Optional[str]:
    text = _clean(value)
    if text is None:
        return None
    head = text.split()[0]
    if len(head) >= 12 and all(c in "0123456789abcdefABCDEF" for c in head):
        return head[:12]
    return text


def _value_for(key: str, payload: Mapping[str, Any]) -> Optional[str]:
    if key == "duration":
        raw = payload.get("duration")
        if raw is None:
            raw = payload.get("duration_seconds")
        return _format_duration(raw)
    if key == "commit":
        return _short_commit(payload.get("commit") or payload.get("commit_sha"))
    if key == "job_id":
        return _clean(payload.get("job_id") or payload.get("runtime_job_id"))
    if key == "task_title":
        return _clean(payload.get("task_title") or payload.get("title"))
    if key == "tests_result":
        return _clean(payload.get("tests_result") or payload.get("tests"))
    if key == "reason":
        # short cause of failure / of a plan-only result
        return _clean(payload.get("reason") or payload.get("error"))
    if key == "validation":
        return _clean(payload.get("validation") or payload.get("validation_status"))
    return _clean(payload.get(key))


def render_completed(payload: Mapping[str, Any]) -> str:
    """Render a ``runtime.job.completed`` payload into Telegram message text.

    The headline reflects the job's *outcome*, not merely that the pipeline
    reached its end: a `fallback_plan_only` job is announced as a plan, never as
    a completed implementation.

    Unknown / missing fields are omitted rather than rendered as ``None`` so a
    partially populated event still produces a useful message.
    """
    title = _value_for("task_title", payload) or "(untitled task)"
    outcome = _clean(payload.get("outcome"))
    icon, headline = _OUTCOME_HEADLINE.get(outcome or "", ("\u2705", "Runtime job completed"))
    lines = [f"{icon} {headline}: {title}"]
    for label, key in _COMPLETED_FIELDS:
        if key == "task_title":
            continue
        value = _value_for(key, payload)
        if value is not None:
            lines.append(f"{label}: {value}")
    if outcome in _NOT_IMPLEMENTED_OUTCOMES:
        lines.append("This task is NOT done: a plan was recorded, no implementation was made. "
                     "It must be requeued for real implementation and must not be released.")
    return "\n".join(lines)


def render_event(event: Mapping[str, Any]) -> Optional[str]:
    """Render any runtime event.

    Returns ``None`` when the event carries no renderable text, which the caller
    should treat as "nothing to send".
    """
    event_type = _clean(event.get("type") or event.get("event_type")) or ""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        payload = event
    if event_type == COMPLETED_EVENT:
        return render_completed(payload)
    # Preserve existing wording for warning / decision / any other event.
    for key in ("text", "message", "body"):
        existing = _clean(event.get(key)) or _clean(payload.get(key))
        if existing is not None:
            return existing
    return None


def dedupe_key(event: Mapping[str, Any], transport: str, destination: str) -> Dict[str, str]:
    """Build the durable dedupe key for an event/transport/destination triple."""
    event_id = _clean(event.get("event_id") or event.get("id"))
    if event_id is None:
        raise ValueError("event is missing event_id; refusing to dedupe blindly")
    return {
        "event_id": event_id,
        "transport": str(transport).strip(),
        "destination": str(destination).strip(),
    }
