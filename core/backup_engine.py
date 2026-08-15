"""
Backup Engine — резервное копирование перед каждым изменением.

Hardened backup stage (issue: a Runtime job stuck forever in `backing_up`):
  - snapshot() runs the tar on a worker thread with a BOUNDED timeout and
    emits measurable progress via progress_cb (files + bytes written);
  - writes to an atomic <name>.tar.gz.tmp then os.replace() to the final name,
    so a partial/corrupt archive is never published as "latest";
  - detects a dead / hung / orphaned backup worker (cannot join after abort)
    and fails with a precise terminal error instead of hanging;
  - cleans incomplete *.tar.gz.tmp on entry and on failure, and NEVER deletes
    an existing valid backup on failure (last good backup is preserved);
  - never recurses into backup directories (.ai-runtime-backups) or venv/.git;
  - light=True takes the smallest safe snapshot (git-tracked source files only,
    size-bounded) for read-only inventory/report tasks instead of a full
    migration-grade archive;
  - rollback()/history unchanged in behaviour.

Бэкапы хранятся в <project_path>/.ai-runtime-backups/
"""
import os
import subprocess
import tarfile
import json
import threading
import time
from datetime import datetime


# Что не включаем в бэкап
EXCLUDE_DIRS = {".git", "venv", ".venv", "env", "__pycache__",
                ".ai-runtime-backups", "node_modules", ".mypy_cache",
                ".pytest_cache", ".ruff_cache"}

# env-tunable bounds
_DEFAULT_TIMEOUT = int(os.getenv("RUNTIME_BACKUP_TIMEOUT", "120"))
_PROGRESS_INTERVAL = float(os.getenv("RUNTIME_BACKUP_PROGRESS_SECS", "2"))
_ABORT_GRACE = float(os.getenv("RUNTIME_BACKUP_ABORT_GRACE_SECS", "10"))
_MAX_LIGHT_FILE_BYTES = int(os.getenv("RUNTIME_BACKUP_LIGHT_MAX_FILE_BYTES", str(5 * 1024 * 1024)))


class BackupError(RuntimeError):
    """Raised for a precise, terminal backup failure (timeout, orphaned worker,
    tar error) — callers turn this into a failed job with this message, never an
    indefinite `backing_up`."""


class BackupEngine:
    def __init__(self, project_path: str, max_backups: int = 20):
        self.project_path = os.path.realpath(project_path)
        self.backup_dir = os.path.join(self.project_path, ".ai-runtime-backups")
        self.history_file = os.path.join(self.backup_dir, "history.json")
        self.max_backups = max_backups
        os.makedirs(self.backup_dir, exist_ok=True)

    # ---- создание бэкапа ----

    def snapshot(self, reason: str = "manual", *, timeout: float | None = None,
                 progress_cb=None, light: bool = False) -> dict:
        """Создаёт атомарный tar.gz-бэкап проекта. Bounded by `timeout` seconds;
        calls progress_cb({files, bytes, elapsed}) roughly every
        RUNTIME_BACKUP_PROGRESS_SECS. Raises BackupError on timeout / orphaned
        worker / tar error (never hangs). Returns metadata on success."""
        timeout = float(timeout if timeout is not None else _DEFAULT_TIMEOUT)
        self._cleanup_stale_temp()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = f"backup_{ts}"
        final_path = os.path.join(self.backup_dir, f"{name}.tar.gz")
        tmp_path = final_path + ".tmp"

        members = list(self._members(light))
        abort = threading.Event()
        stats = {"files": 0, "done": False, "error": None}

        def _work():
            try:
                with tarfile.open(tmp_path, "w:gz") as tar:
                    for abspath, arcname in members:
                        if abort.is_set():
                            raise BackupError("aborted")
                        try:
                            ti = tar.gettarinfo(abspath, arcname=arcname)
                        except (FileNotFoundError, OSError):
                            continue  # file vanished mid-backup — skip, keep going
                        if ti is None:
                            continue
                        if ti.isreg():
                            with open(abspath, "rb") as fh:
                                tar.addfile(ti, fh)
                        else:
                            tar.addfile(ti)
                        stats["files"] += 1
                stats["done"] = True
            except BaseException as e:  # noqa: BLE001 — record ANY failure for the caller
                stats["error"] = e

        worker = threading.Thread(target=_work, name=f"backup-{name}", daemon=True)
        start = time.monotonic()
        worker.start()
        deadline = start + timeout

        # monitor loop: bounded, emits progress, aborts on deadline
        while worker.is_alive():
            worker.join(timeout=_PROGRESS_INTERVAL)
            if progress_cb:
                try:
                    progress_cb({"files": stats["files"],
                                 "bytes": (os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0),
                                 "elapsed": round(time.monotonic() - start, 1)})
                except Exception:  # noqa: BLE001 — progress must never break the backup
                    pass
            if worker.is_alive() and time.monotonic() > deadline:
                abort.set()
                worker.join(timeout=_ABORT_GRACE)
                break

        if worker.is_alive():
            # Aborted but the worker did not stop within the grace window: a hung
            # / orphaned backup child. Do NOT hang the pipeline on it.
            self._safe_remove(tmp_path)
            raise BackupError(
                f"backup worker did not stop within {int(timeout)}s + grace after abort — "
                f"orphaned/hung; incomplete archive discarded")

        if stats["error"] is not None or not stats["done"]:
            self._safe_remove(tmp_path)
            if isinstance(stats["error"], BaseException):
                raise BackupError(f"backup failed after {stats['files']} files: {stats['error']}")
            raise BackupError(f"backup did not complete (timed out after {int(timeout)}s)")

        # atomic publish: only a fully-written archive ever becomes "latest"
        os.replace(tmp_path, final_path)
        meta = {
            "id": name, "timestamp": ts, "reason": reason,
            "archive": f"{name}.tar.gz", "size": os.path.getsize(final_path),
            "files": stats["files"], "light": bool(light),
            "elapsed": round(time.monotonic() - start, 2),
        }
        self._append_history(meta)
        self._enforce_limit()
        return meta

    # ---- выбор файлов ----

    def _members(self, light: bool):
        """Yield (abspath, arcname) pairs. Never descends into EXCLUDE_DIRS or the
        backup directory itself (no recursive backup-of-backups)."""
        if light:
            yield from self._light_members()
            return
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            rroot = os.path.realpath(root)
            if rroot == self.backup_dir or rroot.startswith(self.backup_dir + os.sep):
                dirs[:] = []
                continue
            for fn in files:
                ap = os.path.join(root, fn)
                rel = os.path.relpath(ap, self.project_path)
                yield ap, "./" + rel

    def _light_members(self):
        """Smallest safe snapshot: git-tracked source files only, size-bounded.
        Falls back to a size-bounded walk if the project is not a git repo."""
        try:
            out = subprocess.run(["git", "-C", self.project_path, "ls-files"],
                                 capture_output=True, text=True, timeout=30, shell=False)
            if out.returncode == 0 and out.stdout.strip():
                for rel in out.stdout.splitlines():
                    rel = rel.strip()
                    if not rel:
                        continue
                    ap = os.path.join(self.project_path, rel)
                    try:
                        if os.path.isfile(ap) and os.path.getsize(ap) <= _MAX_LIGHT_FILE_BYTES:
                            yield ap, "./" + rel
                    except OSError:
                        continue
                return
        except (OSError, subprocess.SubprocessError):
            pass
        # fallback: bounded walk (skip large files so it stays "light")
        for ap, arc in self._members(light=False):
            try:
                if os.path.getsize(ap) <= _MAX_LIGHT_FILE_BYTES:
                    yield ap, arc
            except OSError:
                continue

    # ---- rollback ----

    def rollback(self, backup_id: str) -> dict:
        """Восстанавливает файлы проекта из бэкапа. Перед rollback автоматически
        создаётся ещё один snapshot."""
        archive = os.path.join(self.backup_dir, f"{backup_id}.tar.gz")
        if not os.path.exists(archive):
            return {"ok": False, "error": f"backup {backup_id} not found"}

        # страховочный snapshot перед откатом
        self.snapshot(reason=f"pre-rollback of {backup_id}")

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(path=self.project_path)

        return {"ok": True, "restored_from": backup_id, "project_path": self.project_path}

    # ---- история ----

    def list_backups(self) -> list:
        """Список всех бэкапов (новые сверху)."""
        history = self._read_history()
        return list(reversed(history))

    def get_latest(self) -> dict:
        history = self._read_history()
        return history[-1] if history else None

    # ---- внутренние ----

    # A tmp file younger than this is treated as another live snapshot in
    # flight, never as stale debris. Two runtime jobs on the same project
    # snapshot concurrently (2026-08-15: jobs dd6ee850/e4a1b151 started ~1s
    # apart; the second's entry-cleanup deleted the first's in-progress tmp and
    # its os.replace() died with ENOENT). Comfortably above _BACKUP_TIMEOUT.
    _STALE_TMP_SECS = 900

    def _cleanup_stale_temp(self) -> None:
        """Remove leftover *.tar.gz.tmp from a previously crashed/aborted backup.
        Only ever touches temp files old enough that no live snapshot can still
        be writing them — existing valid backups are preserved."""
        try:
            cutoff = time.time() - self._STALE_TMP_SECS
            for fn in os.listdir(self.backup_dir):
                if fn.startswith("backup_") and fn.endswith(".tar.gz.tmp"):
                    p = os.path.join(self.backup_dir, fn)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            self._safe_remove(p)
                    except OSError:
                        pass
        except OSError:
            pass

    @staticmethod
    def _safe_remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

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
        """Удаляет самые старые бэкапы сверх лимита (валидный последний бэкап
        всегда сохраняется — удаляются только самые старые)."""
        history = self._read_history()
        if len(history) <= self.max_backups:
            return
        excess = len(history) - self.max_backups
        to_remove = history[:excess]
        for meta in to_remove:
            archive = os.path.join(self.backup_dir, meta["archive"])
            if os.path.exists(archive):
                os.remove(archive)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(history[excess:], f, indent=2, ensure_ascii=False)
