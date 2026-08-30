"""core/deliver.py — the PHASE 17 merge -> test -> push gate's test runner.

`core/deliver.py` is live: api/v1.py exposes POST /api/v1/deliver, which calls
deliver.deliver(). Its _run_tests defaults to the FULL suite
(`python3 -m pytest -q`) under the same RUNTIME_TEST_TIMEOUT that
job_executor uses, so it is exposed to exactly the same timeout — the suite
measured 742-1171s against a 600s cap on 2026-08-29.

subprocess.run(timeout=) kills only the direct child, so a delivery whose tests
timed out left everything those tests had spawned running on the server. These
pin the process-group reap and that ordinary pass/fail is unchanged.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from core import deliver


def test_passing_step_reports_ok_and_captures_output(tmp_path):
    ok, out = deliver._run_one(str(tmp_path), ["python3", "-c", "print('hello-from-step')"])
    assert ok is True
    assert "hello-from-step" in out


def test_failing_step_reports_not_ok(tmp_path):
    ok, _ = deliver._run_one(str(tmp_path), ["python3", "-c", "import sys; sys.exit(3)"])
    assert ok is False


def test_timed_out_step_reaps_its_grandchildren(tmp_path, monkeypatch):
    marker = tmp_path / "gc.pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '90'])\n"
        f"open({str(marker)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(90)\n")

    monkeypatch.setattr(deliver, "_TEST_TIMEOUT", 3)
    with pytest.raises(subprocess.TimeoutExpired):
        deliver._run_one(str(tmp_path), ["python3", str(script)])

    gc_pid = int(marker.read_text())
    for _ in range(20):
        try:
            os.kill(gc_pid, 0)
        except (ProcessLookupError, OSError):
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(gc_pid, signal.SIGKILL)   # never leak from the test itself
        except Exception:
            pass
        pytest.fail(f"grandchild {gc_pid} survived the delivery-step timeout")


def test_run_tests_records_the_timeout_as_a_failure(tmp_path, monkeypatch):
    """A timing-out step must surface as a normal failed result, not an
    exception escaping into the delivery flow."""
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(deliver, "_TEST_TIMEOUT", 2)
    res = deliver._run_tests(str(tmp_path), [f"python3 {script}"])
    assert res["ok"] is False
    assert "timed out" in res["results"][0]["output"]
