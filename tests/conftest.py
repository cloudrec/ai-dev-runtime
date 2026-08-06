"""Конфиг pytest — общие фикстуры.

Критично: RUNTIME_DB задаётся ЗДЕСЬ, до импорта любого тестового модуля или
core.job_store. Иначе job_store._DB (читается один раз при импорте модуля)
разрешается в боевую /root/ai-dev-runtime/runtime_jobs.db, и тесты, вызывающие
recover_interrupted()/reap_orphaned(), выметают живые job-строки прямо во время
работы runtime. Раньше каждый тестовый модуль звал os.environ.setdefault() со
своим путём: побеждал первый импортированный, а teardown соседнего модуля удалял
общий файл — отсюда плавающие падения и повреждение боевой базы.
"""
import os
import sys
import tempfile

# корень проекта доступен для импортов
sys.path.insert(0, "/root/ai-dev-runtime")
os.environ.setdefault("PYTHONPATH", "/root/ai-dev-runtime")

# Жёстко (не setdefault): ни один прогон тестов не должен видеть боевую базу,
# даже если запущен один файл (`pytest tests/test_x.py`).
_TEST_DB_DIR = tempfile.mkdtemp(prefix="ai-runtime-tests-")
os.environ["RUNTIME_DB"] = os.path.join(_TEST_DB_DIR, "runtime_jobs_test.db")

# Same rule for the control plane database. Policy enforcement (preflight/completion)
# writes decisions and CLAIMS from any code path a test exercises; against the live
# file a test run would leave claims that block real work, and read live channel/gate
# state as if it were fixture data. Tests that need their own DB still monkeypatch it.
os.environ["CONTROL_PLANE_DB"] = os.path.join(_TEST_DB_DIR, "control_plane_test.db")
