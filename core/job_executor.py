"""PHASE 13 — job executor: plan -> backup -> branch -> edit -> test -> commit -> push.

Reuses the existing engines (file_engine, backup_engine). Git via git_write.
No shell=True. Autonomy-gated. Runs in a background thread.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import threading

from core import ai_planner, git_write, job_store
from core.backup_engine import BackupEngine
from core.file_engine import FileEngine

AUTONOMY_ORDER = ["observe", "suggest", "prepare", "execute_safe", "execute_full", "deploy"]
_MAX_REPAIRS = int(os.getenv("RUNTIME_MAX_REPAIRS", "1"))
_TEST_TIMEOUT = int(os.getenv("RUNTIME_TEST_TIMEOUT", "300"))
# comfortably under job_store._HEARTBEAT_STALE_SECS (20s) so a live job never
# looks orphaned to recover_interrupted(), even during a long silent test run.
_HEARTBEAT_INTERVAL_SECS = int(os.getenv("RUNTIME_HEARTBEAT_INTERVAL_SECS", "5"))


def _idx(level: str) -> int:
    return AUTONOMY_ORDER.index(level) if level in AUTONOMY_ORDER else 0


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except FileNotFoundError:
        return "absent"


def _run_step(project_path: str, step: str) -> tuple[bool, str]:
    parts = shlex.split(step)
    if not parts:
        return True, ""
    p = subprocess.run(parts, cwd=project_path, capture_output=True, text=True,
                       timeout=_TEST_TIMEOUT, shell=False)
    return p.returncode == 0, (p.stdout + p.stderr)


def _run_tests(project_path: str, commands: list[str]) -> dict:
    """Each entry in `commands` may be a single command OR a `&&`-chained
    sequence (planner-produced validation like `test -s foo && echo OK` is
    common). Still never shell=True — each step is tokenized with shlex and
    run as its own argv, chained steps short-circuit on the first failure
    exactly like a real `&&` would, without ever invoking an actual shell."""
    results = []
    ok = True
    for cmd in commands[:5]:
        steps = [s.strip() for s in cmd.split("&&") if s.strip()]
        if not steps:
            continue
        step_ok = True
        output = ""
        try:
            for step in steps:
                passed, out = _run_step(project_path, step)
                output += out
                if not passed:
                    step_ok = False
                    break
        except Exception as e:  # noqa: BLE001
            step_ok = False
            output += str(e)[:400]
        results.append({"cmd": cmd, "passed": step_ok, "output": output[-1500:]})
        ok = ok and step_ok
    return {"ok": ok, "results": results}


def _apply_files(project_path: str, files: list[dict]) -> list[dict]:
    fe = FileEngine(project_path)
    changed = []
    for f in files:
        rel = f["path"]
        op = f["operation"]
        abspath = os.path.join(project_path, rel)
        before = _hash(abspath)
        if op == "create":
            fe.create_file(rel, f.get("content", ""))
        elif op == "replace":
            fe.replace_file(rel, f.get("content", ""))
        elif op == "patch":
            fe.replace_file(rel, f.get("content", f.get("patch", "")))
        elif op == "delete":
            fe.delete_file(rel)
        elif op == "mkdir":
            fe.create_dir(rel)
        changed.append({"path": rel, "operation": op, "before": before, "after": _hash(abspath)})
    return changed


_REPORT_DIR = os.getenv("RUNTIME_REPORT_DIR", "/opt/seo/reports/runtime")


def _write_report(job: dict) -> None:
    try:
        os.makedirs(_REPORT_DIR, exist_ok=True)
        plan = job.get("plan") or {}
        gi = job.get("git_info") or {}
        tests = job.get("tests") or {}
        lines = [
            f"# Runtime Job Report — {job['id']}", "",
            f"- **Status:** {job['status']}", f"- **Task:** OWNER-{job.get('task_id')}",
            f"- **Project:** {job.get('project_path')} (id {job.get('project_id')})",
            f"- **Autonomy:** {job.get('autonomy_level')} · risk {job.get('risk_level')} · dangerous {job.get('dangerous')}",
            f"- **Goal:** {job.get('goal')}", "",
            f"## Plan\n{plan.get('summary','')}", "",
            "### File operations",
            *[f"- `{c.get('operation')}` {c.get('path')} ({c.get('before')}→{c.get('after')})" for c in (job.get('changed_files') or [])],
            "", f"## Tests\nok={tests.get('ok')}",
            *[f"- {'PASS' if r.get('passed') else 'FAIL'} `{r.get('cmd')}`" for r in tests.get('results', [])],
            "", "## Git",
            f"- branch: `{gi.get('branch')}`", f"- commit: `{gi.get('commit')}`",
            f"- remote: {gi.get('remote')}", f"- pushed: {gi.get('pushed')}",
            "", f"## Error\n{job.get('error') or '(none)'}",
            "", "## Rollback\nA pre-execute backup was taken via BackupEngine; restore with its snapshot id if needed.",
        ]
        with open(os.path.join(_REPORT_DIR, f"{job['id']}.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass


def _finish(job_id: str, status: str, **extra):
    job = job_store.update_job(job_id, status=status, finished_at=job_store._now(), **extra)
    if job:
        _write_report(job)


def execute(job_id: str) -> None:
    """Synchronous pipeline (call in a thread)."""
    job = job_store.get_job(job_id)
    if not job:
        return
    pp = job["project_path"]
    job_store.update_job(job_id, started_at=job_store._now())

    stop_heartbeat = threading.Event()

    def _pulse() -> None:
        while not stop_heartbeat.wait(_HEARTBEAT_INTERVAL_SECS):
            job_store.touch_heartbeat(job_id)
    hb_thread = threading.Thread(target=_pulse, daemon=True)
    job_store.touch_heartbeat(job_id)
    hb_thread.start()
    try:
        _run_pipeline(job_id, job, pp)
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=2)


def _run_pipeline(job_id: str, job: dict, pp: str) -> None:
    # 1) PLAN
    job_store.update_job(job_id, status="planning")
    job_store.append_log(job_id, "info", "planning via AI provider")
    def _heartbeat(elapsed: float) -> None:
        job_store.append_log(job_id, "info", f"planning… still running ({int(elapsed)}s elapsed)")

    fallback_used = False
    try:
        plan = ai_planner.plan(job["goal"] or "", job["instructions"] or "", pp, job.get("allowed_paths") or [],
                               heartbeat_cb=_heartbeat)
    except ai_planner.PlannerError as e:
        reason = str(e)
        # No provider at all -> genuinely cannot proceed; stay blocked (a
        # deterministic fallback would mask a misconfigured host, not repair it).
        if "provider_not_configured" in reason:
            job_store.append_log(job_id, "warn", "AI provider not configured — job blocked")
            _finish(job_id, "blocked", error="provider_not_configured")
            return
        # Any other planner failure (timeout / empty / malformed / non-JSON /
        # plain-text / provider error): do NOT kill the job. Record sanitized
        # diagnostics and continue on ONE deterministic local fallback plan.
        # This is not retried in a loop — a single fallback, then execute.
        diag = {
            "reason": reason,
            "raw": getattr(e, "raw", "") or "",
            "tokens": getattr(e, "tokens", None),
            "cost_usd": getattr(e, "cost_usd", None),
            "duration_ms": getattr(e, "duration_ms", None),
            "timed_out": bool(getattr(e, "timed_out", False)),
        }
        job_store.append_log(job_id, "warn",
                             f"planner failed ({reason[:120]}) — building deterministic fallback plan")
        try:
            plan = ai_planner.build_fallback_plan(
                job["goal"] or "", job["instructions"] or "", pp,
                job.get("allowed_paths") or [], task_id=job.get("task_id"), diagnostics=diag)
        except Exception as fe:  # noqa: BLE001
            job_store.append_log(job_id, "error", f"fallback planning failed: {fe}")
            _finish(job_id, "failed", error=f"planner and fallback both failed: {reason[:300]}")
            return
        fallback_used = True
        # Mark in job metadata that fallback planning was used + preserve accounting.
        job_store.update_job(job_id, artifacts=(job.get("artifacts") or []) + [{
            "fallback_planning": True, "reason": reason[:200],
            "timed_out": diag["timed_out"], "tokens": diag["tokens"],
            "cost_usd": diag["cost_usd"], "duration_ms": diag["duration_ms"],
        }])
        job_store.append_log(job_id, "info",
                             "fallback plan generated — continuing to execution (no planner retry)")
    job_store.update_job(job_id, plan=plan, risk_level=plan.get("risk_level", job["risk_level"]))
    if fallback_used:
        job_store.append_log(job_id, "info", f"FALLBACK PLAN in use ({len(plan['files'])} safe file op)")
    job_store.append_log(job_id, "info", f"plan: {plan.get('summary','')[:120]} ({len(plan['files'])} file ops)")

    # observe/suggest -> plan only, stop here
    if _idx(job["autonomy_level"]) <= _idx("suggest"):
        _finish(job_id, "completed")
        job_store.append_log(job_id, "info", "plan-only (autonomy suggest/observe) — no changes applied")
        return

    # 2) BACKUP
    job_store.update_job(job_id, status="backing_up")
    try:
        backup = BackupEngine(pp)
        backup_meta = backup.snapshot(reason=f"pre-runtime-job {job_id}")
        job_store.append_log(job_id, "info", f"backup created: {backup_meta.get('id')}")
    except Exception as e:  # noqa: BLE001
        _finish(job_id, "failed", error=f"backup failed: {e}")
        return

    def _rollback(reason: str):
        try:
            backup.rollback(backup_meta["id"])
            job_store.append_log(job_id, "warn", f"rolled back ({reason})")
        except Exception as e:  # noqa: BLE001
            job_store.append_log(job_id, "error", f"rollback error: {e}")

    # 3) BRANCH
    branch = None
    if git_write.is_repo(pp):
        job_store.update_job(job_id, status="branching")
        try:
            git_write.fetch(pp)
            base = git_write.resolve_base_branch(pp, job["base_branch"])
            job_store.append_log(job_id, "info", f"base branch resolved: {base}")
            branch = git_write.create_work_branch(pp, job.get("task_id"), job["goal"] or "", base)
            job_store.append_log(job_id, "info", f"work branch: {branch}")
        except git_write.GitWriteError as e:
            _finish(job_id, "failed", error=f"branch failed: {e}")
            return

    # 4) EDIT
    job_store.update_job(job_id, status="editing")
    try:
        changed = _apply_files(pp, plan["files"])
        job_store.update_job(job_id, changed_files=changed)
        job_store.append_log(job_id, "info", f"applied {len(changed)} file operation(s)")
    except Exception as e:  # noqa: BLE001
        _rollback("edit error")
        _finish(job_id, "failed", error=f"edit failed: {e}")
        return

    # 5) VALIDATE / TEST (bounded repair)
    job_store.update_job(job_id, status="testing")
    tests = _run_tests(pp, plan.get("test_commands") or [])
    attempt = 0
    # When a fallback plan is in use the provider planner is known-broken —
    # re-invoking it for a repair attempt would just fail/time out again, so skip
    # the planner-based repair loop entirely (never retry a broken planner).
    while not tests["ok"] and attempt < _MAX_REPAIRS and not fallback_used:
        attempt += 1
        job_store.append_log(job_id, "warn", f"tests failed — repair attempt {attempt}")
        try:
            fails = "\n".join(r["output"][-500:] for r in tests["results"] if not r["passed"])
            repair = ai_planner.plan(job["goal"] or "", (job["instructions"] or "") +
                                     f"\n\nThe previous attempt FAILED tests:\n{fails}\nFix it.",
                                     pp, job.get("allowed_paths") or [], heartbeat_cb=_heartbeat)
            repair_changed = _apply_files(pp, repair["files"])
            by_path = {c["path"]: c for c in changed}
            by_path.update({c["path"]: c for c in repair_changed})
            changed = list(by_path.values())
            job_store.update_job(job_id, changed_files=changed)
            job_store.append_log(job_id, "info", f"repair applied {len(repair_changed)} file operation(s)")
        except Exception as e:  # noqa: BLE001
            job_store.append_log(job_id, "error", f"repair failed: {e}")
            break
        tests = _run_tests(pp, plan.get("test_commands") or [])
    job_store.update_job(job_id, tests=tests)
    if not tests["ok"] and (plan.get("test_commands")):
        _rollback("tests failed")
        _finish(job_id, "failed", error="tests failed after repair attempts")
        return
    job_store.append_log(job_id, "info", "tests passed" if tests["ok"] else "no tests specified")

    # 6) COMMIT
    git_info = {"branch": branch}
    if branch and job.get("auto_commit", True):
        job_store.update_job(job_id, status="committing")
        try:
            git_write.add_paths(pp, [c["path"] for c in changed if c["operation"] != "delete"] +
                                [c["path"] for c in changed if c["operation"] == "delete"])
            secrets = git_write.scan_staged_for_secrets(pp)
            if secrets:
                _rollback("secret in staged diff")
                _finish(job_id, "failed", error=f"aborted: {secrets}")
                return
            tcount = sum(1 for r in tests.get("results", []) if r["passed"])
            msg = (f"feat(runtime): {plan.get('summary','autonomous change')}\n\n"
                   f"Task: OWNER-{job.get('task_id')}\nRuntime job: {job_id}\nTests: {tcount} passed")
            commit_hash = git_write.commit(pp, msg)
            git_info.update({"commit": commit_hash, "remote": git_write.remote_url(pp)})
            job_store.append_log(job_id, "info", f"committed {commit_hash} on {branch}")
        except git_write.GitWriteError as e:
            _rollback("commit error")
            _finish(job_id, "failed", error=f"commit failed: {e}")
            return

    # 7) PUSH (policy)
    if branch and job.get("auto_push") and _idx(job["autonomy_level"]) >= _idx("execute_safe"):
        job_store.update_job(job_id, status="pushing")
        try:
            res = git_write.push(pp, branch)
            git_info["pushed"] = True
            job_store.append_log(job_id, "info", f"pushed {branch}")
        except git_write.GitWriteError as e:
            git_info["pushed"] = False
            git_info["push_error"] = str(e)[:300]
            job_store.append_log(job_id, "warn", f"push failed: {e}")
    else:
        git_info["pushed"] = False

    job_store.update_job(job_id, git_info=git_info)
    _finish(job_id, "completed")
    job_store.append_log(job_id, "info", "job completed")


def execute_async(job_id: str) -> None:
    threading.Thread(target=execute, args=(job_id,), daemon=True).start()
