"""PHASE 13 — stable versioned Runtime API (/api/v1). Owner OS connects here."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Optional

import asyncio

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


class SmokeReq(BaseModel):
    model: Optional[str] = None
    timeout_seconds: Optional[float] = None


@router.post("/smoke")
async def smoke(req: SmokeReq, _: bool = Depends(_auth)):
    """PHASE 45 — read-only provider smoke test: one minimal, non-agentic
    round-trip to the configured provider/model. No project_path, no file or
    DB writes, no job created, no repository touched. Owner OS's
    runtime_client.provider_smoke() calls this before ever dispatching a
    coding job (retry_runtime_job). Blocking work runs in a thread so it
    can't stall the event loop for the full hard timeout."""
    return await asyncio.to_thread(ai_planner.smoke, req.model, req.timeout_seconds)


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
    base_branch: Optional[str] = None
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
