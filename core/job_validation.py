"""Task-specific validation for non-code job kinds.

`code_change` jobs are validated by the repository test suite. Every other kind
is validated against evidence that its *own* work happened — artifacts written,
records produced, a report rendered — never against unrelated repository tests.

The result of validation is always recorded explicitly on the job (see
`core.job_executor`), so an operator can see what was checked and why a job was
considered complete.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from core import job_kinds

#: Commands that are never acceptable as validation for a non-code job: they
#: re-introduce exactly the repo-wide gate this module exists to avoid.
_REPO_SUITE_MARKERS = ("pytest", "python -m unittest", "python3 -m unittest", "tox", "nox")


def is_repo_suite_command(cmd: str) -> bool:
    text = (cmd or "").lower()
    return any(marker in text for marker in _REPO_SUITE_MARKERS)


def strip_repo_suite_commands(commands: List[str]) -> List[str]:
    """Drop repository-suite commands from a non-code job's validation list.

    The fallback planner derives `test_commands` from repository metadata alone
    (`default_test_commands` -> `python3 -m pytest -q`). For an operational or
    content job that command is not validation of the task — it is what made
    OWNER-114..120 fail on another job's leftover defect.
    """
    return [c for c in (commands or []) if not is_repo_suite_command(c)]


def artifact_checks(project_path: str, changed_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Verify that every artifact the job claims to have produced exists.

    This is the minimum, kind-agnostic evidence: a job that says it wrote a
    report must actually have written it.
    """
    results: List[Dict[str, Any]] = []
    ok = True
    for change in changed_files or []:
        if change.get("operation") == "delete":
            continue
        rel = change.get("path")
        if not rel:
            continue
        abspath = os.path.join(project_path, rel)
        exists = os.path.exists(abspath)
        size = os.path.getsize(abspath) if exists else 0
        passed = exists and size > 0
        ok = ok and passed
        results.append({
            "check": f"artifact present and non-empty: {rel}",
            "passed": passed,
            "detail": f"exists={exists} size={size}",
        })
    return {"ok": ok, "results": results}


def validate(kind: str, project_path: str, plan: Dict[str, Any],
             changed_files: List[Dict[str, Any]],
             run_commands=None) -> Dict[str, Any]:
    """Run task-specific validation for a non-code job kind.

    Returns a dict shaped like the executor's test result
    (`{"ok": bool, "results": [...]}`) plus explicit provenance fields so the
    job record states what validation actually ran.

    `run_commands` is injected by the caller (the executor's sandboxed command
    runner) so this module stays free of subprocess handling and is unit
    testable.
    """
    checks: List[Dict[str, Any]] = []
    ok = True

    # 1) Artifacts the plan claims to produce must really exist.
    artifacts = artifact_checks(project_path, changed_files)
    checks.extend(artifacts["results"])
    ok = ok and artifacts["ok"]

    # 2) Any task-specific command the plan supplied — but never the repo suite.
    task_commands = strip_repo_suite_commands(plan.get("test_commands") or [])
    dropped = [c for c in (plan.get("test_commands") or []) if is_repo_suite_command(c)]
    if task_commands and run_commands is not None:
        cmd_result = run_commands(project_path, task_commands)
        for r in cmd_result.get("results", []):
            checks.append({
                "check": f"task command: {r.get('cmd')}",
                "passed": bool(r.get("passed")),
                "detail": (r.get("output") or "")[-500:],
            })
        ok = ok and bool(cmd_result.get("ok", True))

    # A non-code job with nothing to show for itself is not complete. This is the
    # one way such a job can fail validation on its own merits.
    if not (changed_files or task_commands):
        ok = False
        checks.append({
            "check": "job produced at least one artifact or ran one task command",
            "passed": False,
            "detail": "no artifacts and no task-specific validation commands",
        })

    return {
        "ok": ok,
        "results": checks,
        "validation_kind": job_kinds.validation_kind_for(kind),
        "repo_suite_used": False,
        "dropped_repo_suite_commands": dropped,
    }
