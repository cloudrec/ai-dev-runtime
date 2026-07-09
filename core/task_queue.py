"""
Task Queue — асинхронная очередь задач AI Runtime.

Каждая задача:
  - UUID
  - время создания / старта / завершения
  - статус: pending | running | done | failed
  - лог выполнения (строка за строкой)
  - результат
  - ошибки

Выполнение идёт в фоновом потоке, API не блокируется.
"""
import threading
import time
import uuid
import traceback
from datetime import datetime


# Возможные статусы задачи
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class Task:
    """Одна задача в очереди."""

    def __init__(self, command: str, project_path: str):
        self.id = str(uuid.uuid4())
        self.command = command
        self.project_path = project_path
        self.status = STATUS_PENDING
        self.created_at = datetime.now().isoformat()
        self.started_at = None
        self.finished_at = None
        self.log = []      # список строк
        self.result = None
        self.error = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "project_path": self.project_path,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": self.log,
            "result": self.result,
            "error": self.error,
        }

    def add_log(self, message: str):
        self.log.append(f"{datetime.now().isoformat()} | {message}")


class TaskQueue:
    """
    Потокобезопасная очередь задач.
    Хранит задачи в памяти, выполняет их в фоновом worker-потоке.
    """

    def __init__(self, worker_fn=None):
        self._tasks = {}                 # id -> Task
        self._lock = threading.Lock()
        self._pending = []               # FIFO очередь id
        self._worker_fn = worker_fn      # функция(command, project_path) -> result
        self._stop = False
        self._cond = threading.Condition(self._lock)
        # запускаем worker
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_worker(self, worker_fn):
        """Устанавливает функцию, исполняющую задачу."""
        self._worker_fn = worker_fn

    def submit(self, command: str, project_path: str) -> str:
        """Создаёт новую задачу и ставит в очередь. Возвращает task_id."""
        task = Task(command, project_path)
        with self._lock:
            self._tasks[task.id] = task
            self._pending.append(task.id)
            task.add_log("задача создана и поставлена в очередь")
            self._cond.notify()
        return task.id

    def get(self, task_id: str):
        """Возвращает задачу по id (или None)."""
        with self._lock:
            return self._tasks.get(task_id)

    def status(self, task_id: str) -> str:
        task = self.get(task_id)
        return task.status if task else "unknown"

    def list(self) -> list:
        """Список всех задач."""
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def stop(self):
        self._stop = True
        with self._lock:
            self._cond.notify()

    # ---- внутренний worker-поток ----

    def _run(self):
        while not self._stop:
            with self._lock:
                while not self._pending and not self._stop:
                    self._cond.wait()
                if self._stop:
                    break
                task_id = self._pending.pop(0)
                task = self._tasks[task_id]

            self._execute(task)

    def _execute(self, task: Task):
        task.status = STATUS_RUNNING
        task.started_at = datetime.now().isoformat()
        task.add_log("задача запущена")

        if self._worker_fn is None:
            task.error = "worker function not set"
            task.status = STATUS_FAILED
            task.finished_at = datetime.now().isoformat()
            task.add_log("ошибка: worker не настроен")
            return

        try:
            result = self._worker_fn(task.command, task.project_path)
            task.result = result
            task.status = STATUS_DONE
            task.add_log("задача успешно завершена")
        except Exception as e:
            task.error = f"{type(e).__name__}: {e}"
            task.status = STATUS_FAILED
            task.add_log(f"ошибка: {task.error}")
            task.add_log(traceback.format_exc())
        finally:
            task.finished_at = datetime.now().isoformat()
