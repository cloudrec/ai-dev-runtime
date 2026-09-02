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

# корень проекта доступен для импортов. ОТНОСИТЕЛЬНО этого файла, не жёстким
# путём: прогон из изолированного worktree (runtime-джоба, baseline-проверка)
# обязан импортировать код ЭТОГО дерева — жёсткий /root/ai-dev-runtime тянул
# исправленный код из живого дерева и давал ложно-зелёный baseline.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
os.environ["PYTHONPATH"] = _REPO_ROOT

# Жёстко (не setdefault): ни один прогон тестов не должен видеть боевую базу,
# даже если запущен один файл (`pytest tests/test_x.py`).
_TEST_DB_DIR = tempfile.mkdtemp(prefix="ai-runtime-tests-")
os.environ["RUNTIME_DB"] = os.path.join(_TEST_DB_DIR, "runtime_jobs_test.db")

# Same rule for the control plane database. Policy enforcement (preflight/completion)
# writes decisions and CLAIMS from any code path a test exercises; against the live
# file a test run would leave claims that block real work, and read live channel/gate
# state as if it were fixture data. Tests that need their own DB still monkeypatch it.
os.environ["CONTROL_PLANE_DB"] = os.path.join(_TEST_DB_DIR, "control_plane_test.db")

# And the agent-control database, for the same reason: supervisor/watchdog/recovery
# tables must never be fixture data read from — or written into — the live file.
os.environ["AGENT_CONTROL_DB"] = os.path.join(_TEST_DB_DIR, "agent_control_test.db")

# Job worktrees likewise: a test-run executor must never materialize worktrees
# under the live /var/lib/ai-runtime tree.
os.environ["RUNTIME_WORKTREE_ROOT"] = os.path.join(_TEST_DB_DIR, "worktrees")

# Same rule again, for the runtime's own view of its sessions. `claude agents --json`
# reports the REAL sessions running on this host, so a test that consults it is reading
# live machine state as fixture data: whether a suite passes would depend on what the
# operator happens to be running. Observed exactly that — three closed-loop tests using
# a real conversation id resolved against a genuinely `busy` live pane and inverted their
# own assertions. Hard off, like the databases above; the tests that cover the native
# path enable it explicitly and inject their own listing.
os.environ["OWNEROS_NATIVE_SESSIONS"] = "0"

# Same hazard, second door. `closed_loop_wake._transcript_advanced` reads the session
# transcripts under ~/.claude/projects to answer "did this agent write anything since the
# wake?", which on this host is the operator's REAL transcript directory. Three existing
# stall-detection controls inverted the moment it was added: their fixed NOW is older than
# every live transcript on the machine, so a genuinely silent test agent looked busy.
# Empty means off; the tests that cover this oracle point it at their own tmp_path.
os.environ["OWNEROS_CLAUDE_PROJECTS"] = ""
