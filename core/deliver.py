"""PHASE 17 — deterministic delivery (merge → test → push), NO AI.

Promotes an already-verified feature branch into a target branch on the host repo:
  fetch → checkout target → merge --no-ff source → run tests → push (or abort/rollback).

Every failure leaves the target branch exactly as it was (hard reset to the
pre-merge commit / merge --abort), so a bad delivery never corrupts the branch.
"""
from __future__ import annotations

import os
import subprocess

from core import git_write as gw

_TEST_TIMEOUT = int(os.getenv("RUNTIME_TEST_TIMEOUT", "180"))
_DEFAULT_TESTS = ["python3 -m pytest -q"]


def _run_tests(project_path: str, commands: list[str]) -> dict:
    results, ok = [], True
    for cmd in (commands or _DEFAULT_TESTS)[:5]:
        parts = cmd.split()
        if not parts:
            continue
        try:
            p = subprocess.run(parts, cwd=project_path, capture_output=True, text=True,
                               timeout=_TEST_TIMEOUT, shell=False)
            passed = p.returncode == 0
            results.append({"cmd": cmd, "passed": passed, "output": (p.stdout + p.stderr)[-1500:]})
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
