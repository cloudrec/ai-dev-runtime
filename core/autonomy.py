"""Which agents are permitted full autonomy (spec → agent → ChatGPT review → next step).

One answer for every consumer — tools/stage_done.py (may the agent ring the tech lead?)
and core.wake_bridge.compose_phrase (must the tech lead act on the wake?) — so the two can
never disagree about who is in the loop.

Permitted: a session on the automation allowlist (AGENT_AUTOMATION_TARGETS, `session` or
`session@caller`), or one named by an ACTIVE automation grant (its targets, or the session
prefix it creates), or any session at all while an open caller class is configured
(AGENT_AUTOMATION_OPEN_CALLERS). Never a payment session. Fails closed.
"""
from __future__ import annotations


def session_of(agent_id: str) -> str:
    """`gaika-country-pl:0.0` → `gaika-country-pl`."""
    return (agent_id or "").strip().split(":", 1)[0]


def permitted(agent_id: str) -> bool:
    session = session_of(agent_id)
    if not session or session.startswith("session"):
        return False
    try:
        from core import agent_control as ac
        if ac._payment_session(session):
            return False
        if ac.open_callers():
            return True             # the tech lead may drive every non-payment agent
        names = {t.split("@", 1)[0].lower() for t in ac.automation_targets()}
    except Exception:  # noqa: BLE001
        names = set()
    if session.lower() in names:
        return True
    try:
        from core import automation_grants as ag
        for g in ag.list_grants():
            if ag.effective_status(g) != "active":
                continue
            spec = g.get("spec") or {}
            if session in (spec.get("targets") or []):
                return True
            prefix = ((spec.get("create") or {}).get("session_prefix") or "")
            if prefix and session.startswith(prefix):
                return True
    except Exception:  # noqa: BLE001 — unreadable grants: not permitted
        return False
    return False
