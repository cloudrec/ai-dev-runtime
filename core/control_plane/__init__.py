"""Owner OS Control Plane V2 — single durable source of truth + event log.

P0 foundations: the durable substrate (entities, append-only event log, leases,
owner gates, notifications, evidence, decisions, budget) with EXPLICIT unknown /
stale state. No collectors and no actuation live here yet (P1/P2) — this module is
read-only with respect to the legacy control paths and is safe to run alongside
them.

See reports/OWNER_OS_CONTROL_PLANE_V2_ARCHITECTURE.md.
"""
from __future__ import annotations

from core.control_plane import store as store  # noqa: F401
from core.control_plane.api import (  # noqa: F401
    append_event, get_events,
    upsert_project, upsert_work_item,
    upsert_agent, get_agent, list_agents, set_agent_state, is_stale,
    add_evidence, latest_evidence,
    acquire_lease, release_lease, lease_holder, lease_is_current,
    open_gate, answer_gate, get_open_gates,
    enqueue_notification, mark_notification, pending_notifications,
    record_decision, upsert_budget, get_budget,
    register_agent, set_lifecycle, get_registry, find_agent_by_conversation,
)
from core.control_plane.api import (  # noqa: F401
    upsert_channel, get_channel, list_channels,
)
from core.control_plane import cto as cto  # noqa: F401
from core.control_plane import discovery as discovery  # noqa: F401
from core.control_plane import delivery as delivery  # noqa: F401

SCHEMA_VERSION = store.SCHEMA_VERSION
