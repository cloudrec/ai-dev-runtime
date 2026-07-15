"""
Validator — проверка состояния проекта после изменений.

Вызывается перед commit. Если проверка не прошла — commit отменяется.

Проверки:
  1. Синтаксис Python-файлов (py_compile)
  2. Проверка импортов (import ключевых модулей)
  3. Форматирование (необязательный ruff/black, если установлен)
  4. Запуск тестов (pytest, если есть tests/)
"""
import os
import sys
import py_compile
import subprocess
from datetime import datetime


class Validator:
    def __init__(self, project_root: str):
        self.project_root = os.path.realpath(project_root)
        self.venv_python = self._find_python()

    def _find_python(self) -> str:
        """Находит python в venv проекта или системный."""
        candidates = [
            os.path.join(self.project_root, "venv", "bin", "python3"),
            os.path.join(self.project_root, "venv", "bin", "python"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return sys.executable

    def check_all(self, changed_files=None) -> dict:
        """
        Запускает все проверки. Возвращает отчёт.
        changed_files: список путей для проверки (если None — все .py).
        """
        report = {
            "checked_at": datetime.now().isoformat(),
            "passed": True,
            "checks": [],
        }

        # 1. Синтаксис
        r = self.check_syntax(changed_files)
        report["checks"].append(r)
        if not r["ok"]:
            report["passed"] = False

        # 2. Импорты
        r = self.check_imports()
        report["checks"].append(r)
        if not r["ok"]:
            report["passed"] = False

        # 3. Тесты
        r = self.run_tests()
        report["checks"].append(r)
        if not r["ok"]:
            report["passed"] = False

        return report

    # ---- отдельные проверки ----

    def check_syntax(self, changed_files=None) -> dict:
        """Проверяет синтаксис всех .py файлов (или только изменённых)."""
        targets = changed_files or self._collect_py_files()
        errors = []
        checked = 0

        for rel in targets:
            full = os.path.join(self.project_root, rel)
            if not os.path.isfile(full):
                continue
            try:
                py_compile.compile(full, doraise=True)
                checked += 1
            except py_compile.PyCompileError as e:
                errors.append({"file": rel, "error": str(e)})

        return {
            "check": "syntax",
            "ok": len(errors) == 0,
            "files_checked": checked,
            "errors": errors,
        }

    def check_imports(self) -> dict:
        """Пробует импортировать ключевые модули проекта."""
        modules = ["core", "runtime", "api"]
        errors = []
        env = dict(os.environ)
        env["PYTHONPATH"] = self.project_root

        for mod in modules:
            try:
                result = subprocess.run(
                    [self.venv_python, "-c", f"import {mod}"],
                    capture_output=True, text=True, env=env, timeout=15,
                )
                if result.returncode != 0:
                    errors.append({"module": mod, "error": result.stderr.strip()})
            except subprocess.TimeoutExpired:
                errors.append({"module": mod, "error": "timeout"})

        return {
            "check": "imports",
            "ok": len(errors) == 0,
            "errors": errors,
        }

    def run_tests(self) -> dict:
        """Запускает pytest, если есть tests/."""
        tests_dir = os.path.join(self.project_root, "tests")
        if not os.path.isdir(tests_dir):
            return {"check": "tests", "ok": True, "skipped": "no tests/ dir"}

        # есть ли хоть один test_*.py
        test_files = [f for f in os.listdir(tests_dir) if f.startswith("test_") and f.endswith(".py")]
        if not test_files:
            return {"check": "tests", "ok": True, "skipped": "no test files"}

        try:
            result = subprocess.run(
                [self.venv_python, "-m", "pytest", tests_dir, "-v", "--tb=short"],
                capture_output=True, text=True, timeout=60,
            )
            ok = result.returncode == 0
            return {
                "check": "tests",
                "ok": ok,
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-1000:] if result.stderr else "",
            }
        except subprocess.TimeoutExpired:
            return {"check": "tests", "ok": False, "error": "tests timeout (60s)"}

    # ---- helpers ----

    def _collect_py_files(self) -> list:
        """Собирает все .py файлы проекта (без venv)."""
        exclude = {"venv", ".venv", "__pycache__", ".git", ".ai-runtime-backups"}
        files = []
        for root, dirs, filenames in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in exclude]
            for f in filenames:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(root, f), self.project_root)
                    files.append(rel)
        return files
