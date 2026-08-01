"""PHASE 13 — stable versioned Runtime API (/api/v1). Owner OS connects here."""
from __future__ import annotations

import hashlib
import hmac
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
        return True
    # Bearer path
    if authorization and authorization.startswith("Bearer "):
        if hmac.compare_digest(authorization[7:].strip(), _TOKEN):
            return True
    raise HTTPException(status_code=401, detail="unauthorized")


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


@router.post("/jobs")
async def create_job(req: JobCreate, _: bool = Depends(_auth)):
    pp = _validate_project_path(req.project_path)
    goal = req.goal or req.title or "runtime task"
    instructions = req.instructions or req.instruction or goal
    autonomy = req.autonomy_level or req.autonomy or "prepare"
    if autonomy not in job_executor.AUTONOMY_ORDER:
        raise HTTPException(status_code=422, detail=f"autonomy must be one of {job_executor.AUTONOMY_ORDER}")
    dangerous = bool(_DANGEROUS_RE.search(f"{goal} {instructions}"))
    # approval required unless execute_full+ and not dangerous
    auto_ok = job_executor._idx(autonomy) >= job_executor._idx("execute_full") and not dangerous
    approval_required = req.approval_required if req.approval_required is not None else (not auto_ok)
    job = job_store.create_job(
        project_id=req.project_id, project_path=pp, task_id=req.task_id, goal=goal, instructions=instructions,
        autonomy_level=autonomy, allowed_paths=req.allowed_paths, forbidden_paths=req.forbidden_paths,
        base_branch=req.base_branch, auto_commit=req.auto_commit, auto_push=req.auto_push,
        approval_required=approval_required, dangerous=int(dangerous),
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
async def agents_send(req: AgentSendReq, _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_send, req.target, req.text, req.idempotency_key)


@router.post("/agents/answer")
async def agents_answer(req: AgentSendReq, _: bool = Depends(_auth)):
    return _agent_call(agent_control.agent_answer, req.target, req.text, req.idempotency_key)


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
