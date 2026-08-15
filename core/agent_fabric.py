"""Agent Fabric v1 — one abstraction over live tmux/Claude agents and Runtime
workers (task OWNER-192).

Design rule: the fabric is a UNIFYING VIEW + LIFECYCLE GATEWAY, never a second
store. tmux agents stay authoritative in core.agent_control (+ the control
plane's `agent` table); runtime workers stay authoritative in core.job_store.
Duplicate protection therefore holds by construction — the fabric cannot drift
from a registry it does not own — and every mutating verb delegates to the
already-hardened primitive with its own gates (duplicate proof, leases,
idempotency keys, dialog fail-closed rules) intact.

Refs (stable addressing):
  tmux:<session:pane>        e.g. tmux:gaika-video:0.0
  runtime:<job-uuid>         e.g. runtime:eda37d2c-...

Verbs: list / status / start_or_resume / send / result / stop / handoff.
Fail-closed: an unknown ref kind, a dead pane, a terminal job, or a duplicate
start is a refusal with a reason, never a guess. The wake/stuchalka paths are
untouched — fabric emits nothing on its own; the sources it reads already do.
"""
from __future__ import annotations

from typing import Optional

_TMUX = "tmux"
_RUNTIME = "runtime"

_RUNTIME_TERMINAL = {"completed", "failed", "cancelled", "blocked", "rolled_back",
                     "fallback_plan_only"}

# runtime job status -> fabric state (Task Contract vocabulary)
_RUNTIME_STATE = {
    "waiting_approval": "OWNER_DECISION",
    "queued": "CREATED",
    "blocked": "BLOCKED",
    "failed": "VERIFICATION_FAILED",
    "completed": "AGENT_DONE",       # completion gate verified it; still surfaced
    "fallback_plan_only": "BLOCKED", # a plan is not the work — needs a real pass
    "cancelled": "CANCELLED",
}
# tmux inventory state -> fabric state
_TMUX_STATE = {
    "working": "WORKING", "shell_running": "WORKING",
    "waiting_owner": "OWNER_DECISION", "waiting_input": "BLOCKED",
    "idle": "WORKING", "unknown": "WORKING",
}


class FabricError(RuntimeError):
    pass


def parse_ref(ref: str) -> tuple[str, str]:
    kind, _, ident = (ref or "").partition(":")
    if kind not in (_TMUX, _RUNTIME) or not ident:
        raise FabricError(f"bad agent ref: {ref!r} (want tmux:<target> or runtime:<job-id>)")
    return kind, ident


def _tmux_entry(a: dict) -> dict:
    state = (a.get("state") or "unknown").strip() or "unknown"
    return {
        "ref": f"tmux:{a.get('target')}",
        "kind": _TMUX,
        "project": (a.get("claude_cwd") or a.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1],
        "server": "local",
        "cwd": a.get("claude_cwd") or a.get("cwd") or "",
        "tmux_target": a.get("target"),
        "session_id": a.get("conversation_id") or "",
        "model": a.get("model") or "",
        "state": state,
        "fabric_state": _TMUX_STATE.get(state, "WORKING"),
        "current_task": a.get("assigned_task") or "",
        "last_activity": a.get("last_activity") or "",
        "alive": bool(a.get("alive")),
        "healthy": bool(a.get("alive")),
        "capabilities": ["send", "read", "stop", "resume"],
    }


def _runtime_entry(j: dict) -> dict:
    status = j.get("status") or ""
    return {
        "ref": f"runtime:{j.get('id')}",
        "kind": _RUNTIME,
        "project": (j.get("project_path") or "").rstrip("/").rsplit("/", 1)[-1],
        "server": "local",
        "cwd": j.get("project_path") or "",
        "tmux_target": "",
        "session_id": "",
        "model": "",
        "state": status,
        "fabric_state": _RUNTIME_STATE.get(status, "WORKING"),
        "current_task": f"OWNER-{j.get('task_id')}: {(j.get('goal') or '')[:120]}",
        "last_activity": j.get("heartbeat_at") or j.get("updated_at") or "",
        "alive": status not in _RUNTIME_TERMINAL,
        "healthy": status not in ("failed", "blocked"),
        "capabilities": ["status", "result", "stop"],
    }


def list_agents(*, include_terminal_jobs: bool = False) -> dict:
    """The unified inventory. tmux from agent_control (live truth), runtime
    from the job store (durable truth)."""
    from core import agent_control, job_store
    entries, errors = [], []
    try:
        for a in agent_control.agent_list().get("agents", []):
            if a.get("is_agent"):
                entries.append(_tmux_entry(a))
    except Exception as e:  # noqa: BLE001 — one source down must not blind the other
        errors.append(f"tmux_inventory_unavailable: {str(e)[:120]}")
    try:
        for j in job_store.list_jobs(limit=100):
            if include_terminal_jobs or (j.get("status") or "") not in _RUNTIME_TERMINAL:
                entries.append(_runtime_entry(j))
    except Exception as e:  # noqa: BLE001
        errors.append(f"runtime_inventory_unavailable: {str(e)[:120]}")
    return {"agents": entries, "count": len(entries), "errors": errors}


def status(ref: str) -> dict:
    kind, ident = parse_ref(ref)
    if kind == _TMUX:
        from core import agent_control
        st = agent_control.agent_status(ident)
        return {"ref": ref, "kind": kind, **st}
    from core import job_store
    j = job_store.get_job(ident)
    if not j:
        raise FabricError(f"no such runtime job: {ident}")
    return {"ref": ref, "kind": kind, **_runtime_entry(j),
            "error": j.get("error"), "outcome": j.get("outcome"),
            "logs_tail": (j.get("logs") or [])[-5:]}


def start_or_resume(project_dir: str, *, conversation_id: Optional[str] = None,
                    by: str = "fabric") -> dict:
    """Start-or-resume with fail-closed no-duplicate semantics: delegates to
    agent_control.agent_resume, whose duplicate proof (one live Claude per cwd)
    and session-liveness checks already hold. Never starts a second agent for
    a directory that has one."""
    from core import agent_control
    live = agent_control.find_live_agent_for_dir(project_dir)
    if live:
        return {"ok": True, "resumed": False, "duplicate_prevented": True,
                "ref": f"tmux:{live.get('target')}",
                "reason": "live agent already exists for this cwd"}
    r = agent_control.agent_resume(project_dir, conversation_id=conversation_id)
    ok = bool(r.get("ok", r.get("started") or r.get("resumed")))
    return {"ok": ok, "resumed": True, "duplicate_prevented": False, **r}


def send(ref: str, text: str, *, idempotency_key: Optional[str] = None) -> dict:
    """Send input to a live tmux agent. Runtime workers take no free-text input
    by design — their instructions are their job row; refusing here is the
    fail-closed answer, not a limitation to paper over."""
    kind, ident = parse_ref(ref)
    if kind == _RUNTIME:
        raise FabricError("runtime workers accept no interactive input; "
                          "create/approve a job through the runtime API instead")
    from core import agent_control
    return agent_control.agent_send(ident, text, idempotency_key=idempotency_key)


def result(ref: str) -> dict:
    kind, ident = parse_ref(ref)
    if kind == _TMUX:
        from core import agent_control
        st = agent_control.agent_status(ident)
        cwd = st.get("claude_cwd") or st.get("cwd") or ""
        return agent_control.agent_report(cwd) if cwd else {"reports": []}
    from core import job_store
    j = job_store.get_job(ident)
    if not j:
        raise FabricError(f"no such runtime job: {ident}")
    return {"id": ident, "status": j.get("status"), "outcome": j.get("outcome"),
            "plan": j.get("plan"), "changed_files": j.get("changed_files"),
            "tests": j.get("tests"), "git_info": j.get("git_info"),
            "error": j.get("error")}


def stop(ref: str, *, confirm: bool = False,
         idempotency_key: Optional[str] = None) -> dict:
    """Stop is destructive; both paths demand explicit confirm."""
    kind, ident = parse_ref(ref)
    if not confirm:
        raise FabricError("stop requires confirm=true")
    if kind == _TMUX:
        from core import agent_control
        return agent_control.agent_stop(ident, confirm=True,
                                        idempotency_key=idempotency_key)
    from core import job_store
    j = job_store.get_job(ident)
    if not j:
        raise FabricError(f"no such runtime job: {ident}")
    if (j.get("status") or "") in _RUNTIME_TERMINAL:
        return {"ok": True, "already_terminal": True, "status": j["status"]}
    out = job_store.update_job(ident, status="cancelled",
                               finished_at=job_store._now())
    return {"ok": True, "already_terminal": False, "status": out["status"]}


def handoff(ref: str, to_project_dir: str, *, note: str = "",
            by: str = "fabric") -> dict:
    """v1 handoff: durable, auditable intent — a CTO event carrying the source
    agent's context pointer and the destination — plus a start-or-resume at the
    destination. No pane surgery: the source keeps running until stopped
    explicitly (a half-moved agent is worse than two live ones)."""
    src = status(ref)
    from core.control_plane.cto import emit
    ev = emit("agent_fabric", "runtime_job_state",
              project_id=(to_project_dir or "").rstrip("/").rsplit("/", 1)[-1],
              agent_id=ref, severity="info", owner_action_required=False,
              payload={"handoff_from": ref, "handoff_to": to_project_dir,
                       "note": note[:300], "source_state": src.get("state")},
              action_taken=f"handoff {ref} -> {to_project_dir}: {note[:120]}",
              correlation_id=f"fabric:handoff:{ref}",
              dedup_key=f"fabric:handoff:{ref}:{to_project_dir}",
              dedup_window_secs=3600)
    dst = start_or_resume(to_project_dir, by=by)
    return {"ok": bool(dst.get("ok")), "event_id": ev.get("event_id"),
            "source": ref, "destination": dst}
