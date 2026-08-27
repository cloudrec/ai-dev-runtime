"""PHASE 13 — stable versioned Runtime API (/api/v1). Owner OS connects here."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from core import ai_planner, deliver as deliver_mod, job_executor, job_store

router = APIRouter(prefix="/api/v1", tags=["runtime-v1"])

_TOKEN = os.getenv("RUNTIME_TOKEN", "").strip()
_ALLOWED_ROOTS = [r.strip() for r in os.getenv("RUNTIME_ALLOWED_ROOTS", "/tmp,/opt,/root/ai-dev-runtime").split(",") if r.strip()]
_REPLAY_WINDOW = 300
_DANGEROUS_RE = re.compile(
    r"(drop\s+(database|table))|(rm\s+-rf)|(force[- ]?push)|(reset\s+--hard)|(delete\s+(project|user|backup|database))|"
    r"(truncate)|(firewall)|(iptables)|(\bdns\b)|(ssh\s+key)|(rotate\s+secret)|(disable\s+(security|logging|auth))",
    re.IGNORECASE)


# ── auth (bearer + optional HMAC, constant-time, replay-protected) ──────────
async def _auth(request: Request,
                authorization: Optional[str] = Header(None),
                x_runtime_timestamp: Optional[str] = Header(None),
                x_runtime_signature: Optional[str] = Header(None)) -> bool:
    if not _TOKEN:
        raise HTTPException(status_code=503, detail="runtime auth not configured (RUNTIME_TOKEN unset)")
    # HMAC path (preferred)
    if x_runtime_signature and x_runtime_timestamp:
        try:
            ts = int(x_runtime_timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="bad timestamp")
        if abs(time.time() - ts) > _REPLAY_WINDOW:
            raise HTTPException(status_code=401, detail="stale request (replay window)")
        expected = hmac.new(_TOKEN.encode(), x_runtime_timestamp.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_runtime_signature):
            raise HTTPException(status_code=401, detail="bad signature")
        request.state.auth_method = "hmac"
        return True
    # Bearer path
    if authorization and authorization.startswith("Bearer "):
        if hmac.compare_digest(authorization[7:].strip(), _TOKEN):
            request.state.auth_method = "bearer"
            return True
    raise HTTPException(status_code=401, detail="unauthorized")


# ── caller attribution (observability only — no gate reads these) ───────────
# `deliveries` recorded WHAT was delivered but never WHO sent it, so attributing a row
# meant correlating the access log, the docker network and the caller's source by hand
# (reports/ACTUATOR_BLIND_PANE_AND_DELIVERY_ATTRIBUTION_2026-08-04.md). The API knows
# the authenticated principal and the client address at request time; it now passes both
# down to the delivery record instead of discarding them.
_ACTOR_MAX = 120
_SOURCE_MAX = 160
# A caller MAY name itself (e.g. "chatgpt-mcp"). Self-declared and therefore NOT
# trustworthy for any decision — it is recorded next to the auth method, which is.
_ACTOR_SAFE_RE = re.compile(r"[^A-Za-z0-9 ._:@/+-]")


def caller_identity(request: Optional[Request], declared: Optional[str] = None) -> tuple:
    """(actor, source) for a delivery record. Never raises: attribution must not be able
    to break a delivery, so every field degrades to "unknown" rather than failing."""
    method = "unknown"
    try:
        method = getattr(request.state, "auth_method", None) or "unknown"
    except Exception:  # noqa: BLE001
        method = "unknown"
    name = _ACTOR_SAFE_RE.sub("", (declared or "").strip())[:64]
    actor = f"api:{method}" + (f"/{name}" if name else "")
    host = port = agent = ""
    try:
        client = getattr(request, "client", None)
        host = getattr(client, "host", "") or ""
        port = str(getattr(client, "port", "") or "")
        agent = (request.headers.get("user-agent") or "")[:60]
    except Exception:  # noqa: BLE001
        pass
    source = f"{host or 'unknown'}{':' + port if port else ''}"
    if agent:
        source += f" ua={_ACTOR_SAFE_RE.sub('', agent)}"
    return actor[:_ACTOR_MAX], source[:_SOURCE_MAX]


def _validate_project_path(path: str) -> str:
    path = os.path.realpath(path or "")
    if ".." in (path or "").split(os.sep):
        raise HTTPException(status_code=400, detail="path traversal")
    if not any(path == r or path.startswith(r.rstrip("/") + "/") for r in _ALLOWED_ROOTS):
        raise HTTPException(status_code=403, detail=f"project_path outside allowed roots {_ALLOWED_ROOTS}")
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="project_path is not a directory")
    return path


def _view(job: dict) -> dict:
    """Shape the Owner-OS connector expects (+extras)."""
    plan = job.get("plan") or {}
    diff = ""
    for c in (job.get("changed_files") or []):
        diff += f"{c.get('operation','?')} {c.get('path','?')}  ({c.get('before','')}->{c.get('after','')})\n"
    return {
        "id": job["id"], "status": job["status"], "project_id": job.get("project_id"),
        "task_id": job.get("task_id"), "goal": job.get("goal"),
        "plan": (plan.get("summary") or "") + ("\n" + "\n".join(
            f"- {f.get('operation')} {f.get('path')}" for f in plan.get("files", [])) if plan.get("files") else ""),
        "diff": diff, "changed_files": [c.get("path") for c in (job.get("changed_files") or [])],
        "test_results": job.get("tests") or {}, "commit_hash": (job.get("git_info") or {}).get("commit"),
        "branch": (job.get("git_info") or {}).get("branch"),
        "pushed": (job.get("git_info") or {}).get("pushed"),
        "risk_level": job.get("risk_level"), "autonomy_level": job.get("autonomy_level"),
        "requires_approval": job.get("approval_required"), "dangerous": job.get("dangerous"),
        "logs": job.get("logs") or [], "error": job.get("error"),
        "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
        # task 220: what tier was asked for. The tier actually DISPATCHED is in
        # the model_selection artifact, because an explicit ask that fails the
        # escalation gate is honestly reported as sonnet, not as what was wanted.
        "requested_model": job.get("requested_model"),
    }


@router.get("/health")
async def health():
    return {"status": "ok", "provider_available": ai_planner.available(),
            "jobs_total": len(job_store.list_jobs(limit=1000)), "version": "v1"}


class JobCreate(BaseModel):
    project_path: str
    goal: Optional[str] = None
    title: Optional[str] = None          # alias
    instructions: Optional[str] = None
    instruction: Optional[str] = None    # alias (Owner-OS connector)
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    autonomy: Optional[str] = None       # alias
    autonomy_level: Optional[str] = None
    allowed_paths: Optional[list] = None
    forbidden_paths: Optional[list] = None
    base_branch: str = "master"
    auto_commit: bool = True
    auto_push: bool = False
    approval_required: Optional[bool] = None
    # task 220 — explicit model selection for THIS job, plus the justification
    # the model router's hard gate needs before it will actually dispatch on an
    # expensive tier. Both are durable columns on the jobs table; neither is a
    # bypass of the risk floor or of the escalation gate.
    requested_model: Optional[str] = None
    escalation_reason: Optional[dict] = None


@router.post("/jobs")
async def create_job(req: JobCreate, _: bool = Depends(_auth)):
    pp = _validate_project_path(req.project_path)
    goal = req.goal or req.title or "runtime task"
    instructions = req.instructions or req.instruction or goal
    autonomy = req.autonomy_level or req.autonomy or "prepare"
    if autonomy not in job_executor.AUTONOMY_ORDER:
        raise HTTPException(status_code=422, detail=f"autonomy must be one of {job_executor.AUTONOMY_ORDER}")
    dangerous = bool(_DANGEROUS_RE.search(f"{goal} {instructions}"))
    if req.requested_model is not None:
        from core import model_router as _mr
        if req.requested_model not in _mr.RANK:
            raise HTTPException(status_code=422,
                                detail=f"requested_model must be one of {tuple(_mr.RANK)}")
    # approval required unless execute_full+ and not dangerous
    auto_ok = job_executor._idx(autonomy) >= job_executor._idx("execute_full") and not dangerous
    approval_required = req.approval_required if req.approval_required is not None else (not auto_ok)
    job = job_store.create_job(
        project_id=req.project_id, project_path=pp, task_id=req.task_id, goal=goal, instructions=instructions,
        autonomy_level=autonomy, allowed_paths=req.allowed_paths, forbidden_paths=req.forbidden_paths,
        base_branch=req.base_branch, auto_commit=req.auto_commit, auto_push=req.auto_push,
        approval_required=approval_required, dangerous=int(dangerous),
        requested_model=req.requested_model, escalation_reason=req.escalation_reason,
        status="waiting_approval" if approval_required else "queued",
    )
    job_store.append_log(job["id"], "info", f"created (autonomy={autonomy}, approval_required={approval_required}, dangerous={dangerous})")
    if not approval_required:
        job_executor.execute_async(job["id"])
    return _view(job)


@router.get("/jobs")
async def list_jobs(status: Optional[str] = None, _: bool = Depends(_auth)):
    return {"jobs": [_view(j) for j in job_store.list_jobs(limit=100, status=status)]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _: bool = Depends(_auth)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    return _view(j)


@router.post("/jobs/{job_id}/approve")
async def approve(job_id: str, _: bool = Depends(_auth)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    if j["status"] in ("completed", "cancelled", "failed"):
        raise HTTPException(status_code=409, detail=f"job already {j['status']}")
    job_store.update_job(job_id, status="queued", approval_required=0)
    job_store.append_log(job_id, "info", "approved")
    job_executor.execute_async(job_id)
    return _view(job_store.get_job(job_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, _: bool = Depends(_auth)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    job_store.update_job(job_id, status="cancelled", finished_at=job_store._now())
    job_store.append_log(job_id, "info", "cancelled")
    return _view(job_store.get_job(job_id))


@router.get("/jobs/{job_id}/artifacts")
async def artifacts(job_id: str, _: bool = Depends(_auth)):
    j = job_store.get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="not found")
    return {"id": job_id, "plan": j.get("plan"), "changed_files": j.get("changed_files"),
            "tests": j.get("tests"), "git_info": j.get("git_info"), "logs": j.get("logs"),
            "report_path": f"/opt/seo/reports/runtime/{job_id}.md"}


# ── PHASE 45: provider smoke test ────────────────────────────────────────────
# The runtime half of a contract that had only ever existed on the caller's
# side: /opt/seo/backend/services/runtime_client.py:provider_smoke() POSTs
# `{RUNTIME_URL}/smoke` to gate every retry dispatch, and its own comment cites
# "ai-dev-runtime's POST /api/v1/smoke" — a route that was never implemented
# here. Every retry therefore failed the gate with HTTP 404 Not Found and no
# replacement coding job was ever dispatched (owner_task #220 / runtime job
# #81; the same 404 is recorded against job #72 in
# reports/OWNER_OS_WAKE_CONTINUATION_HANDOFF_2026-08-13.md).
#
# Read-only by construction: core.ai_planner.smoke() makes exactly one
# non-agentic provider call with no project_path, no tools, no file or DB
# writes, one hard-capped timeout and no internal retry.
class SmokeReq(BaseModel):
    model: Optional[str] = None
    timeout_seconds: Optional[float] = None


@router.post("/smoke")
async def provider_smoke(req: Optional[SmokeReq] = None, _: bool = Depends(_auth)):
    """{ok, provider, model, latency_seconds, tokens{input_tokens,output_tokens},
    cost_usd, error} — the exact shape runtime_client.provider_smoke() reads.
    A provider failure is a 200 with ok=false, not an HTTP error: the caller
    gates on the body, and a transport-level failure must stay distinguishable
    from "the provider answered and said no"."""
    req = req or SmokeReq()
    return ai_planner.smoke(model=req.model, timeout_seconds=req.timeout_seconds)


# ── PHASE 17: deterministic delivery (merge → test → push), no AI ────────────
class DeliverReq(BaseModel):
    project_path: str
    source_branch: str
    target_branch: str = "master"
    test_commands: Optional[list] = None
    push: bool = False


@router.post("/deliver")
async def deliver(req: DeliverReq, _: bool = Depends(_auth)):
    pp = _validate_project_path(req.project_path)
    if _DANGEROUS_RE.search(f"{req.source_branch} {req.target_branch}"):
        raise HTTPException(status_code=422, detail="dangerous branch name")
    return deliver_mod.deliver(pp, req.source_branch, req.target_branch, req.test_commands, req.push)


class RollbackReq(BaseModel):
    project_path: str
    target_branch: str = "master"
    to_commit: str
    push: bool = False


@router.post("/rollback")
async def rollback(req: RollbackReq, _: bool = Depends(_auth)):
    pp = _validate_project_path(req.project_path)
    return deliver_mod.rollback(pp, req.target_branch, req.to_commit, req.push)


# ── Direct Agent Control Plane: manage the Claude agents already in tmux ─────
# The Owner OS MCP server runs in a container with no tmux socket and no host
# PID namespace, so it cannot address these agents itself. It proxies here.
# There is deliberately no arbitrary-command endpoint: every route below maps to
# one bounded, validated, audited operation in core.agent_control.
from core import agent_control  # noqa: E402


def _agent_call(fn, *args, **kwargs):
    """Map control-plane refusals to 400s and keep tmux failures off the wire."""
    try:
        return fn(*args, **kwargs)
    except agent_control.AgentControlError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/agents")
async def agents_list(_: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_list)


@router.get("/agents/status")
async def agents_status(target: str, _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_status, target)


@router.get("/agents/read")
async def agents_read(target: str, lines: int = agent_control.DEFAULT_CAPTURE_LINES,
                      _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_read, target, lines)


class AgentSendReq(BaseModel):
    target: str
    text: str
    idempotency_key: Optional[str] = None


@router.post("/agents/send")
async def agents_send(req: AgentSendReq, request: Request,
                      x_runtime_actor: Optional[str] = Header(None),
                      _: bool = Depends(_auth)):
    actor, source = caller_identity(request, x_runtime_actor)
    return _agent_call(agent_control.agent_send, req.target, req.text, req.idempotency_key,
                       actor=actor, source=source)


@router.post("/agents/answer")
async def agents_answer(req: AgentSendReq, request: Request,
                        x_runtime_actor: Optional[str] = Header(None),
                        _: bool = Depends(_auth)):
    actor, source = caller_identity(request, x_runtime_actor)
    return _agent_call(agent_control.agent_answer, req.target, req.text, req.idempotency_key,
                       actor=actor, source=source)


class AgentResumeReq(BaseModel):
    project_dir: str
    conversation_id: Optional[str] = None
    session_name: Optional[str] = None


@router.post("/agents/resume")
async def agents_resume(req: AgentResumeReq, _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_resume, req.project_dir,
                       req.conversation_id, req.session_name)


@router.get("/agents/report")
async def agents_report(project_dir: str, limit: int = 20, path: Optional[str] = None,
                        _: bool = Depends(_auth)):
    if path:
        return _agent_call(agent_control.agent_report_read, project_dir, path)
    return _agent_call(agent_control.agent_report, project_dir, limit)


class AgentStopReq(BaseModel):
    target: str
    confirm: bool = False
    idempotency_key: Optional[str] = None


@router.post("/agents/stop")
async def agents_stop(req: AgentStopReq, _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_stop, req.target, req.confirm, req.idempotency_key)


# ── Agent Supervisor: auto-resolve provably-safe permission prompts ─────────
from core import agent_supervisor  # noqa: E402


class AgentResolveReq(BaseModel):
    target: str
    approve: bool = False        # default is a dry-run classification, no keystroke


@router.post("/agents/resolve")
async def agents_resolve(req: AgentResolveReq, _: bool = Depends(_auth)):
    """Classify (and optionally confirm) one agent's pending permission prompt.
    approve=false is a dry-run: it reports the decision without sending anything."""
    try:
        return agent_supervisor.resolve_target(req.target, approve=req.approve)
    except agent_control.AgentControlError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/agents/supervise")
async def agents_supervise(_: bool = Depends(_auth)):
    """Run one supervision sweep now (dry-run over allowlisted sessions)."""
    return agent_supervisor.poll_once(approve=False)


# ── Autonomous Agent Orchestrator ───────────────────────────────────────────
from core import agent_orchestrator  # noqa: E402


@router.get("/agents/orchestrator")
async def agents_orchestrator_status(_: bool = Depends(_auth)):
    """Read-only orchestrator status: per-agent records, states, budget gate."""
    return agent_orchestrator.status()


@router.post("/agents/orchestrator/tick")
async def agents_orchestrator_tick(approve: bool = False, _: bool = Depends(_auth)):
    """Run one orchestration sweep. approve=false is a dry-run (no keystroke)."""
    return agent_orchestrator.refresh_and_resolve(approve=approve)


@router.get("/agents/orchestrator/plan")
async def agents_orchestrator_plan(_: bool = Depends(_auth)):
    """Read-only orchestrator plan: goal, queue, assignments, ticks, dispatches."""
    from core import orchestrator_plan as plan
    return plan.status()


class GoalReq(BaseModel):
    text: str


@router.post("/agents/orchestrator/goal")
async def agents_set_goal(req: GoalReq, _: bool = Depends(_auth)):
    from core import orchestrator_plan as plan
    return plan.set_goal(req.text)


class TaskReq(BaseModel):
    project: str
    title: str
    task_text: str
    agent: str = ""
    order_index: int = 0
    priority: int = 0
    depends_on: list[int] = []
    status: str = "pending"
    handoff_path: str = ""
    approved_scope: str = ""
    completion_marker: str = ""
    note: str = ""


@router.post("/agents/orchestrator/task")
async def agents_add_task(req: TaskReq, _: bool = Depends(_auth)):
    from core import orchestrator_plan as plan
    goal = plan.get_active_goal()
    if not goal:
        raise HTTPException(status_code=400, detail="no active goal")
    tid = plan.add_task(goal["id"], req.project, req.title, req.task_text, agent=req.agent,
                        order_index=req.order_index, priority=req.priority, depends_on=req.depends_on,
                        status=req.status, handoff_path=req.handoff_path,
                        approved_scope=req.approved_scope, completion_marker=req.completion_marker,
                        note=req.note)
    return {"task_id": tid}


class TaskStatusReq(BaseModel):
    task_id: int
    status: str
    note: str = ""


@router.post("/agents/orchestrator/task-status")
async def agents_task_status(req: TaskStatusReq, _: bool = Depends(_auth)):
    from core import orchestrator_plan as plan
    if req.status == "completed":
        plan.mark_completed(req.task_id, next_action=req.note)
    else:
        plan.mark_status(req.task_id, req.status, req.note)
    return {"task_id": req.task_id, "status": req.status}


@router.get("/agents/commander/events")
async def agents_commander_events(unacked: bool = True, limit: int = 50, _: bool = Depends(_auth)):
    """Durable Commander events (checkpoint/completion/waiting-external/…) for
    proactive delivery by Owner OS — no polling of the raw record needed."""
    from core import agent_control as _ac
    return {"events": _ac.list_commander_events(limit=limit, unacked_only=unacked)}


@router.get("/agents/direct-lifecycle/metrics")
async def agents_direct_lifecycle_metrics(_: bool = Depends(_auth)):
    """Direct-agent lifecycle counters (agents observed, completion/dead
    candidates, emitted events, duplicate + insufficient-evidence suppressions,
    delivery outcomes). Read-only."""
    from core import direct_agent_lifecycle as _dal
    return {"enabled": _dal.ENABLED, "metrics": _dal.metrics()}


@router.get("/agents/continuation-watchdog/health")
async def agents_continuation_watchdog_health(_: bool = Depends(_auth)):
    """Continuation-watchdog health + last action (last run, agents checked,
    submitted / verified / retried / blocked / errors). Read-only."""
    from core import agent_continuation_watchdog as _cw
    return _cw.health()


# ── Control Plane V2 (CTO inbox + registry + delivery health) ────────────────
@router.get("/control-plane/cto/brief")
async def control_plane_cto_brief(consumer: str = "chatgpt", limit: int = 200,
                                  ack: bool = False, _: bool = Depends(_auth)):
    """Verified event deltas since the consumer's durable cursor. Set ack=true only
    after the batch is durably processed. Never returns cached prose."""
    from core.control_plane import cto
    return cto.cto_brief_since(consumer, limit=limit, ack=ack)


class CtoAckReq(BaseModel):
    consumer: str = "chatgpt"
    through_event_id: int


@router.post("/control-plane/cto/ack")
async def control_plane_cto_ack(req: CtoAckReq, _: bool = Depends(_auth)):
    """Acknowledge the CTO inbox cursor up to a specific event id (monotonic)."""
    from core.control_plane import cto
    return cto.ack_through(req.consumer, req.through_event_id)


@router.get("/control-plane/wake/pending")
async def control_plane_wake_pending(_: bool = Depends(_auth)):
    """What the wake companion polls. Returns the fixed phrase and the event to acknowledge,
    or nothing at all. Carries NO event content — the companion never learns why it woke."""
    from core import wake_bridge as wb
    h = wb.health()
    pending = (h.get("last_wake_event_id") if h.get("enabled")
               and h.get("last_wake_acknowledged") is False else None)
    return {"wake": pending is not None, "event_id": pending,
            "phrase": wb.WAKE_PHRASE if pending is not None else None,
            "enabled": h.get("enabled"), "kill_switch": h.get("kill_switch")}


class WakeAckReq(BaseModel):
    event_id: int


@router.post("/control-plane/wake/ack")
async def control_plane_wake_ack(req: WakeAckReq, _: bool = Depends(_auth)):
    """The companion confirms it submitted the phrase. Stops further wakes for this event."""
    from core import wake_bridge as wb
    return wb.acknowledge(req.event_id)


@router.get("/control-plane/wake/health")
async def control_plane_wake_health(_: bool = Depends(_auth)):
    """Freshness for the owner: enabled, kill switch, last wake and whether it was acked."""
    from core import wake_bridge as wb
    return wb.health()


# ── wake route registry: the multi-chat routing table, manageable from the chat ─────
# Before these existed, binding a project's work chat meant a server CLI session. A chat
# the owner is already in can now bind itself: the owner pastes the conversation URL, the
# assistant calls bind_wake_route. Validation and audit live in core.wake_routes — the API
# adds nothing but transport, so there is still exactly one writer path.

@router.get("/control-plane/wake/routes", operation_id="list_wake_routes")
async def list_wake_routes(_: bool = Depends(_auth)):
    """The wake route registry: which ChatGPT conversation each project's events wake.
    Read-only; no secrets — route keys, conversation URLs and bind audit fields only."""
    from core import wake_routes as wr
    return {"routes": wr.list_routes(), "fallback_route": wr.FALLBACK_ROUTE}


class WakeRouteBindReq(BaseModel):
    route_key: str
    conversation_url: str
    note: str = ""
    actor: Optional[str] = None      # self-declared caller name, audit only


@router.post("/control-plane/wake/routes/bind", operation_id="bind_wake_route")
async def bind_wake_route(req: WakeRouteBindReq, request: Request,
                          _: bool = Depends(_auth)):
    """Bind or rebind ONE route to ONE ChatGPT conversation URL. Owner-directed config
    mutation: strict URL validation, idempotent (re-binding the held URL is a no-op),
    audited with the caller identity. Fails closed with 400 before any write on a
    malformed key or URL — never guesses either."""
    from core import wake_routes as wr
    actor, _src = caller_identity(request, req.actor)
    res = wr.bind_route(req.route_key, req.conversation_url, by=actor,
                        note=req.note or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res)
    return res


@router.get("/control-plane/wake/chats", operation_id="list_chat_inventory")
async def list_chat_inventory(active_only: bool = False, _: bool = Depends(_auth)):
    """The chat inventory: every ChatGPT conversation the companion browser has actually
    observed — title, first/last seen, writability evidence, inferred route, liveness —
    plus the current route registry, so inventory and routing are one read. Read-only."""
    from core import chat_registry as cr
    from core import wake_routes as wr
    return {"chats": cr.list_chats(active_only=active_only),
            "routes": wr.list_routes(), "fallback_route": wr.FALLBACK_ROUTE}


@router.get("/control-plane/wake/alerts", operation_id="list_agent_alerts")
async def list_agent_alerts(limit: int = 30, _: bool = Depends(_auth)):
    """Agent-derived owner alerts (source=agent_watch): waiting prompts, blockers,
    completions, crashes — the history behind the wakes. Read-only."""
    from core import agent_watch
    return {"alerts": agent_watch.recent_alerts(limit=max(1, min(int(limit), 100)))}


@router.get("/control-plane/wake/routes/resolve", operation_id="resolve_wake_route")
async def resolve_wake_route(project_id: str = "", source: str = "", agent_id: str = "",
                             _: bool = Depends(_auth)):
    """Read-only: which conversation an event with this metadata would wake, and why
    (explicit_route / owner_os_route / unmapped_route:<key> / not bound). Changes nothing."""
    from core import wake_routes as wr
    return wr.resolve(project_id=project_id, source=source, agent_id=agent_id)


@router.get("/control-plane/registry")
async def control_plane_registry(_: bool = Depends(_auth)):
    """The auto-discovered AgentRegistry (visibility never depends on an allowlist)."""
    from core.control_plane import api as _cp
    return {"agents": _cp.get_registry(), "open_gates": _cp.get_open_gates()}


@router.get("/control-plane/notifications/status")
async def control_plane_notifications_status(_: bool = Depends(_auth)):
    """Fail-closed delivery posture. RED (notifications_enabled=false) is a health
    error, never healthy; same_chat_wake_complete is true only with a proven E2E turn."""
    from core.control_plane import delivery
    return delivery.notifications_status()


@router.get("/policy/explain")
async def policy_explain(action: str, declared_risk: Optional[str] = None,
                         owner_approved: bool = False, _: bool = Depends(_auth)):
    """What the Operating Constitution would decide about an action, and why.

    Side-effect free: it creates no claim and consumes no override, so an operator (or an
    agent deciding whether to ask) can query the policy without changing its state.
    """
    from core import policy_engine
    try:
        return policy_engine.explain(action, declared_risk=declared_risk,
                                     owner_approved=owner_approved)
    except policy_engine.PolicyError as e:
        raise HTTPException(status_code=503, detail=f"policy unavailable: {e}")


@router.get("/policy/decisions")
async def policy_decisions(task_id: str = "", limit: int = 50, _: bool = Depends(_auth)):
    """The durable audit: every preflight/completion evaluation, allowed and blocked
    alike, with the rules violated and the evidence that was missing."""
    from core import policy_engine
    return {"decisions": policy_engine.decisions(task_id=task_id, limit=limit)}


@router.get("/policy/overrides")
async def policy_overrides(include_expired: bool = True, _: bool = Depends(_auth)):
    """Every emergency override ever granted — active, expired and revoked. An override
    is never hidden from this list; that is the point of it being owner-scoped."""
    from core import policy_engine
    return {"overrides": policy_engine.list_overrides(include_expired=include_expired)}


@router.get("/control-plane/observability")
async def control_plane_observability(_: bool = Depends(_auth)):
    """Read-only observability: failed runtime jobs + dead-lettered notifications split
    into HISTORICAL vs ACTIVE. `all_clear` is true when there are no ACTIVE (recent)
    failures, so stale historical counters do not flag a healthy system."""
    from core.control_plane import diagnostics
    return diagnostics.observability_summary()


@router.get("/runtime/status")
async def runtime_status(_: bool = Depends(_auth)):
    """Runtime job blockers beside the tmux agent view: active jobs with liveness
    evidence, watchdog stall verdicts, open approvals, recent failures with cause."""
    from core.control_plane import diagnostics
    return diagnostics.runtime_blockers_report()


# ── Agent Fabric v1 (task OWNER-192): one view + lifecycle gateway ───────────
class FabricSend(BaseModel):
    text: str
    idempotency_key: Optional[str] = None


class FabricStart(BaseModel):
    # A Windows workspace is addressed by ref (win:<device>:<workspace>) rather
    # than by path — its real path never crosses the wire — so project_dir is
    # optional when `ref` is given.
    project_dir: str = ""
    conversation_id: Optional[str] = None
    ref: str = ""
    text: str = ""


class FabricStop(BaseModel):
    confirm: bool = False
    idempotency_key: Optional[str] = None


class FabricContractCreate(BaseModel):
    contract: dict
    task_id: Optional[int] = None
    agent_ref: str = ""
    project: str = ""


class FabricTransition(BaseModel):
    to_state: str
    by: str = "api"
    evidence: Optional[dict] = None


async def _fabric_call_async(ref: str, fn, *args, **kw):
    """Windows refs block waiting on a remote device; tmux/runtime refs do not.

    A blocking wait on the event loop would starve the device's own long-poll,
    so a `win:` ref is offloaded to a worker thread. Every other ref keeps the
    exact inline path it had before the Windows bridge existed — the tmux
    behaviour is deliberately untouched."""
    if (ref or "").startswith("win:"):
        return await asyncio.to_thread(_fabric_call, fn, *args, **kw)
    return _fabric_call(fn, *args, **kw)


def _fabric_call(fn, *args, **kw):
    from core.agent_fabric import FabricError
    from core.task_contract import ContractError
    try:
        return fn(*args, **kw)
    except (FabricError, ContractError) as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/fabric/agents")
async def fabric_agents(include_terminal: bool = False, _: bool = Depends(_auth)):
    from core import agent_fabric
    return agent_fabric.list_agents(include_terminal_jobs=include_terminal)


@router.get("/fabric/agents/{ref:path}/status")
async def fabric_status(ref: str, _: bool = Depends(_auth)):
    from core import agent_fabric
    return await _fabric_call_async(ref, agent_fabric.status, ref)


@router.post("/fabric/agents/{ref:path}/send")
async def fabric_send(ref: str, req: FabricSend, _: bool = Depends(_auth)):
    from core import agent_fabric
    return await _fabric_call_async(ref, agent_fabric.send, ref, req.text,
                                    idempotency_key=req.idempotency_key)


@router.post("/fabric/agents/{ref:path}/stop")
async def fabric_stop(ref: str, req: FabricStop, _: bool = Depends(_auth)):
    from core import agent_fabric
    return await _fabric_call_async(ref, agent_fabric.stop, ref, confirm=req.confirm,
                                    idempotency_key=req.idempotency_key)


@router.get("/fabric/agents/{ref:path}/result")
async def fabric_result(ref: str, _: bool = Depends(_auth)):
    from core import agent_fabric
    return await _fabric_call_async(ref, agent_fabric.result, ref)


@router.post("/fabric/start-or-resume")
async def fabric_start(req: FabricStart, _: bool = Depends(_auth)):
    from core import agent_fabric
    if req.ref:
        # Windows refs skip _validate_project_path deliberately: RUNTIME_ALLOWED_ROOTS
        # describes THIS server's filesystem and has nothing to say about a path
        # on the owner's PC. The device's own enrollment file is that gate.
        return await _fabric_call_async(req.ref, agent_fabric.start_or_resume_ref,
                                        req.ref, text=req.text)
    if not req.project_dir:
        raise HTTPException(status_code=422, detail="project_dir or ref is required")
    pp = _validate_project_path(req.project_dir)
    return _fabric_call(agent_fabric.start_or_resume, pp,
                        conversation_id=req.conversation_id)


@router.get("/fabric/contracts")
async def fabric_contracts(state: Optional[str] = None,
                           task_id: Optional[int] = None,
                           _: bool = Depends(_auth)):
    from core import task_contract
    return {"contracts": task_contract.list_contracts(state=state, task_id=task_id)}


@router.post("/fabric/contracts")
async def fabric_contract_create(req: FabricContractCreate, _: bool = Depends(_auth)):
    from core import task_contract
    return _fabric_call(task_contract.create, req.contract, task_id=req.task_id,
                        agent_ref=req.agent_ref, project=req.project, by="api")


@router.get("/fabric/contracts/{contract_id}")
async def fabric_contract_get(contract_id: str, _: bool = Depends(_auth)):
    from core import task_contract
    c = task_contract.get(contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="not found")
    return {**c, "history": task_contract.history(contract_id)}


@router.post("/fabric/contracts/{contract_id}/transition")
async def fabric_contract_transition(contract_id: str, req: FabricTransition,
                                     _: bool = Depends(_auth)):
    from core import task_contract
    return _fabric_call(task_contract.transition, contract_id, req.to_state,
                        by=req.by, evidence=req.evidence)


class AckReq(BaseModel):
    ids: list[int]


@router.post("/agents/commander/events/ack")
async def agents_commander_events_ack(req: AckReq, _: bool = Depends(_auth)):
    """Acknowledge delivered Commander events so they are not re-delivered."""
    from core import agent_control as _ac
    return {"acked": _ac.ack_commander_events(req.ids)}


class PhaseTextReq(BaseModel):
    session: str
    phase_id: str
    approved_task_text: str


@router.post("/agents/orchestrator/phase-text")
async def agents_set_phase_text(req: PhaseTextReq, _: bool = Depends(_auth)):
    """Record the owner's exact approved next-phase text (enables auto-advance for
    that phase). Rejects text authorising external publish/payment/email/credential."""
    try:
        return agent_orchestrator.set_phase_text(req.session, req.phase_id, req.approved_task_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/agents/orchestrator/phase-text")
async def agents_get_phase_text(session: str, phase_id: str, _: bool = Depends(_auth)):
    return {"session": session, "phase_id": phase_id,
            "approved_task_text": agent_orchestrator.get_phase_text(session, phase_id)}


class PhaseRollbackReq(BaseModel):
    session: str
    phase_id: str


@router.post("/agents/orchestrator/phase-rollback")
async def agents_phase_rollback(req: PhaseRollbackReq, _: bool = Depends(_auth)):
    """Roll back a dispatched phase advance (audited; text already sent cannot be
    un-sent, but the phase is no longer treated as dispatched)."""
    from core import agent_phase_advance
    return agent_phase_advance.rollback(req.session, req.phase_id)


# ── Venture Radar (task 193) + Business Analyzer (task 202) ─────────────────
# Canonical core surfaces; the seo-backend adapter renders/forwards only.
# Refusals are 409 + exact reason, like fabric/contracts.

def _venture_call(fn, *args, **kw):
    from core.business_analyzer import AnalyzerError
    from core.venture_radar import RadarError
    try:
        return fn(*args, **kw)
    except (RadarError, AnalyzerError) as e:
        raise HTTPException(status_code=409, detail=str(e))


class RadarCandidateCreate(BaseModel):
    mode: str
    title: str
    card: dict


class RadarCardUpdate(BaseModel):
    card: dict


class VentureTransition(BaseModel):
    to_state: str
    by: str
    note: str = ""


@router.get("/radar/candidates")
async def radar_candidates(state: Optional[str] = None, _: bool = Depends(_auth)):
    from core import venture_radar
    return {"candidates": venture_radar.ranked(state=state)}


@router.post("/radar/candidates")
async def radar_candidate_create(req: RadarCandidateCreate, _: bool = Depends(_auth)):
    from core import venture_radar
    return _venture_call(venture_radar.propose, req.mode, req.title, req.card)


@router.get("/radar/candidates/{candidate_id}")
async def radar_candidate_get(candidate_id: str, _: bool = Depends(_auth)):
    from core import venture_radar
    return _venture_call(venture_radar.get, candidate_id)


@router.post("/radar/candidates/{candidate_id}/card")
async def radar_candidate_update(candidate_id: str, req: RadarCardUpdate,
                                 _: bool = Depends(_auth)):
    from core import venture_radar
    return _venture_call(venture_radar.update_card, candidate_id, req.card)


@router.post("/radar/candidates/{candidate_id}/transition")
async def radar_candidate_transition(candidate_id: str, req: VentureTransition,
                                     _: bool = Depends(_auth)):
    from core import venture_radar
    return _venture_call(venture_radar.transition, candidate_id, req.to_state,
                         by=req.by, note=req.note)


@router.post("/radar/seed")
async def radar_seed(_: bool = Depends(_auth)):
    from core import venture_radar
    return venture_radar.seed_default()


class AnalyzerCardCreate(BaseModel):
    title: str
    card: dict


class AnalyzerRescore(BaseModel):
    scores: dict


class AnalyzerCombine(BaseModel):
    assets: list
    max_size: int = 3


@router.get("/analyzer/cards")
async def analyzer_cards(state: Optional[str] = None, _: bool = Depends(_auth)):
    from core import business_analyzer
    return {"cards": business_analyzer.ranked(state=state)}


@router.post("/analyzer/cards")
async def analyzer_card_create(req: AnalyzerCardCreate, _: bool = Depends(_auth)):
    from core import business_analyzer
    return _venture_call(business_analyzer.record, req.title, req.card)


@router.get("/analyzer/cards/{card_id}")
async def analyzer_card_get(card_id: str, _: bool = Depends(_auth)):
    from core import business_analyzer
    return _venture_call(business_analyzer.get, card_id)


@router.post("/analyzer/cards/{card_id}/rescore")
async def analyzer_card_rescore(card_id: str, req: AnalyzerRescore,
                                _: bool = Depends(_auth)):
    from core import business_analyzer
    return _venture_call(business_analyzer.rescore, card_id, req.scores)


@router.post("/analyzer/cards/{card_id}/transition")
async def analyzer_card_transition(card_id: str, req: VentureTransition,
                                   _: bool = Depends(_auth)):
    from core import business_analyzer
    return _venture_call(business_analyzer.transition, card_id, req.to_state,
                         by=req.by, note=req.note)


@router.post("/analyzer/combine")
async def analyzer_combine(req: AnalyzerCombine, _: bool = Depends(_auth)):
    from core import business_analyzer
    return {"combinations": _venture_call(business_analyzer.combine, req.assets,
                                          max_size=req.max_size)}


# ── Windows Agent Bridge (task 220) ─────────────────────────────────────────
# Two audiences, two auth schemes, on purpose:
#
#   * DEVICE routes (/windows/enroll|poll|result|rotate) are called by the
#     owner's Windows machine. They do NOT use RUNTIME_TOKEN: a laptop is a
#     weaker place to keep a secret than this server, so it gets its own
#     per-device, rotatable, revocable identity and signs each request over
#     (device, timestamp, nonce, path, body-hash) — see core.windows_bridge.
#   * OWNER routes (everything else below) are ordinary Owner OS calls behind
#     the existing bearer/HMAC `_auth`.
#
# The device never listens on a port: it long-polls /windows/poll and posts
# results back, so the Windows machine opens every connection outbound.
from core import windows_bridge  # noqa: E402


def _win_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except windows_bridge.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except windows_bridge.WindowsBridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _win_device(request: Request) -> tuple[dict, dict]:
    """Authenticate a device request and return (device_row, parsed_body).

    The body is read RAW and hashed before it is parsed, because the signature
    covers the exact bytes on the wire — re-serializing a parsed body would
    verify a document the client never sent."""
    raw = await request.body()
    device_id = request.headers.get("x-oos-device") or ""
    device = _win_call(windows_bridge.verify_request,
                       device_id,
                       request.headers.get("x-oos-timestamp"),
                       request.headers.get("x-oos-nonce") or "",
                       request.url.path,
                       raw,
                       request.headers.get("x-oos-signature") or "")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return device, body


class WinEnrollReq(BaseModel):
    code: str
    device_name: str = ""
    os_version: str = ""
    agent_version: str = ""


@router.post("/windows/enroll")
async def windows_enroll(req: WinEnrollReq, request: Request):
    """Exchange a single-use enrollment code for a device identity + secret.
    Deliberately unauthenticated by RUNTIME_TOKEN: the whole point is that the
    Windows machine never holds the server's own token. The code is the
    credential, it is single-use, it expires, and it is stored only as a hash."""
    client = getattr(request.client, "host", "") or ""
    return _win_call(windows_bridge.enroll, req.code, device_name=req.device_name,
                     os_version=req.os_version, agent_version=req.agent_version,
                     enrolled_from=client)


@router.post("/windows/poll")
async def windows_poll(request: Request):
    """Long-poll: heartbeat + workspace inventory in, leased commands out.

    Returning an empty list after `wait` seconds is a normal, healthy answer —
    the device reconnects immediately, which is also how the server learns it
    is still alive."""
    import asyncio

    device, body = await _win_device(request)
    device_id = device["device_id"]
    windows_bridge.touch_device(device_id,
                               agent_version=str(body.get("agent_version") or "")[:40],
                               os_version=str(body.get("os_version") or "")[:120])
    if "workspaces" in body:
        _win_call(windows_bridge.report_workspaces, device_id, body.get("workspaces"))
    try:
        wait = float(body.get("wait") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    wait = max(0.0, min(wait, windows_bridge.MAX_POLL_WAIT_SECS))
    deadline = time.monotonic() + wait
    while True:
        commands = _win_call(windows_bridge.lease, device_id)
        if commands or time.monotonic() >= deadline:
            return {"commands": commands, "server_time": time.time(),
                    "poll_after_secs": 1 if commands else 0}
        await asyncio.sleep(0.5)


@router.post("/windows/result")
async def windows_result(request: Request):
    device, body = await _win_device(request)
    return _win_call(windows_bridge.complete, device["device_id"],
                     str(body.get("command_id") or ""), ok=bool(body.get("ok")),
                     result=body.get("result"), error=str(body.get("error") or ""))


@router.post("/windows/rotate")
async def windows_rotate(request: Request):
    """Device-initiated secret rotation over an already-authenticated request."""
    device, _body = await _win_device(request)
    return _win_call(windows_bridge.rotate_secret, device["device_id"])


class WinEnrollCodeReq(BaseModel):
    label: str = ""
    ttl_secs: int = windows_bridge.ENROLL_CODE_TTL_SECS


@router.post("/windows/enroll-code")
async def windows_enroll_code(req: WinEnrollCodeReq, _: bool = Depends(_auth)):
    """Owner-only: mint the one-time code that is typed on the Windows machine.
    The plaintext is returned exactly once and never stored."""
    return _win_call(windows_bridge.create_enrollment_code, req.label, req.ttl_secs)


@router.get("/windows/devices")
async def windows_devices(_: bool = Depends(_auth)):
    return windows_bridge.list_devices()


@router.post("/windows/devices/{device_id}/revoke")
async def windows_revoke(device_id: str, reason: str = "", _: bool = Depends(_auth)):
    return _win_call(windows_bridge.revoke_device, device_id, reason=reason)


@router.get("/windows/workspaces")
async def windows_workspaces(device_id: str = "", _: bool = Depends(_auth)):
    return windows_bridge.list_workspaces(device_id)


class WinWorkspaceToggle(BaseModel):
    device_id: str
    workspace_id: str
    enabled: bool


@router.post("/windows/workspaces/enabled")
async def windows_workspace_enabled(req: WinWorkspaceToggle, _: bool = Depends(_auth)):
    """The server-side off switch. Enrollment happens on the Windows machine;
    this can only withdraw reachability, never grant it."""
    return _win_call(windows_bridge.set_workspace_enabled, req.device_id,
                     req.workspace_id, req.enabled)


class WinCommandReq(BaseModel):
    device_id: str
    action: str
    workspace_id: str = ""
    params: Optional[dict] = None
    command_id: str = ""
    wait_secs: float = 30.0


@router.post("/windows/command")
async def windows_command(req: WinCommandReq, request: Request,
                          x_runtime_actor: Optional[str] = Header(None),
                          _: bool = Depends(_auth)):
    """Enqueue one allowlisted command and (by default) wait for its result.
    A timeout is reported as timed_out=true with the command still queued —
    never as a failure the device did not report."""
    actor, _source = caller_identity(request, x_runtime_actor)
    # to_thread, NOT inline: dispatch() waits for the device by polling the
    # queue, and doing that on the event loop would freeze the very
    # /windows/poll request the device is holding open — the command would then
    # ALWAYS time out, because the only process that could answer it is blocked
    # behind this handler. Caught by tools/windows_bridge_sim.py step 6.
    return await asyncio.to_thread(
        _win_call, windows_bridge.dispatch, req.device_id, req.action,
        workspace_id=req.workspace_id, params=req.params,
        command_id=req.command_id, created_by=actor or "owner",
        wait_secs=max(0.0, min(float(req.wait_secs or 0), 120.0)))


@router.get("/windows/command/{command_id}")
async def windows_command_get(command_id: str, _: bool = Depends(_auth)):
    cmd = windows_bridge.get_command(command_id)
    if not cmd:
        raise HTTPException(status_code=404, detail="not found")
    return cmd


@router.get("/windows/policy")
async def windows_policy(_: bool = Depends(_auth)):
    """The entire remote surface, in one auditable dump."""
    return windows_bridge.policy()


# ── Model Router (task 209) ──────────────────────────────────────────────────
# Standing cost-aware routing policy: decides and records, never dispatches.
# The adapter renders effectiveness and forwards outcomes only.

def _router_call(fn, *args, **kw):
    from core.model_router import RouterError
    try:
        return fn(*args, **kw)
    except RouterError as e:
        raise HTTPException(status_code=409, detail=str(e))


class RouterRoute(BaseModel):
    task_class: str
    risk: str = "normal"
    prior_attempts: list = []
    context_pack: str = ""
    task_ref: str = ""
    strict: bool = False
    escalation_reason: Optional[dict] = None
    explicit_model: Optional[str] = None


class RouterOutcome(BaseModel):
    decision_id: int
    outcome: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    retries: int = 0
    note: str = ""


@router.post("/router/route")
async def router_route(req: RouterRoute, _: bool = Depends(_auth)):
    from core import model_router
    return _router_call(model_router.route, req.task_class, risk=req.risk,
                        prior_attempts=req.prior_attempts, context_pack=req.context_pack,
                        task_ref=req.task_ref, strict=req.strict,
                        escalation_reason=req.escalation_reason,
                        explicit_model=req.explicit_model)


@router.post("/router/outcome")
async def router_outcome(req: RouterOutcome, _: bool = Depends(_auth)):
    from core import model_router
    return _router_call(model_router.record_outcome, req.decision_id,
                        outcome=req.outcome, tokens_in=req.tokens_in,
                        tokens_out=req.tokens_out, usd=req.usd, retries=req.retries,
                        note=req.note)


@router.get("/router/effectiveness")
async def router_effectiveness(days: int = 30, _: bool = Depends(_auth)):
    from core import model_router
    return model_router.effectiveness(days=days)


@router.get("/router/policy")
async def router_policy(_: bool = Depends(_auth)):
    from core import model_router
    return model_router.policy()
