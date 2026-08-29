"""job_executor._run_tests: `&&`-chained validation commands (issue #10 —
planner-produced `test -s file && echo OK` was silently failing because the
old code passed the literal string "&&" as an argv token to `test`)."""
import importlib
import os
import stat
import subprocess
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_test_jobs.db"))

from core import ai_planner, job_executor, job_store  # noqa: E402


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


def test_single_command_still_works(tmp_path):
    out = job_executor._run_tests(str(tmp_path), ["python3 -c 'print(1)'"])
    assert out["ok"] is True and out["results"][0]["passed"] is True


def test_chained_command_both_steps_pass(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hi")
    out = job_executor._run_tests(str(tmp_path), [f"test -s {f.name} && echo VALIDATION_OK"])
    assert out["ok"] is True
    assert "VALIDATION_OK" in out["results"][0]["output"]


def test_chained_command_short_circuits_on_first_failure(tmp_path):
    out = job_executor._run_tests(str(tmp_path), ["test -s missing_file.md && echo SHOULD_NOT_RUN"])
    assert out["ok"] is False
    assert "SHOULD_NOT_RUN" not in out["results"][0]["output"]


def test_never_invokes_a_real_shell(tmp_path):
    """`&&` must be handled by chaining argv-level subprocess calls, not by
    shell=True — prove a shell metacharacter in a later step is inert."""
    f = tmp_path / "y.md"
    f.write_text("hi")
    marker = tmp_path / "should_not_exist.txt"
    out = job_executor._run_tests(str(tmp_path), [f"test -s {f.name} && echo hi > {marker.name}"])
    # `>` is not shell-interpreted (shell=False, argv tokens) — echo just prints
    # "hi > should_not_exist.txt" as literal words, no redirection happens.
    assert out["ok"] is True
    assert not marker.exists()


def test_multiple_test_commands_each_independently_chained(tmp_path):
    f = tmp_path / "z.md"
    f.write_text("hi")
    out = job_executor._run_tests(str(tmp_path), [
        f"test -s {f.name} && echo first_ok",
        "test -s nope.md && echo second_should_fail",
    ])
    assert out["ok"] is False
    assert out["results"][0]["passed"] is True
    assert out["results"][1]["passed"] is False


def _git(path, *args):
    subprocess.run(["git", "-C", str(path)] + list(args), check=True,
                    capture_output=True, text=True)


def _fake_cli(tmp_path, body):
    p = tmp_path / "fake_claude.py"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_repair_attempt_touching_a_different_file_still_gets_committed(tmp_path, monkeypatch):
    """The initial plan creates a.txt with a test that requires b.txt, which
    doesn't exist yet -> tests fail -> repair attempt creates b.txt (a file
    outside the original plan). Before the fix, _apply_files()'s return value
    for the repair attempt was discarded, so only a.txt got `git add`-ed and
    b.txt was left as an uncommitted stray file on disk forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "--allow-empty", "-m", "init")

    counter = tmp_path / "calls.count"
    cli = _fake_cli(tmp_path, f"""
import json, os, sys
counter = {str(counter)!r}
n = int(open(counter).read()) if os.path.exists(counter) else 0
n += 1
open(counter, "w").write(str(n))
if n == 1:
    files = [{{"path": "a.txt", "operation": "create", "content": "A"}}]
    test_commands = ["test -s b.txt"]
else:
    files = [{{"path": "b.txt", "operation": "create", "content": "B"}}]
    test_commands = []
result = json.dumps({{"summary": "s", "files": files, "test_commands": test_commands}})
envelope = {{"type": "result", "subtype": "success", "is_error": False, "result": result}}
sys.stdout.write(json.dumps(envelope))
""")
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    monkeypatch.setenv("RUNTIME_PLAN_TIMEOUT", "15")
    importlib.reload(ai_planner)

    job = job_store.create_job(project_path=str(repo), goal="g", instructions="i",
                               autonomy_level="execute_safe", auto_commit=True, auto_push=False)
    job_executor.execute(job["id"])

    final = job_store.get_job(job["id"])
    assert final["status"] == "completed", final.get("error")
    assert {c["path"] for c in final["changed_files"]} == {"a.txt", "b.txt"}

    branch = final["git_info"]["branch"]
    committed = subprocess.run(["git", "-C", str(repo), "show", "--stat", branch],
                               capture_output=True, text=True, check=True).stdout
    assert "a.txt" in committed and "b.txt" in committed
    from core import git_write
    assert not any(p.endswith("a.txt") or p.endswith("b.txt") for p in git_write.dirty_files(str(repo)))


# ── test-step timeout must reap the whole process group ──────────────────────
# subprocess.run(timeout=) kills only the direct child, so a `pytest` killed at
# RUNTIME_TEST_TIMEOUT left everything its tests had spawned running on the
# server. Real exposure: 7 recorded jobs (tasks 162/182/193/220/221) hit that cap
# on the full suite, and this repo's own suite spawns long-lived CLI stubs.

def test_timed_out_step_reaps_its_grandchildren(tmp_path, monkeypatch):
    import signal as _signal
    import subprocess as _sp
    import time as _time

    marker = tmp_path / "gc.pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen(['sleep', '90'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(90)\n")

    monkeypatch.setattr(job_executor, "_TEST_TIMEOUT", 3)
    with pytest.raises(_sp.TimeoutExpired):
        job_executor._run_step(str(tmp_path), f"python3 {script}")

    gc_pid = int(marker.read_text())
    # give the group kill a moment to land
    for _ in range(20):
        try:
            os.kill(gc_pid, 0)
        except (ProcessLookupError, OSError):
            break
        _time.sleep(0.25)
    else:
        try:
            os.killpg(gc_pid, _signal.SIGKILL)   # don't leak from the test itself
        except Exception:
            pass
        pytest.fail(f"grandchild {gc_pid} survived the step timeout — process group not reaped")
