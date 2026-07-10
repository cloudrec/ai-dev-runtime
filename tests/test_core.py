"""
Тесты для core-модулей AI Runtime Agent.
Запуск: cd /root/ai-dev-runtime && python -m pytest tests/ -v
"""
import os
import sys
import tempfile
import json

import pytest

# добавляем корень проекта в path
sys.path.insert(0, "/root/ai-dev-runtime")

from core.file_engine import FileEngine
from core.safety import approve_action, validate_file_operation
from core.parser import parse_command, guess_intent
from core.planner import plan_task
from core.git_bridge import GitBridge
from core.backup_engine import BackupEngine
from core.security import check_api_key, is_public_path


# ---- FileEngine ----

class TestFileEngine:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp(prefix="test_")
        self.fe = FileEngine(self.tmp)

    def test_create_file(self):
        r = self.fe.create_file("a.py", "print('hi')")
        assert r["ok"] is True
        assert os.path.exists(os.path.join(self.tmp, "a.py"))

    def test_replace_file(self):
        self.fe.create_file("b.py", "old")
        r = self.fe.replace_file("b.py", "new")
        assert r["ok"] is True
        with open(os.path.join(self.tmp, "b.py")) as f:
            assert f.read() == "new"

    def test_insert_at(self):
        self.fe.create_file("c.py", "def main():\n    pass\n")
        r = self.fe.insert_at("c.py", "def main():", "    return 1")
        assert r["ok"] is True

    def test_replace_block(self):
        self.fe.create_file("d.py", "# START\nold\n# END\n")
        r = self.fe.replace_block("d.py", "# START", "# END", "new")
        assert r["ok"] is True
        with open(os.path.join(self.tmp, "d.py")) as f:
            content = f.read()
            assert "new" in content
            assert "old" not in content

    def test_delete_file(self):
        self.fe.create_file("e.py", "x")
        r = self.fe.delete_file("e.py")
        assert r["ok"] is True
        assert not os.path.exists(os.path.join(self.tmp, "e.py"))

    def test_create_dir(self):
        r = self.fe.create_dir("modules/sub")
        assert r["ok"] is True
        assert os.path.isdir(os.path.join(self.tmp, "modules", "sub"))

    def test_unsafe_path_blocked(self):
        with pytest.raises(ValueError):
            self.fe.create_file("../escape.txt", "hack")

    def test_create_existing_file_fails(self):
        self.fe.create_file("f.py", "x")
        with pytest.raises(FileExistsError):
            self.fe.create_file("f.py", "y")


# ---- Safety ----

class TestSafety:
    def test_approve_safe_step(self):
        assert approve_action({"step": "create logger"}) is True

    def test_block_dangerous(self):
        assert approve_action({"step": "rm -rf /"}) is False
        assert approve_action({"step": "sudo shutdown"}) is False

    def test_validate_file_operation_ok(self):
        assert validate_file_operation("src/app.py", "/project") is True

    def test_validate_file_operation_escape(self):
        assert validate_file_operation("../etc/passwd", "/project") is False


# ---- Parser ----

class TestParser:
    def test_intent_logging(self):
        assert guess_intent("добавь лог") == "add_logging"

    def test_intent_api(self):
        assert guess_intent("создай api") == "create_api"

    def test_intent_general(self):
        assert guess_intent("что-то другое") == "general"

    def test_parse_command(self):
        r = parse_command("добавь лог", {})
        assert r["raw"] == "добавь лог"
        assert "intent" in r


# ---- Planner ----

class TestPlanner:
    def test_plan_logging(self):
        plan = plan_task({"intent": "add_logging"}, {})
        assert "create logger" in plan["steps"]

    def test_plan_api(self):
        plan = plan_task({"intent": "create_api"}, {})
        assert "setup fastapi" in plan["steps"]


# ---- BackupEngine ----

class TestBackupEngine:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp(prefix="bk_")
        with open(os.path.join(self.tmp, "file.txt"), "w") as f:
            f.write("original")
        self.be = BackupEngine(self.tmp, max_backups=3)

    def test_snapshot(self):
        m = self.be.snapshot(reason="test")
        assert m["ok"] if "ok" in m else m["id"]  # id присутствует
        assert os.path.exists(os.path.join(self.be.backup_dir, m["archive"]))

    def test_list_backups(self):
        self.be.snapshot("t1")
        self.be.snapshot("t2")
        backups = self.be.list_backups()
        assert len(backups) >= 2

    def test_rollback(self):
        m = self.be.snapshot("before change")
        # меняем файл
        with open(os.path.join(self.tmp, "file.txt"), "w") as f:
            f.write("changed")
        # откат
        r = self.be.rollback(m["id"])
        assert r["ok"] is True
        with open(os.path.join(self.tmp, "file.txt")) as f:
            assert f.read() == "original"


# ---- Security ----

class TestSecurity:
    def test_public_path(self):
        assert is_public_path("/health") is True
        assert is_public_path("/docs") is True
        assert is_public_path("/task") is False

    def test_api_key_empty_env_allows(self):
        # если ключи не настроены — пропускаем (dev-режим)
        os.environ.pop("AI_RUNTIME_API_KEY", None)
        assert check_api_key("") is True

    def test_api_key_with_env(self):
        os.environ["AI_RUNTIME_API_KEY"] = "secret123"
        assert check_api_key("secret123") is True
        assert check_api_key("wrong") is False
        assert check_api_key("") is False
        os.environ.pop("AI_RUNTIME_API_KEY", None)
