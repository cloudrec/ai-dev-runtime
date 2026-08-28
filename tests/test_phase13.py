"""PHASE 13 — tests for the new autonomous-execution modules (no network)."""
import os
import stat
import subprocess
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_test_jobs.db"))

from core import job_store, git_write, ai_planner  # noqa: E402


def _fake_cli(tmp_path, body):
    p = tmp_path / "fake_claude.py"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def setup_module(_m):
    # conftest.py points RUNTIME_DB at ONE shared temp file for the whole pytest
    # session. Removing it here raced other test modules' still-live background
    # threads (job_executor's heartbeat, or an unjoined dispatch thread) writing
    # to it mid-run -> sqlite3.OperationalError: attempt to write a readonly
    # database in unrelated files. Clearing the ROWS instead leaves the file
    # (and any other module's open connection) intact, while still giving this
    # module the same "starts empty" guarantee the old os.remove() gave it.
    job_store.init_db()
    with job_store._LOCK, job_store._conn() as c:
        c.execute("DELETE FROM jobs")


def test_job_create_persist_get():
    j = job_store.create_job(project_path="/tmp/x", goal="g", instructions="i", autonomy_level="prepare")
    assert j["status"] == "draft" and j["autonomy_level"] == "prepare"
    got = job_store.get_job(j["id"])
    assert got and got["goal"] == "g"
    job_store.update_job(j["id"], status="queued")
    assert job_store.get_job(j["id"])["status"] == "queued"
    job_store.append_log(j["id"], "info", "hello")
    assert job_store.get_job(j["id"])["logs"][-1]["msg"] == "hello"


def test_recover_interrupted_requires_reapproval():
    j = job_store.create_job(project_path="/tmp/x", goal="g", status="editing")
    n = job_store.recover_interrupted()
    assert n >= 1
    assert job_store.get_job(j["id"])["status"] == "waiting_approval"


def test_git_slug_and_no_add_all():
    assert git_write.slug("Add a Feature!") == "add-a-feature"
    with pytest.raises(git_write.GitWriteError):
        git_write.add_paths("/tmp", [])  # refuses empty (== git add .)


def test_planner_validate_rejects_bad_paths():
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "../escape.py", "operation": "create", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": ".env", "operation": "create", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "a.py", "operation": "rm -rf", "content": "x"}]}, [])
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "a.py", "operation": "create", "content": ""}]}, [])
    # allow-list enforcement
    with pytest.raises(ai_planner.PlannerError):
        ai_planner._validate({"files": [{"path": "src/a.py", "operation": "create", "content": "x"}]}, ["utils"])
    ok = ai_planner._validate({"files": [{"path": "utils/a.py", "operation": "create", "content": "x"}]}, ["utils"])
    assert ok["files"][0]["path"] == "utils/a.py"


def test_planner_available_flag():
    assert isinstance(ai_planner.available(), bool)


def _reload(monkeypatch, cli, timeout=10, heartbeat=None, max_output=None):
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", str(timeout))
    if heartbeat is not None:
        monkeypatch.setenv("RUNTIME_PLAN_HEARTBEAT_SECS", str(heartbeat))
    if max_output is not None:
        monkeypatch.setenv("RUNTIME_PLAN_MAX_OUTPUT_BYTES", str(max_output))
    import importlib
    importlib.reload(ai_planner)


def _fake_cli_envelope(tmp_path, envelope_obj):
    """A fake CLI that prints a --output-format json envelope. Uses repr() to
    embed the payload as a Python string literal so nested-JSON quoting/escaping
    is handled by Python itself instead of by hand."""
    import json as _json
    payload = _json.dumps(envelope_obj)
    return _fake_cli(tmp_path, f"import sys\nsys.stdout.write({payload!r})\n")


def test_planner_success_path_via_fake_cli(tmp_path, monkeypatch):
    plan_json = '{"summary":"ok","files":[{"path":"a.py","operation":"create","content":"x=1"}]}'
    cli = _fake_cli_envelope(tmp_path, {
        "type": "result", "subtype": "success", "is_error": False, "result": plan_json,
    })
    _reload(monkeypatch, cli)

    result = ai_planner.plan("g", "i", "/tmp", [])
    assert result["summary"] == "ok"
    assert result["files"][0]["path"] == "a.py"


def test_planner_timeout_kills_process_group_and_fires_heartbeats(tmp_path, monkeypatch):
    # Hanging parent that also forks a grandchild ignoring SIGTERM, to prove the
    # whole process group is reaped (not just the direct child) and heartbeats
    # fire while waiting rather than going silent until the hard timeout.
    cli = _fake_cli(tmp_path, (
        "import signal, subprocess, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen(['sleep', '30'])\n"
        "time.sleep(30)\n"
    ))
    _reload(monkeypatch, cli, timeout=3, heartbeat=1)

    hits = []
    with pytest.raises(ai_planner.PlannerError, match="timed out"):
        ai_planner.plan("g", "i", "/tmp", [], heartbeat_cb=lambda e: hits.append(e))
    assert len(hits) >= 2

    # nothing left running under the fake CLI's name — the process group,
    # including the SIGTERM-ignoring grandchild, was actually reaped.
    ps = subprocess.run(["pgrep", "-af", cli], capture_output=True, text=True)
    assert cli not in ps.stdout


def test_planner_hanging_parent_with_no_children_still_times_out(tmp_path, monkeypatch):
    # Simplest hang: a single process that never exits and never forks. Confirms
    # the timeout path doesn't depend on there being a process group to reap.
    cli = _fake_cli(tmp_path, "import time\ntime.sleep(30)\n")
    _reload(monkeypatch, cli, timeout=2, heartbeat=1)

    start = __import__("time").monotonic()
    with pytest.raises(ai_planner.PlannerError, match="timed out"):
        ai_planner.plan("g", "i", "/tmp", [])
    elapsed = __import__("time").monotonic() - start
    assert elapsed < 10  # bounded by the 2s timeout, not the fake CLI's 30s sleep

    ps = subprocess.run(["pgrep", "-af", cli], capture_output=True, text=True)
    assert cli not in ps.stdout


def test_planner_hanging_child_survives_parent_exit_but_group_is_reaped(tmp_path, monkeypatch):
    # Parent forks a detached-looking child and exits immediately itself while
    # the child keeps running — the leaked child must still die with the group.
    # UNIQUE sleep duration: a global `pgrep "sleep 30"` collides with any
    # unrelated `sleep 30` on the box (2026-08-03: another agent's poll loop made
    # this test fail while the reap itself worked fine).
    marker = "30.7391"
    cli = _fake_cli(tmp_path, (
        "import subprocess, sys\n"
        f"subprocess.Popen(['sleep', '{marker}'])\n"
        "sys.exit(0)\n"
    ))
    _reload(monkeypatch, cli, timeout=10)

    # Parent exits fast with no stdout -> classified as empty output, not a hang.
    with pytest.raises(ai_planner.PlannerError, match="empty output"):
        ai_planner.plan("g", "i", "/tmp", [])

    import time as _t
    _t.sleep(0.5)
    ps = subprocess.run(["pgrep", "-af", f"sleep {marker}"], capture_output=True, text=True)
    assert f"sleep {marker}" not in ps.stdout


def test_planner_malformed_response_raises(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "print('not json at all')\n")
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError, match="did not return JSON"):
        ai_planner.plan("g", "i", "/tmp", [])


def test_planner_empty_stdout_raises(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "pass\n")
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError, match="empty output"):
        ai_planner.plan("g", "i", "/tmp", [])


def test_planner_immediate_provider_error_is_classified(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, (
        "import sys\n"
        "sys.stderr.write('boom: something broke\\n')\n"
        "sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError, match="claude cli error"):
        ai_planner.plan("g", "i", "/tmp", [])


def test_planner_account_limit_error_is_classified(tmp_path, monkeypatch):
    envelope = '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,"result":""}'
    cli = _fake_cli(tmp_path, f"print('{envelope}')\n")
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError, match="provider_limit_exceeded"):
        ai_planner.plan("g", "i", "/tmp", [])


def test_planner_interactive_looking_stderr_is_classified(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, (
        "import sys\n"
        "sys.stderr.write('Press Enter to continue...\\n')\n"
        "sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)
    with pytest.raises(ai_planner.PlannerError, match="provider_interactive_prompt_detected"):
        ai_planner.plan("g", "i", "/tmp", [])


def test_planner_oversized_output_is_bounded_not_hung(tmp_path, monkeypatch):
    # A response far larger than the cap must not hang (pipe-buffer deadlock)
    # or blow up memory — it gets truncated and then fails JSON parsing cleanly.
    cli = _fake_cli(tmp_path, "import sys\nsys.stdout.write('x' * (2_000_000))\n")
    _reload(monkeypatch, cli, timeout=15, max_output=1000)

    import time as _t
    start = _t.monotonic()
    with pytest.raises(ai_planner.PlannerError):
        ai_planner.plan("g", "i", "/tmp", [])
    assert _t.monotonic() - start < 10


def test_drain_caps_output_size():
    import io
    data = b"y" * 5000
    pipe = io.BytesIO(data)
    out = ai_planner._drain(pipe, 100)
    assert len(out) < 5000
    assert out.startswith("y" * 100)
    assert "truncated" in out


def test_classify_failure_patterns():
    assert ai_planner._classify_failure("", "please log in to continue", None) == "provider_auth_required"
    assert ai_planner._classify_failure("", "429 too many requests", None) == "provider_limit_exceeded"
    assert ai_planner._classify_failure("", "select your theme", None) == "provider_setup_required"
    assert ai_planner._classify_failure("", "(y/n)", None) == "provider_interactive_prompt_detected"
    envelope = {"api_error_status": 401}
    assert ai_planner._classify_failure("", "", envelope) == "provider_auth_required"
    envelope = {"permission_denials": [{"tool": "Bash"}]}
    assert ai_planner._classify_failure("", "", envelope) == "provider_permission_denied"
