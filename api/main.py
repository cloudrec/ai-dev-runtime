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
"""
import os
import sys
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# корень проекта в sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.engine import RuntimeEngine
from core.task_queue import TaskQueue

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
    version="0.5",
)

engine = RuntimeEngine()


# worker-функция для очереди — запускает движок
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
