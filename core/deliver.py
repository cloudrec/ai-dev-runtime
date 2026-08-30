"""PHASE 17 — deterministic delivery (merge → test → push), NO AI.

Promotes an already-verified feature branch into a target branch on the host repo:
  fetch → checkout target → merge --no-ff source → run tests → push (or abort/rollback).

Every failure leaves the target branch exactly as it was (hard reset to the
pre-merge commit / merge --abort), so a bad delivery never corrupts the branch.
"""
from __future__ import annotations

import os
import signal
import subprocess

from core import git_write as gw

_TEST_TIMEOUT = int(os.getenv("RUNTIME_TEST_TIMEOUT", "180"))
_DEFAULT_TESTS = ["python3 -m pytest -q"]



def _kill_group(proc: subprocess.Popen) -> None:
    """Reap the step's whole process group. `proc` leads its own group by
    construction (start_new_session=True), so pgid == proc.pid — never look it up
    via os.getpgid(), which raises once proc is reaped and would skip cleanup."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_one(project_path: str, parts: list) -> tuple:
    """Run one delivery test step under its own process group.

    subprocess.run(timeout=) kills ONLY the direct child, so a `pytest` killed at
    RUNTIME_TEST_TIMEOUT left everything its tests had spawned running on the
    server. This path is exposed to exactly that: _DEFAULT_TESTS is the full
    suite, which measured 742-1171s against a 600s cap. Mirrors
    job_executor._run_step; the timeout is re-raised unchanged so the caller's
    recorded `str(e)` text is unaffected. Tokenization is deliberately left to the
    caller's cmd.split() — fixing that is a separate behaviour change."""
    proc = subprocess.Popen(parts, cwd=project_path, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, shell=False,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 — draining must never mask the timeout
            pass
        raise
    return proc.returncode == 0, (out + err)

def _run_tests(project_path: str, commands: list[str]) -> dict:
    results, ok = [], True
    for cmd in (commands or _DEFAULT_TESTS)[:5]:
        parts = cmd.split()
        if not parts:
            continue
        try:
            passed, out = _run_one(project_path, parts)
            results.append({"cmd": cmd, "passed": passed, "output": out[-1500:]})
            ok = ok and passed
        except Exception as e:  # noqa: BLE001
            results.append({"cmd": cmd, "passed": False, "output": str(e)[:400]})
            ok = False
    return {"ok": ok, "results": results}


def _checkout_target(pp: str, target: str) -> None:
    # prefer local branch; else track origin/<target>
    local = gw._run(pp, ["branch", "--list", target]).strip()
    if local:
        gw._run(pp, ["checkout", target])
    else:
        gw._run(pp, ["checkout", "-B", target, f"origin/{target}"])


def deliver(project_path: str, source_branch: str, target_branch: str = "master",
            test_commands: list[str] | None = None, push: bool = False) -> dict:
    pp = os.path.realpath(project_path)
    if not gw.is_repo(pp):
        return {"ok": False, "stage": "precheck", "error": "not a git repo"}
    if not source_branch:
        return {"ok": False, "stage": "precheck", "error": "source_branch required"}

    try:
        gw._run(pp, ["fetch", "origin"], check=False)
        _checkout_target(pp, target_branch)
    except gw.GitWriteError as e:
        return {"ok": False, "stage": "checkout", "error": str(e)}

    pre_commit = gw.rev_parse_short(pp)

    # merge (deterministic, no fast-forward so the merge is auditable)
    try:
        gw._run(pp, ["merge", "--no-ff", "--no-edit", source_branch])
    except gw.GitWriteError as e:
        gw._run(pp, ["merge", "--abort"], check=False)
        return {"ok": False, "stage": "merge", "error": str(e), "pre_commit": pre_commit,
                "target_branch": target_branch, "source_branch": source_branch}

    merge_commit = gw.rev_parse_short(pp)

    # test the merged result
    tests = _run_tests(pp, test_commands)
    if not tests["ok"]:
        gw._run(pp, ["reset", "--hard", pre_commit], check=False)   # roll target back — nothing delivered
        return {"ok": False, "stage": "tests", "tests": tests, "pre_commit": pre_commit,
                "reverted_to": pre_commit, "target_branch": target_branch, "source_branch": source_branch}

    pushed = None
    if push:
        try:
            gw._run(pp, ["push", "origin", target_branch])
            pushed = True
        except gw.GitWriteError as e:
            return {"ok": False, "stage": "push", "error": str(e), "merge_commit": merge_commit,
                    "pre_commit": pre_commit, "tests": tests, "target_branch": target_branch}

    return {"ok": True, "merged": True, "merge_commit": merge_commit, "pre_commit": pre_commit,
            "target_branch": target_branch, "source_branch": source_branch, "tests": tests, "pushed": pushed}


def rollback(project_path: str, target_branch: str, to_commit: str, push: bool = False) -> dict:
    """Undo a delivery: hard-reset target back to a known-good commit (+ push)."""
    pp = os.path.realpath(project_path)
    if not gw.is_repo(pp) or not to_commit:
        return {"ok": False, "error": "bad repo / to_commit"}
    try:
        _checkout_target(pp, target_branch)
        gw._run(pp, ["reset", "--hard", to_commit])
        pushed = None
        if push:
            gw._run(pp, ["push", "--force-with-lease", "origin", target_branch])
            pushed = True
        return {"ok": True, "target_branch": target_branch, "reset_to": to_commit, "pushed": pushed}
    except gw.GitWriteError as e:
        return {"ok": False, "error": str(e)}
