"""
REST API для AI Runtime Agent.

Эндпоинты:
  POST /run               — синхронный запуск команды (legacy)
  POST /task              — создать асинхронную задачу
  GET  /status/{task_id}  — статус задачи
  GET  /tasks             — список всех задач
  GET  /health            — здоровье сервиса
  GET  /logs              — логи
  GET  /logs/{task_id}    — логи конкретной задачи
  POST /shutdown          — остановка воркера

Security:
  - API Key через заголовок X-API-Key
  - HMAC через X-Signature + X-Timestamp
  - настраивается в configs/.env
"""
import os
import sys
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

# корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.engine import RuntimeEngine
from core.task_queue import TaskQueue
from core.security import check_api_key, check_hmac, is_public_path

# ---- структурированные логи (требование 12) ----
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("ai-runtime")

app = FastAPI(
    title="AI Runtime Agent",
    description="Сервис для управления проектами через команды",
    version="0.6",
)


# ---- Security middleware ----
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # публичные пути + versioned /api/v1 (own auth) пропускаем
        if is_public_path(path) or path.startswith("/api/v1"):
            return await call_next(request)

        # читаем тело (для HMAC нужно)
        body = await request.body()

        # проверка API Key
        api_key = request.headers.get("x-api-key", "")
        if not check_api_key(api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
            )

        # проверка HMAC (если настроена)
        signature = request.headers.get("x-signature", "")
        timestamp = request.headers.get("x-timestamp", "")
        if signature or timestamp:
            if not check_hmac(timestamp, signature, body):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "invalid HMAC signature"},
                )

        # восстанавливаем тело для последующих обработчиков
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = receive

        return await call_next(request)


app.add_middleware(AuthMiddleware)

# ---- PHASE 13: stable versioned API + persistent jobs ----
from api.v1 import router as v1_router  # noqa: E402
from core import job_store  # noqa: E402
app.include_router(v1_router)


@app.on_event("startup")
def _phase13_startup():
    job_store.init_db()
    n = job_store.recover_interrupted()
    if n:
        logger.info(f"recovered {n} interrupted job(s) -> waiting_approval")


@app.on_event("startup")
async def _start_agent_supervisor():
    # Always-on supervisor: auto-resolves provably-safe permission prompts for
    # owner-approved sessions. Independent of any MCP/ChatGPT client. Gated by
    # AGENT_SUPERVISOR_ENABLED + AGENT_AUTORESOLVE_SESSIONS (deny-by-default).
    import asyncio
    try:
        from core.agent_supervisor import run_loop
        asyncio.create_task(run_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"agent supervisor not started: {e}")


@app.on_event("startup")
async def _start_agent_orchestrator():
    # Autonomous Agent Orchestrator: supervises EXISTING agents only, auto-continues
    # provably-safe prompts for `auto` sessions, holds Safe Guard/Polyinput, and
    # records persistent per-agent state. Gated by AGENT_ORCHESTRATOR_ENABLED.
    import asyncio
    try:
        from core.agent_orchestrator import run_loop as _orch_loop
        asyncio.create_task(_orch_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"agent orchestrator not started: {e}")


@app.on_event("startup")
async def _start_control_plane():
    # Control Plane V2 engine (P1 SHADOW, observe-only): continuous zero-config agent
    # discovery + classification + durable source-of-truth + CTO inbox + fail-closed
    # delivery health. Issues NO pane commands (actuation is a later, owner-gated phase).
    # Gated by CONTROL_PLANE_ENABLED.
    import asyncio
    try:
        from core.control_plane.engine import run_loop as _cp_loop
        asyncio.create_task(_cp_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"control plane not started: {e}")


@app.on_event("startup")
async def _start_continuation_watchdog():
    # Server-side direct-agent continuation watchdog: submits a documented SAFE next
    # step for an approved idle agent and PROVES the submission (submitted + pane
    # changed + prompt consumed + conversation modified + state transitioned),
    # retrying Enter once and raising a blocker if it will not submit. Independent of
    # any external/hourly automation. Gated by CONTINUATION_WATCHDOG_ENABLED.
    import asyncio
    try:
        from core.agent_continuation_watchdog import run_loop as _cw_loop
        asyncio.create_task(_cw_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"continuation watchdog not started: {e}")


@app.on_event("startup")
async def _start_wake_pipeline_watch():
    # Says out loud when the wake pipeline stops moving: a wake decided and never
    # delivered, or a companion process that died (its claim silence is the only
    # signal - the last successful delivery keeps looking recent). Log-only by
    # design: it emits no event and actuates nothing, because the wake path
    # feeding itself is a failure this system has already had.
    import asyncio
    try:
        from core.wake_bridge import pipeline_watch_loop
        asyncio.create_task(pipeline_watch_loop(
            log=lambda level, msg: getattr(logger, level, logger.info)(msg)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"wake pipeline watch not started: {e}")


@app.on_event("startup")
async def _start_commander_autopilot():
    # Commander autopilot: per-minute evaluation of critical projects (state + unfinished
    # tasks + background subagents + last proven progress) and auto-delivery of the exact
    # SAFE next step to an idle/waiting agent with unfinished pre-approved work. DORMANT by
    # default (COMMANDER_AUTOPILOT_ENABLED off = owner gate); even enabled, actuation stays
    # confined to CONTROL_PLANE_CANARY_AGENTS. No scope expansion without an owner decision.
    import asyncio
    try:
        from core.commander_autopilot import run_loop as _ap_loop
        asyncio.create_task(_ap_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"commander autopilot not started: {e}")


@app.on_event("startup")
async def _start_project_supervisor():
    # Server-resident project supervisor: validates an agent's handoff against
    # git, picks the next ALREADY-RECORDED roadmap block, and re-prompts the same
    # agent — so ordinary block-to-block continuation does not wait on a ChatGPT
    # wake, which cannot be invoked event-driven from here anyway. Wake stays the
    # checkpoint channel (gate / failure / milestone).
    # DORMANT unless PROJECT_SUPERVISOR_PROJECTS names a project.
    import asyncio
    try:
        from core.project_supervisor import run_loop as _ps_loop
        asyncio.create_task(_ps_loop(
            log=lambda level, msg: getattr(logger, level, logger.info)(msg)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"project supervisor not started: {e}")


@app.on_event("startup")
async def _start_context_budget():
    # Context budget / checkpoint / rotation: tracks conversation size + phase for every
    # registered critical agent (read-only, durable), writes an ATOMIC verified checkpoint
    # at a safe boundary over the soft threshold, and rotates (clear + resume from the
    # checkpoint) over the hard threshold — actuation confined to CANARY_AGENTS via the
    # lease-gated Actuator; a non-canary agent over budget raises an owner-gated event.
    import asyncio
    try:
        from core.context_budget import run_loop as _cb_loop
        asyncio.create_task(_cb_loop())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"context budget not started: {e}")


# ---- движок и очередь ----
engine = RuntimeEngine()


def _worker(command: str, project_path: str):
    logger.info("executing task: %s in %s", command, project_path)
    return engine.run(command, project_path)


queue = TaskQueue(worker_fn=_worker)
START_TIME = datetime.now()


# ---- модели ----
class TaskRequest(BaseModel):
    command: str
    project_path: str


# ---- эндпоинты ----

@app.post("/run")
def run(command: str, project_path: str):
    """Синхронный запуск (legacy, блокирующий)."""
    logger.info("sync run: %s", command)
    return engine.run(command, project_path)


@app.post("/task")
def create_task(req: TaskRequest):
    """Создать асинхронную задачу. Возвращает task_id."""
    task_id = queue.submit(req.command, req.project_path)
    logger.info("task submitted: %s", task_id)
    return {"task_id": task_id, "status": "pending"}


@app.get("/status/{task_id}")
def task_status(task_id: str):
    """Статус задачи."""
    task = queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task.to_dict()


@app.get("/tasks")
def list_tasks():
    """Список всех задач."""
    return queue.list()


@app.get("/health")
def health():
    """Здоровье сервиса."""
    return {
        "status": "ok",
        "started_at": START_TIME.isoformat(),
        "uptime_seconds": (datetime.now() - START_TIME).total_seconds(),
        "tasks_total": len(queue.list()),
    }


@app.get("/logs")
def logs():
    """Логи всех задач."""
    result = []
    for t in queue.list():
        result.append({"task_id": t["id"], "command": t["command"], "log": t["log"]})
    return result


@app.get("/logs/{task_id}")
def task_logs(task_id: str):
    """Логи конкретной задачи."""
    task = queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": task_id, "log": task.log}


@app.post("/shutdown")
def shutdown():
    """Остановка воркера очереди."""
    queue.stop()
    logger.warning("shutdown requested")
    return {"status": "shutting down worker"}
