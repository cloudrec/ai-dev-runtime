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

# And its append-only audit log. The database was isolated; the audit beside it was not,
# so every record/ack a test exercised landed in the LIVE /var/log/ai-runtime/
# agent_control.jsonl as `job:0.0` fixture rows (12 on 2026-09-18 from three suite runs)
# — the very log the ack audit exists to make trustworthy. Tests that assert on the audit
# still point it at their own tmp_path.
os.environ["AGENT_CONTROL_AUDIT"] = os.path.join(_TEST_DB_DIR, "agent_control_test.jsonl")

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

# The hook's failure-path diagnostic, for the same reason as the databases above. That
# file is EVIDENCE: it is what distinguished "hook never fired" from "fired and crashed"
# from "event deduped" when a session's event stream looked dark. A test run writing into
# it makes that evidence untrustworthy, and it did — 102 of 120 records in the live file
# came from this suite, under sessions like `s` and `abcdef123456`.
#
# Set HERE rather than per-test, because the polluting call sites were the two that invoke
# the hook through `subprocess.run` directly instead of the suite's helper. Pinning it at
# the process level covers every call site, including ones added later that forget.
os.environ["OWNEROS_HOOK_DIAG"] = os.path.join(_TEST_DB_DIR, "owneros_hook_diag.jsonl")

# Same hazard, third door (2026-11-17). The hook loads the service config into its own
# process. Run in-process by tests, it read the LIVE configs/.env and left every key in
# os.environ for the rest of the suite — secrets included — and once live set
# AGENT_AUTOMATION_OPEN_CALLERS=api, unrelated wake-phrase tests flipped. Tests get an
# empty stand-in; the ones covering the loader point it at their own file.
_EMPTY_RUNTIME_ENV = os.path.join(_TEST_DB_DIR, "runtime.env")
open(_EMPTY_RUNTIME_ENV, "a").close()
os.environ["OWNEROS_RUNTIME_ENV"] = _EMPTY_RUNTIME_ENV

# Same hazard, second door. `closed_loop_wake._transcript_advanced` reads the session
# transcripts under ~/.claude/projects to answer "did this agent write anything since the
# wake?", which on this host is the operator's REAL transcript directory. Three existing
# stall-detection controls inverted the moment it was added: their fixed NOW is older than
# every live transcript on the machine, so a genuinely silent test agent looked busy.
# Empty means off; the tests that cover this oracle point it at their own tmp_path.
os.environ["OWNEROS_CLAUDE_PROJECTS"] = ""

# Third door. `agent_continuation_watchdog.eligible_sessions` unions the orchestrator config
# with the CONTINUATION_WATCHDOG_SESSIONS env allowlist. A test process launched from an
# operator shell that carries the LIVE allowlist (2026-09-18: two real sessions) makes the
# "watchdog enabled but nothing eligible" control report two eligible sessions and pass as
# ok. Tests that cover the env allowlist set it themselves.
os.environ.pop("CONTINUATION_WATCHDOG_SESSIONS", None)
# Fourth door, same shape. `native_supervisor` reads NATIVE_SUPERVISOR_TARGETS at import; the
# operator shell carries the live value (`*` since 2026-09-04), which turns the deny-list
# controls inside out — every registered target is supervised, including the ones the test
# just proved denied. The suite ran green on 2026-09-04 only because that shell did not
# carry it yet. Tests that cover the allowlist set it themselves.
for _k in ("NATIVE_SUPERVISOR_TARGETS", "NATIVE_SUPERVISOR_DENY_PROJECTS",
           "NATIVE_SUPERVISOR_DENY_TARGETS"):
    os.environ.pop(_k, None)

# Fifth door: the LIVE browser. Every Owner OS chat tab lives in the Chromium whose
# debugging port is 127.0.0.1:9222 on this host. `test_cdp_composer::
# test_it_refuses_when_no_composer_is_found` reached the composer-absent recovery, whose
# `_create_tab` PUT /json/new?https://chatgpt.com/c/a to that port — every full-suite run
# (and every runtime job, which runs the suite) opened a real tab, found 2026-11-01 as a
# stray "New chat" on chatgpt.com/c/a. Tabs count toward CDP_MAX_PAGES; enough of them
# make live wake delivery refuse with `browser_degraded:too_many_pages`.
#
# Socket-level, so no copy of any module and no future call site can get through. The
# composer swallows connection errors, so a refusal alone would pass silently: every
# attempt is recorded and fails the test that made it.
import socket as _socket  # noqa: E402

import pytest  # noqa: E402

_LIVE_CDP_PORT = 9222
_live_cdp_attempts: list = []
_real_sock_connect = _socket.socket.connect
_real_create_connection = _socket.create_connection


def _is_live_cdp(addr) -> bool:
    try:
        return int(addr[1]) == _LIVE_CDP_PORT and str(addr[0]) in ("127.0.0.1", "localhost", "::1")
    except Exception:  # noqa: BLE001 — not an (host, port) address
        return False


def _walled_connect(self, addr):
    if _is_live_cdp(addr):
        _live_cdp_attempts.append(str(addr))
        raise ConnectionRefusedError("test suite: the live browser's CDP port is walled off")
    return _real_sock_connect(self, addr)


def _walled_create_connection(addr, *a, **k):
    if _is_live_cdp(addr):
        _live_cdp_attempts.append(str(addr))
        raise ConnectionRefusedError("test suite: the live browser's CDP port is walled off")
    return _real_create_connection(addr, *a, **k)


_socket.socket.connect = _walled_connect
_socket.create_connection = _walled_create_connection


@pytest.fixture(autouse=True)
def _no_live_browser():
    _live_cdp_attempts.clear()
    yield
    assert not _live_cdp_attempts, (
        f"test reached the live browser's CDP port {_live_cdp_attempts}; "
        "stub browser_degraded/find_target/_create_tab/_http instead")


# Policy switches must never outlive the test that set them: a leaked
# AGENT_AUTOMATION_OPEN_CALLERS or WAKE_BRIDGE_* silently changes every later test.
_POLICY_ENV_PREFIXES = ("AGENT_AUTOMATION", "WAKE_BRIDGE")


@pytest.fixture(autouse=True)
def _policy_env_does_not_leak(request):
    before = {k: v for k, v in os.environ.items() if k.startswith(_POLICY_ENV_PREFIXES)}
    yield
    after = {k: v for k, v in os.environ.items() if k.startswith(_POLICY_ENV_PREFIXES)}
    if after != before:
        for k in set(after) - set(before):
            del os.environ[k]
        os.environ.update(before)
        pytest.fail(f"{request.node.nodeid} leaked policy env: "
                    f"{sorted(set(after.items()) ^ set(before.items()))}", pytrace=False)
