"""The deploy step must be unreachable when the gate is red.

2026-08-30: a gate, a backup, a commit and a service restart were chained into one shell
command. Eight tests failed and the deploy ran anyway. The deployed code happened to be
fine — the failures were fixture sequencing — but that was luck. These tests pin the
guard so the same shape cannot recur.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

# Derived, not spelled. A hardcoded absolute path made this file exercise the script in
# /root/ai-dev-runtime no matter which tree pytest was started in — and every hot-loaded
# change in this repo is verified in an inert copy first, so the test ran one tree's
# script against another tree's environment.
SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "tools" / "guarded_deploy.sh")


def _run(gate: str, deploy: str, *extra: str):
    return subprocess.run([SCRIPT, "--gate", gate, "--deploy", deploy, *extra],
                          capture_output=True, text=True, timeout=60)


def test_a_red_gate_refuses_the_deploy(tmp_path):
    marker = tmp_path / "deployed"
    r = _run("exit 1", f"touch {marker}")
    assert r.returncode == 1, r.stderr
    assert not marker.exists(), "the deploy step ran behind a failing gate"
    assert "REFUSED" in r.stderr


def test_a_green_gate_runs_the_deploy(tmp_path):
    marker = tmp_path / "deployed"
    r = _run("true", f"touch {marker}")
    assert r.returncode == 0, r.stderr
    assert marker.exists()


def test_a_realistic_red_pytest_gate_refuses(tmp_path):
    """The exact shape that failed: a pytest gate reporting failures."""
    marker = tmp_path / "deployed"
    failing = tmp_path / "test_red.py"
    failing.write_text("def test_red():\n    assert False\n")
    # The gate runs pytest on a file in a temp directory, and pytest collects from the
    # common ancestor of its rootdir and its arguments. Inheriting this process's cwd
    # therefore made that ancestor /tmp when the suite was started from a tree under
    # /tmp — and collecting /tmp walks every session scratchpad on the box. The gate took
    # 0.03s from a confined cwd and over 90s without one, so this test's runtime depended
    # entirely on where the outer suite happened to be launched. Confine it.
    r = _run(f"cd {tmp_path} && python3 -m pytest {failing.name} -q", f"touch {marker}")
    assert r.returncode == 1
    assert not marker.exists()


def test_a_deploy_failure_is_reported_distinctly(tmp_path):
    """A gate that passed and a deploy that failed is a different situation from a red
    gate, and must not be reported as one."""
    r = _run("true", "exit 7")
    assert r.returncode == 3, r.stdout + r.stderr


def test_dry_run_never_touches_the_deploy(tmp_path):
    marker = tmp_path / "deployed"
    r = _run("true", f"touch {marker}", "--dry-run")
    assert r.returncode == 0
    assert not marker.exists()


def test_missing_arguments_fail_closed(tmp_path):
    r = subprocess.run([SCRIPT, "--gate", "true"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "usage" in r.stderr.lower()


def test_the_gate_output_is_shown_so_a_refusal_is_diagnosable():
    r = _run("echo GATE_SPEAKS; exit 1", "true")
    assert "GATE_SPEAKS" in r.stdout
    assert r.returncode == 1


def test_a_gate_that_calls_exit_cannot_kill_the_guard(tmp_path):
    """`eval` runs in the CURRENT shell, so a gate that exits — a wrapper script, a
    `set -e` runner, the literal `exit 1` — would terminate the guard and return the
    gate's own status: it LOOKS like a refusal but skips the refusal entirely and the
    operator sees no message. Found while writing this guard."""
    marker = tmp_path / "deployed"
    r = _run("exit 1", f"touch {marker}")
    assert not marker.exists()
    assert r.returncode == 1
    assert "REFUSED" in r.stderr, "the refusal must be REPORTED, not merely implied"
    assert "GATE EXIT: 1" in r.stdout


def test_a_deploy_that_calls_exit_is_contained_too(tmp_path):
    r = _run("true", "exit 7")
    assert r.returncode == 3
    assert "DEPLOY EXIT: 7" in r.stdout
