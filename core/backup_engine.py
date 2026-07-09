"""
Backup Engine — резервное копирование перед каждым изменением.

Возможности:
  - snapshot() — создать бэкап (tar.gz) с временной меткой
  - история изменений
  - rollback() — восстановить состояние из бэкапа
  - автоматическая очистка старых бэкапов по лимиту

Бэкапы хранятся в <project_path>/.ai-runtime-backups/
"""
import os
import tarfile
import json
import shutil
from datetime import datetime


# Что не включаем в бэкап
EXCLUDE_DIRS = {".git", "venv", ".venv", "env", "__pycache__",
                ".ai-runtime-backups", "node_modules", ".mypy_cache",
                ".pytest_cache", ".ruff_cache"}


class BackupEngine:
    def __init__(self, project_path: str, max_backups: int = 20):
        self.project_path = os.path.realpath(project_path)
        self.backup_dir = os.path.join(self.project_path, ".ai-runtime-backups")
        self.history_file = os.path.join(self.backup_dir, "history.json")
        self.max_backups = max_backups
        os.makedirs(self.backup_dir, exist_ok=True)

    # ---- создание бэкапа ----

    def snapshot(self, reason: str = "manual") -> dict:
        """
        Создаёт tar.gz-бэкап проекта (без .git, venv, pycache).
        Возвращает метаданные бэкапа.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{ts}"
        archive_path = os.path.join(self.backup_dir, f"{name}.tar.gz")

        def _filter(tarinfo):
            parts = tarinfo.name.split("/")
            if any(part in EXCLUDE_DIRS for part in parts):
                return None
            return tarinfo

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(self.project_path, arcname=".", filter=_filter)

        meta = {
            "id": name,
            "timestamp": ts,
            "reason": reason,
            "archive": f"{name}.tar.gz",
            "size": os.path.getsize(archive_path),
        }
        self._append_history(meta)
        self._enforce_limit()
        return meta

    # ---- rollback ----

    def rollback(self, backup_id: str) -> dict:
        """
        Восстанавливает файлы проекта из бэкапа.
        ВНИМАНИЕ: перезаписывает текущие файлы.
        Перед rollback автоматически создаётся ещё один snapshot.
        """
        archive = os.path.join(self.backup_dir, f"{backup_id}.tar.gz")
        if not os.path.exists(archive):
            return {"ok": False, "error": f"backup {backup_id} not found"}

        # страховочный snapshot перед откатом
        self.snapshot(reason=f"pre-rollback of {backup_id}")

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=self.project_path)

        return {
            "ok": True,
            "restored_from": backup_id,
            "project_path": self.project_path,
        }

    # ---- история ----

    def list_backups(self) -> list:
        """Список всех бэкапов (новые сверху)."""
        history = self._read_history()
        return list(reversed(history))

    def get_latest(self) -> dict:
        history = self._read_history()
        return history[-1] if history else None

    # ---- внутренние ----

    def _read_history(self) -> list:
        if not os.path.exists(self.history_file):
            return []
        with open(self.history_file, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []

    def _append_history(self, meta: dict):
        history = self._read_history()
        history.append(meta)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def _enforce_limit(self):
        """Удаляет самые старые бэкапы сверх лимита."""
        history = self._read_history()
        if len(history) <= self.max_backups:
            return
        excess = len(history) - self.max_backups
        to_remove = history[:excess]
        for meta in to_remove:
            archive = os.path.join(self.backup_dir, meta["archive"])
            if os.path.exists(archive):
                os.remove(archive)
        # обновляем историю
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(history[excess:], f, indent=2, ensure_ascii=False)
