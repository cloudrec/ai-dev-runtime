"""PHASE 13 — job executor: plan -> backup -> branch -> edit -> test -> commit -> push.

Reuses the existing engines (file_engine, backup_engine). Git via git_write.
No shell=True. Autonomy-gated. Runs in a background thread.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import threading

from core import ai_planner, git_write, job_kinds, job_store, job_validation
from core import job_workspace
from core import model_router
from core import policy_engine
from core.backup_engine import BackupEngine
from core.file_engine import FileEngine

# Owner OS Operating Constitution enforcement. ON by default: a runtime job is exactly
# the path that must not depend on an agent having read the rules. Set to 0 only to
# diagnose the policy layer itself — the audit records which mode a decision ran in.
POLICY_ENFORCE = os.getenv("OWNER_OS_POLICY_ENFORCE", "1") not in ("0", "false", "no", "")

AUTONOMY_ORDER = ["observe", "suggest", "prepare", "execute_safe", "execute_full", "deploy"]
_MAX_REPAIRS = int(os.getenv("RUNTIME_MAX_REPAIRS", "1"))
_TEST_TIMEOUT = int(os.getenv("RUNTIME_TEST_TIMEOUT", "300"))
_BACKUP_TIMEOUT = int(os.getenv("RUNTIME_BACKUP_TIMEOUT", "120"))
_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked", "rolled_back",
                      "fallback_plan_only"}
# goals/instructions that describe read-only inventory/report/audit work -> a
# full migration-grade archive is wasteful and risky; take a light snapshot.
_READ_ONLY_RE = re.compile(
    r"\b(inventory|report|reconcile|audit|inspect|read[-\s]?only|survey|summar|list|analy[sz]e|review)\b", re.I)
# comfortably under job_store._HEARTBEAT_STALE_SECS (20s) so a live job never
# looks orphaned to recover_interrupted(), even during a long silent test run.
_HEARTBEAT_INTERVAL_SECS = int(os.getenv("RUNTIME_HEARTBEAT_INTERVAL_SECS", "5"))
# Isolated per-job worktrees (core.job_workspace). ON by default: checking out a
# work branch inside the live project tree is the defect that failed
# OWNER-151/180/182/192/193/200 on owner dirty files. Read at call time so the
# toggle works without a reload.
def _isolated_workspaces() -> bool:
    return os.getenv("RUNTIME_ISOLATED_WORKSPACES", "1") not in ("0", "", "false", "no")


# ── task-209 model router wiring ─────────────────────────────────────────────
# Dispatches every planner call (initial plan + each bounded repair attempt)
# through core.model_router instead of the single static RUNTIME_CLAUDE_MODEL.
# ON by default; read at call time like the other env toggles in this file.
# The router itself never blocks a job: any failure here falls back to
# ai_planner's own default (RUNTIME_CLAUDE_MODEL / provider default).
def _router_enabled() -> bool:
    return os.getenv("RUNTIME_MODEL_ROUTER", "1") not in ("0", "", "false", "no")


# job kind -> model_router task_class. Kinds not listed here fall through to
# the raw kind string (route() then applies its own unknown-class default:
# sonnet, non-strict — fail toward cheap, never toward expensive).
_KIND_TASK_CLASS = {
    job_kinds.CODE_CHANGE: "routine_implementation",
    job_kinds.OPERATIONAL: "routine_implementation",
    job_kinds.CONTENT_PRODUCTION: "docs",
    job_kinds.DEPLOYMENT: "release_design",
    job_kinds.DATA_HANDOFF: "context_pack",
    job_kinds.CONTEXT_RESTORE: "context_pack",
}


def _task_class_for_kind(kind: str | None) -> str:
    return _KIND_TASK_CLASS.get(kind, "")


_MONEY_RE = re.compile(r"(payment|billing|invoice|refund|wallet|payout|money)", re.I)
_SECURITY_RE = re.compile(r"(secret|credential|password|token|api[_ -]?key|security|auth)", re.I)


def _risk_for_job(job: dict) -> str:
    """money wins over security if both fire; job.risk_level high/critical always
    floors to "high" regardless of goal/instructions text."""
    if (job.get("risk_level") or "") in ("high", "critical"):
        return "high"
    text = f"{job.get('goal') or ''} {job.get('instructions') or ''}".lower()
    if _MONEY_RE.search(text):
        return "money"
    if _SECURITY_RE.search(text):
        return "security"
    return "normal"


def _lineage_attempts(job: dict) -> list:
    """Prior FAILED runtime jobs for the same task_id, created before this job,
    as model_router prior_attempts. Bound to the 5 most recent. Never raises —
    lineage inspection must never break dispatch."""
    try:
        task_id = job.get("task_id")
        if not task_id:
            return []
        this_id = job.get("id")
        created_at = job.get("created_at") or ""
        out = []
        # list_jobs() is ordered created_at DESC, so the first matches found are
        # already the most recent — no separate sort needed.
        for j in job_store.list_jobs(limit=200):
            if j.get("id") == this_id or j.get("task_id") != task_id:
                continue
            if j.get("status") != "failed":
                continue
            if created_at and (j.get("created_at") or "") >= created_at:
                continue
            model_name = None
            for art in reversed(j.get("artifacts") or []):
                if isinstance(art, dict) and isinstance(art.get("model_selection"), dict):
                    model_name = art["model_selection"].get("model")
                    break
            if not model_name:
                continue
            out.append({"model": model_name, "outcome": "failure"})
            if len(out) >= 5:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _route_model(job: dict, kind: str | None, *, extra_attempts=(), stage: str = "plan"):
    """Decide + record a model for one planner call. Returns
    (model_id_or_None, decision_id_or_None, model_name_or_None). Disabled router
    or ANY failure here returns (None, None, None) — the caller then passes
    model=None into ai_planner.plan(), which falls back to its own configured
    default. Router availability must never fail or block a job.

    `job["escalation_reason"]`, if set, is the ONLY way an opus/fable-eligible
    class actually dispatches at that tier (task 213 hard gate in
    core.model_router) — ordinary/automated dispatch never sets it, so
    routine/load/perf/docs/repo-inspection/concrete-fix jobs always land on
    sonnet even if their task_class or risk floor would otherwise reach for
    a pricier model.

    `job["requested_model"]` (task 220) is the owner/caller naming a tier for
    THIS job. Both fields are columns on the jobs table, so they survive the
    round-trip through the store: before task 220 they were in-memory only,
    which is why an explicitly-requested opus job still dispatched on sonnet
    once the executor re-read the job (owner_task #220 / runtime job #81)."""
    if not _router_enabled():
        return (None, None, None)
    job_id = job.get("id")
    try:
        task_class = _task_class_for_kind(kind) or (kind or "unknown")
        risk = _risk_for_job(job)
        attempts = list(_lineage_attempts(job)) + list(extra_attempts)
        escalation_reason = job.get("escalation_reason")
        decision = model_router.route(
            task_class, risk=risk, prior_attempts=attempts,
            context_pack=f"planner-prompt:{stage} (goal+instructions+file-listing<=80 lines)",
            task_ref=f"runtimejob:{job_id}:{stage}",
            escalation_reason=escalation_reason if isinstance(escalation_reason, dict) else None,
            explicit_model=(job.get("requested_model") or None))
        cur = job_store.get_job(job_id) or job
        job_store.update_job(job_id, artifacts=(cur.get("artifacts") or []) + [{
            "model_selection": {
                "stage": stage, "decision_id": decision["decision_id"],
                "model": decision["model"], "model_id": decision["model_id"],
                "reason": decision["reason"], "task_class": task_class, "risk": risk,
                "requested_model": decision.get("requested_model"),
                "explicit_model": decision.get("explicit_model"),
                "explicit_granted": decision.get("explicit_granted"),
                "escalation_valid": decision.get("escalation_valid"),
            },
        }])
        job_store.append_log(job_id, "info",
                             f"model routing ({stage}): {task_class}/{risk} -> "
                             f"{decision['model']} [decision {decision['decision_id']}] "
                             f"{decision['reason'][:120]}")
        return (decision["model_id"], decision["decision_id"], decision["model"])
    except Exception:  # noqa: BLE001 — routing must never fail or block a job
        try:
            job_store.append_log(job_id, "warn",
                                 "model routing unavailable — planner uses configured default")
        except Exception:  # noqa: BLE001
            pass
        return (None, None, None)


def _idx(level: str) -> int:
    return AUTONOMY_ORDER.index(level) if level in AUTONOMY_ORDER else 0


def _is_read_only_plan(job: dict, plan: dict) -> bool:
    """A task is read-only enough for a light snapshot when the plan does not
    modify or delete any existing file (all ops are create/mkdir) OR the goal /
    instructions describe inventory/report/audit-style work. Conservative: any
    replace/patch/delete of an existing path forces a full snapshot."""
    files = (plan or {}).get("files") or []
    for f in files:
        if f.get("operation") in ("replace", "patch", "delete"):
            return False
    if files and all(f.get("operation") in ("create", "mkdir") for f in files):
        return True
    text = f"{job.get('goal') or ''} {job.get('instructions') or ''}"
    return bool(_READ_ONLY_RE.search(text))


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except FileNotFoundError:
        return "absent"


def _kill_step_group(proc: subprocess.Popen) -> None:
    """Reap the step's whole process group. `proc` was started with
    start_new_session=True, so it is the group leader and pgid == proc.pid by
    construction — never look it up via os.getpgid(), which raises once proc has
    been reaped and would silently skip cleanup. Same discipline as
    ai_planner._kill_process_group."""
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


def _run_step(project_path: str, step: str) -> tuple[bool, str]:
    parts = shlex.split(step)
    if not parts:
        return True, ""
    # start_new_session makes the step its own process-group leader so a timeout
    # can reap everything it spawned. subprocess.run(timeout=) kills ONLY the
    # direct child: a `pytest` killed at RUNTIME_TEST_TIMEOUT left every process
    # its tests had spawned running on the server indefinitely. That is not
    # hypothetical here — this repo's own suite spawns long-lived CLI stubs, and
    # 7 recorded jobs (tasks 162/182/193/220/221) hit that cap on the full suite.
    proc = subprocess.Popen(parts, cwd=project_path, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, shell=False,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_step_group(proc)
        # Drain whatever the step wrote before it died, then re-raise unchanged:
        # _run_tests records str(e), and the existing
        # "Command '[...]' timed out after N seconds" text is what operators and
        # the stored `tests` blobs already carry.
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 — draining must never mask the timeout
            pass
        raise
    return proc.returncode == 0, (out + err)


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


def _remove_created_paths(project_path: str, changed: list) -> list[str]:
    """Delete files a failed job created, so its debris cannot leak into the
    shared workspace (and into the next job's test run). Never touches a path
    that already existed before the job: those are restored from the backup
    archive instead. `changed` entries record `before == 'absent'` exactly when
    the job created the path.
    """
    removed: list[str] = []
    for c in changed or []:
        if c.get("before") != "absent" or c.get("operation") not in ("create", "patch", "replace"):
            continue
        rel = c.get("path")
        if not rel:
            continue
        target = os.path.realpath(os.path.join(project_path, rel))
        # never escape the project directory
        if not target.startswith(os.path.realpath(project_path) + os.sep):
            continue
        try:
            if os.path.isfile(target):
                os.remove(target)
                removed.append(rel)
        except OSError:
            pass
    return removed


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


def _job_action(job: dict) -> str:
    """The action text the policy classifies: what the job says it will do."""
    return f"{job.get('goal') or ''} {job.get('instructions') or ''}".strip()


def _rollback_evidence(job: dict) -> dict:
    """The rollback reference this job actually recorded, or nothing.

    Reads the job's own artifacts rather than assuming a backup happened: an unrecorded
    backup is, for policy purposes, not a backup (R1.7).
    """
    for art in reversed(job.get("artifacts") or []):
        if isinstance(art, dict) and art.get("rollback"):
            return art["rollback"]
    return {}


def _completion_evidence(job: dict) -> dict:
    """Structured evidence assembled from what the pipeline actually recorded."""
    git_info = job.get("git_info") or {}
    tests = job.get("tests") or {}
    ev = {
        "rollback": _rollback_evidence(job),
        "baseline": {"head": git_info.get("commit") or git_info.get("base") or "",
                     "branch": git_info.get("branch") or "",
                     "clean_tree": True},
        "changed_files": job.get("changed_files") if job.get("changed_files") is not None else [],
        "summary": (job.get("goal") or "")[:200],
    }
    if tests or job.get("validation"):
        ev["tests"] = {"ok": bool(tests.get("ok", (job.get("validation") or {}).get("ok")))}
    if job.get("artifacts"):
        ev["artifacts"] = job["artifacts"]
    return ev


def _finish(job_id: str, status: str, **extra):
    # Every terminal state carries an explicit outcome: a job that ends without
    # one is exactly the ambiguity this model removes. Failure paths default to
    # the `failed` outcome unless the caller states a more specific one.
    if status == "failed":
        extra.setdefault("outcome", job_kinds.FAILED)
    # Fail-closed truthfulness gate: a plan-only outcome may not be recorded as
    # `completed`, no matter what the caller passed. Jobs 59/60/61 reported a
    # fallback PLAN as a completed implementation; this is the one chokepoint
    # every terminal transition goes through, so that cannot recur.
    status = job_kinds.terminal_status_for(extra.get("outcome"), status)
    # Constitution completion gate (R4): a `completed` claim must be backed by the
    # structured evidence its risk class demands. Without it the job is recorded as
    # UNVERIFIED — honest about what is known — instead of green. Non-success terminal
    # states are not gated: a failure needs no evidence to be believed.
    if POLICY_ENFORCE and status == job_kinds.STATUS_COMPLETED:
        cur = job_store.get_job(job_id) or {}
        merged = {**cur, **{k: v for k, v in extra.items() if k in
                            ("changed_files", "tests", "validation", "git_info", "artifacts")}}
        try:
            gate = policy_engine.completion_gate(
                action=_job_action(merged), evidence=_completion_evidence(merged),
                actor="runtime_job", project=merged.get("project_path") or "",
                task_id=job_id, claimed_status="completed")
        except policy_engine.PolicyError as e:      # unreadable policy ⇒ stop, never pass
            gate = {"allowed": False, "status": "blocked", "reason": f"policy unavailable: {e}",
                    "missing_evidence": [], "violated_rules": ["R8.1-fail-closed"]}
        if not gate["allowed"]:
            status = "blocked"
            extra["error"] = (f"completion gate: {gate['reason']}"[:400]
                              if gate.get("reason") else "completion gate refused DONE")
            try:
                job_store.append_log(job_id, "warn",
                                     f"completion gate refused `completed` — missing "
                                     f"{gate.get('missing_evidence')}; recorded as {status}")
            except Exception:  # noqa: BLE001 — logging must never undo the gate's decision
                pass
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
    except BaseException as e:  # noqa: BLE001
        # Any unhandled crash in a pipeline stage (e.g. a backup worker dying)
        # must NOT leave the job frozen in a non-terminal status forever with no
        # error — that is exactly how a job gets stuck in `backing_up`. Record a
        # precise terminal failure unless the pipeline already reached a terminal
        # state itself.
        try:
            cur = job_store.get_job(job_id)
            if cur and cur["status"] not in _TERMINAL_STATUSES:
                stage = cur["status"]
                job_store.append_log(job_id, "error", f"worker crashed during '{stage}': {str(e)[:300]}")
                _finish(job_id, "failed", error=f"worker crashed during '{stage}': {str(e)[:300]}")
        except Exception:  # noqa: BLE001
            pass
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=2)


def _first_failure(validation: dict) -> str:
    for c in validation.get("results", []):
        if not c.get("passed"):
            return f"{c.get('check')} ({c.get('detail', '')[:160]})"
    return "unknown validation failure"


def _run_pipeline(job_id: str, job: dict, pp: str) -> None:
    # 0) KIND — decides how this job is validated and what a success means.
    kind = job.get("kind") or job_kinds.classify(job.get("goal") or "", job.get("instructions") or "")
    if job.get("kind") != kind:
        job_store.update_job(job_id, kind=kind)
    job_store.append_log(job_id, "info",
                         f"job kind: {kind} — validated by {job_kinds.validation_kind_for(kind)}")

    # 1) PLAN
    job_store.update_job(job_id, status="planning")
    job_store.append_log(job_id, "info", "planning via AI provider")
    def _heartbeat(elapsed: float) -> None:
        job_store.append_log(job_id, "info", f"planning… still running ({int(elapsed)}s elapsed)")

    fallback_used = False
    m_id, m_dec, m_name = _route_model(job, kind)
    try:
        plan = ai_planner.plan(job["goal"] or "", job["instructions"] or "", pp, job.get("allowed_paths") or [],
                               heartbeat_cb=_heartbeat, kind=kind, model=m_id)
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
        if m_dec:
            try:
                _tok = diag.get("tokens")
                model_router.record_outcome(
                    m_dec, outcome="failure",
                    tokens_in=(_tok.get("input_tokens") or 0) if isinstance(_tok, dict) else 0,
                    tokens_out=(_tok.get("output_tokens") or 0) if isinstance(_tok, dict) else 0,
                    note=reason[:200])
            except Exception:  # noqa: BLE001 — routing outcome recording must never block a job
                pass
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
        # Re-fetch: the in-memory `job` predates the plan stage, and appending from
        # it silently drops artifacts recorded since (the model_selection artifact
        # was lost exactly this way on job b34772f4, 2026-08-15).
        _cur = job_store.get_job(job_id) or job
        job_store.update_job(job_id, artifacts=(_cur.get("artifacts") or []) + [{
            "fallback_planning": True, "reason": reason[:200],
            "timed_out": diag["timed_out"], "tokens": diag["tokens"],
            "cost_usd": diag["cost_usd"], "duration_ms": diag["duration_ms"],
        }])
        job_store.append_log(job_id, "info",
                             "fallback plan generated — continuing to execution (no planner retry)")
    else:
        if m_dec:
            try:
                model_router.record_outcome(m_dec, outcome="success", note="plan produced")
            except Exception:  # noqa: BLE001 — routing outcome recording must never block a job
                pass
    job_store.update_job(job_id, plan=plan, risk_level=plan.get("risk_level", job["risk_level"]))
    if fallback_used:
        job_store.append_log(job_id, "info", f"FALLBACK PLAN in use ({len(plan['files'])} safe file op)")
    if plan.get("salvaged_after_timeout"):
        # The provider blew its deadline but had ALREADY written a complete,
        # valid plan, so the plan was used instead of being discarded into a
        # fallback. Recorded explicitly: without it this is indistinguishable
        # from an ordinary slow plan, and the only way to answer "did salvage
        # fire?" was inferring it from heartbeat timing.
        job_store.append_log(job_id, "warn",
                             "planner exceeded its deadline but had already delivered a "
                             "complete plan — SALVAGED, no fallback used")
        _cur = job_store.get_job(job_id) or job
        job_store.update_job(job_id, artifacts=(_cur.get("artifacts") or []) + [{
            "planner_salvaged_after_timeout": True,
            "tokens": plan.get("planner_tokens"),
            "cost_usd": plan.get("planner_cost_usd"),
            "duration_ms": plan.get("planner_duration_ms"),
        }])
    job_store.append_log(job_id, "info", f"plan: {plan.get('summary','')[:120]} ({len(plan['files'])} file ops)")

    # observe/suggest -> plan only, stop here. A plan is not an implementation,
    # so this reports `fallback_plan_only` rather than a success outcome.
    if _idx(job["autonomy_level"]) <= _idx("suggest"):
        _finish(job_id, job_kinds.STATUS_FALLBACK_PLAN_ONLY, outcome=job_kinds.FALLBACK_PLAN_ONLY)
        job_store.append_log(job_id, "info", "plan-only (autonomy suggest/observe) — no changes applied")
        return

    # 2) BACKUP — bounded, progress-emitting, atomic; a read-only inventory/report
    # task takes the smallest safe (light) snapshot rather than a full archive.
    job_store.update_job(job_id, status="backing_up")
    light = _is_read_only_plan(job, plan)

    def _backup_progress(p: dict) -> None:
        # keep the job demonstrably alive AND record measurable progress so it can
        # never look silently frozen in `backing_up`
        job_store.touch_heartbeat(job_id)
        job_store.append_log(job_id, "info",
                             f"backing up… {p.get('files', 0)} files, "
                             f"{p.get('bytes', 0)} bytes ({p.get('elapsed', 0)}s)")

    try:
        backup = BackupEngine(pp)
        backup_meta = backup.snapshot(reason=f"pre-runtime-job {job_id}",
                                      timeout=_BACKUP_TIMEOUT, progress_cb=_backup_progress, light=light)
        job_store.append_log(job_id, "info",
                             f"backup created: {backup_meta.get('id')} "
                             f"({'light' if light else 'full'}, {backup_meta.get('files')} files, "
                             f"{backup_meta.get('size')} bytes)")
    except Exception as e:  # noqa: BLE001 — BackupError or any tar/os failure -> terminal, precise
        _finish(job_id, "failed", error=f"backup failed: {str(e)[:400]}")
        return

    # 2b) CONSTITUTION PREFLIGHT (R1/R2/R3/R7) — evaluated with the rollback path in
    # hand and BEFORE the first edit, so a prohibited, owner-gated, out-of-scope or
    # duplicated job stops without having touched the workspace.
    rollback_ref = {"kind": "file_backup", "ref": str(backup_meta.get("id") or ""),
                    "verified": bool(backup_meta.get("id"))}
    # Append to the artifacts as they stand NOW, not to the snapshot taken when the
    # pipeline started: earlier stages (e.g. fallback planning) record artifacts too, and
    # writing back a stale list would silently drop them.
    _cur = job_store.get_job(job_id) or job
    job_store.update_job(job_id, artifacts=(_cur.get("artifacts") or []) +
                         [{"backup_id": backup_meta.get("id"), "rollback": rollback_ref}])
    if POLICY_ENFORCE:
        try:
            gate = policy_engine.preflight(
                action=_job_action(job), actor="runtime_job", project=pp, task_id=job_id,
                scope=(job.get("allowed_paths") or None),
                evidence={"rollback": rollback_ref},
                owner_approved=not job.get("approval_required"))
        except policy_engine.PolicyError as e:      # unreadable policy ⇒ stop, never proceed
            _finish(job_id, "blocked", error=f"policy unavailable: {str(e)[:300]}")
            return
        if not gate["allowed"]:
            job_store.append_log(job_id, "warn",
                                 f"policy {gate['decision']} ({gate['risk_class']}): "
                                 f"{gate['reason']}")
            _finish(job_id, "blocked",
                    error=f"policy {gate['decision']}: {gate['reason']}"[:400])
            return
        job_store.append_log(job_id, "info",
                             f"policy preflight ALLOW (risk {gate['risk_class']}, "
                             f"rollback {rollback_ref['kind']}:{rollback_ref['ref']})")

    # Paths this job brought into existence, tracked so a rollback can undo them.
    created_paths: list[str] = []
    # Set by the branch stage when the job runs in an isolated worktree. Kept in a
    # dict so _rollback (defined before the branch stage runs) sees the final value.
    ws: dict = {"workspace": None}

    def _rollback(reason: str):
        # An isolated job never modified the primary tree, so there is nothing to
        # restore there — restoring the pre-job snapshot over the live tree would
        # actually DESTROY owner edits made while the job ran. Discarding the
        # worktree is the whole rollback.
        if ws["workspace"]:
            job_store.append_log(job_id, "warn",
                                 f"rolled back ({reason}) — isolated worktree discarded, "
                                 f"primary tree untouched")
            return
        try:
            backup.rollback(backup_meta["id"])
            job_store.append_log(job_id, "warn", f"rolled back ({reason})")
        except Exception as e:  # noqa: BLE001
            job_store.append_log(job_id, "error", f"rollback error: {e}")
        # BackupEngine.rollback() restores archived files (tar extract) but cannot
        # remove files that did not exist when the snapshot was taken. Without
        # this sweep a failed job leaves its new, untracked files in the shared
        # workspace forever — exactly how OWNER-113's broken module and its
        # failing test survived its own rollback and then failed the repo-suite
        # gate of every later job. Only paths this job itself created are removed.
        removed = _remove_created_paths(pp, created_paths)
        if removed:
            job_store.append_log(job_id, "warn",
                                 f"workspace hygiene: removed {len(removed)} file(s) created by this "
                                 f"job so they cannot poison later jobs: {removed}")

    # 3) BRANCH — in an ISOLATED worktree, never a checkout inside the live tree.
    # The direct `checkout -b` in the shared working tree is what failed
    # OWNER-151/180/182/192/193/200 with "Your local changes ... would be
    # overwritten by checkout" whenever the owner had dirty files; the worktree
    # path leaves the primary tree — dirty files included — byte-for-byte alone.
    branch = None
    work_pp = pp
    if git_write.is_repo(pp):
        job_store.update_job(job_id, status="branching")
        try:
            git_write.fetch(pp)
            base = git_write.resolve_base_branch(pp, job["base_branch"])
            job_store.append_log(job_id, "info", f"base branch resolved: {base}")
            if _isolated_workspaces():
                created = job_workspace.create(pp, job_id, job.get("task_id"),
                                               job["goal"] or "", base)
                ws["workspace"] = created
                branch = created["branch"]
                work_pp = created["path"]
                job_store.append_log(job_id, "info",
                                     f"work branch: {branch} (isolated worktree {work_pp})")
            else:
                branch = git_write.create_work_branch(pp, job.get("task_id"), job["goal"] or "", base)
                job_store.append_log(job_id, "info", f"work branch: {branch}")
        except git_write.GitWriteError as e:
            _finish(job_id, "failed", error=f"branch failed: {e}")
            return

    # Stages 4-8 operate on `work_pp`: the isolated worktree when one exists,
    # otherwise the project tree itself (non-repo projects, or isolation off).
    # The worktree is removed on EVERY exit path — its branch and commits
    # survive; only the scratch directory goes.
    try:
        _run_work_stages(job_id, job, pp, work_pp, plan, kind, branch, tests_ctx={
            "fallback_used": fallback_used, "rollback": _rollback,
            "created_paths": created_paths, "heartbeat": _heartbeat, "plan_model_name": m_name})
    finally:
        if ws["workspace"]:
            removed = job_workspace.remove(pp, ws["workspace"]["path"])
            job_store.append_log(job_id, "info",
                                 f"isolated worktree removed: {removed} "
                                 f"({ws['workspace']['path']})")


def _run_work_stages(job_id: str, job: dict, pp: str, work_pp: str, plan: dict,
                     kind: str, branch, tests_ctx: dict) -> None:
    fallback_used = tests_ctx["fallback_used"]
    _rollback = tests_ctx["rollback"]
    created_paths = tests_ctx["created_paths"]
    _heartbeat = tests_ctx["heartbeat"]
    plan_model_name = tests_ctx.get("plan_model_name")

    # 4) EDIT
    job_store.update_job(job_id, status="editing")
    try:
        changed = _apply_files(work_pp, plan["files"])
        created_paths.extend(changed)
        job_store.update_job(job_id, changed_files=changed)
        job_store.append_log(job_id, "info", f"applied {len(changed)} file operation(s)")
    except Exception as e:  # noqa: BLE001
        _rollback("edit error")
        _finish(job_id, "failed", outcome=job_kinds.FAILED, error=f"edit failed: {e}")
        return

    # 5) VALIDATE / TEST (bounded repair)
    #
    # Routing by kind. Only a `code_change` job is gated on the repository test
    # suite; every other kind is validated against evidence of its own work.
    # Gating a content/operational job on repo-wide pytest is what made
    # OWNER-114..120 fail on a defect left behind by OWNER-113.
    job_store.update_job(job_id, status="testing")

    if not job_kinds.requires_repo_tests(kind):
        validation = job_validation.validate(kind, work_pp, plan, changed, run_commands=_run_tests)
        job_store.update_job(job_id, validation=validation, tests={
            "ok": validation["ok"], "results": [], "skipped_repo_suite": True,
            "reason": f"kind={kind} is validated by {validation['validation_kind']}",
        })
        for c in validation["results"]:
            job_store.append_log(job_id, "info" if c["passed"] else "warn",
                                 f"validation {'PASS' if c['passed'] else 'FAIL'}: {c['check']}")
        if validation.get("dropped_repo_suite_commands"):
            job_store.append_log(job_id, "info",
                                 f"repo suite command(s) not used as validation for kind={kind}: "
                                 f"{validation['dropped_repo_suite_commands']}")
        if not validation["ok"]:
            _rollback("task validation failed")
            _finish(job_id, "failed", outcome=job_kinds.FAILED,
                    error=f"task validation failed for kind={kind}: "
                          f"{_first_failure(validation)}")
            return
        job_store.append_log(job_id, "info", f"task validation passed ({validation['validation_kind']})")
        tests = {"ok": True, "results": [], "skipped_repo_suite": True}
    else:
        tests = _run_tests(work_pp, plan.get("test_commands") or [])
        attempt = 0
        # When a fallback plan is in use the provider planner is known-broken —
        # re-invoking it for a repair attempt would just fail/time out again, so skip
        # the planner-based repair loop entirely (never retry a broken planner).
        while not tests["ok"] and attempt < _MAX_REPAIRS and not fallback_used:
            attempt += 1
            job_store.append_log(job_id, "warn", f"tests failed — repair attempt {attempt}")
            # Real escalation ladder: a sonnet plan whose tests fail escalates the
            # repair attempt toward opus (model_router's escalation ladder), via
            # the prior plan-stage outcome fed in as an extra prior_attempt.
            r_id, r_dec, _r_name = _route_model(
                job, kind,
                extra_attempts=([{"model": plan_model_name, "outcome": "failure"}]
                                if plan_model_name else []),
                stage=f"repair{attempt}")
            try:
                fails = "\n".join(r["output"][-500:] for r in tests["results"] if not r["passed"])
                repair = ai_planner.plan(job["goal"] or "", (job["instructions"] or "") +
                                         f"\n\nThe previous attempt FAILED tests:\n{fails}\nFix it.",
                                         work_pp, job.get("allowed_paths") or [], heartbeat_cb=_heartbeat,
                                         model=r_id)
                repair_changed = _apply_files(work_pp, repair["files"])
                created_paths.extend(repair_changed)
                by_path = {c["path"]: c for c in changed}
                by_path.update({c["path"]: c for c in repair_changed})
                changed = list(by_path.values())
                job_store.update_job(job_id, changed_files=changed)
                job_store.append_log(job_id, "info", f"repair applied {len(repair_changed)} file operation(s)")
            except Exception as e:  # noqa: BLE001
                job_store.append_log(job_id, "error", f"repair failed: {e}")
                break
            tests = _run_tests(work_pp, plan.get("test_commands") or [])
            if r_dec:
                try:
                    model_router.record_outcome(
                        r_dec, outcome="success" if tests["ok"] else "failure")
                except Exception:  # noqa: BLE001 — routing outcome recording must never block a job
                    pass
        job_store.update_job(job_id, tests=tests, validation={
            "ok": tests["ok"], "validation_kind": job_kinds.validation_kind_for(kind),
            "repo_suite_used": True,
            "results": [{"check": f"repo suite: {r.get('cmd')}", "passed": bool(r.get("passed")),
                         "detail": (r.get("output") or "")[-500:]} for r in tests.get("results", [])],
        })
        if not tests["ok"] and (plan.get("test_commands")):
            _rollback("tests failed")
            _finish(job_id, "failed", outcome=job_kinds.FAILED,
                    error="tests failed after repair attempts")
            return
        job_store.append_log(job_id, "info", "tests passed" if tests["ok"] else "no tests specified")

    # 6) COMMIT
    #
    # A job with no file changes does not commit. For a non-code kind that is a
    # perfectly normal result — the work happened outside the repository — and
    # fabricating an empty commit purely to look successful is forbidden
    # (job_kinds.allows_empty_commit is False for every kind).
    git_info = {"branch": branch}
    if branch and job.get("auto_commit", True) and not changed:
        job_store.append_log(job_id, "info",
                             f"no file changes to commit (kind={kind}) — not creating a commit; "
                             f"this is not an error for a {kind} job")
    elif branch and job.get("auto_commit", True):
        job_store.update_job(job_id, status="committing")
        try:
            git_write.add_paths(work_pp, [c["path"] for c in changed if c["operation"] != "delete"] +
                                [c["path"] for c in changed if c["operation"] == "delete"])
            secrets = git_write.scan_staged_for_secrets(work_pp)
            if secrets:
                _rollback("secret in staged diff")
                _finish(job_id, "failed", error=f"aborted: {secrets}")
                return
            tcount = sum(1 for r in tests.get("results", []) if r["passed"])
            msg = (f"feat(runtime): {plan.get('summary','autonomous change')}\n\n"
                   f"Task: OWNER-{job.get('task_id')}\nRuntime job: {job_id}\nTests: {tcount} passed")
            commit_hash = git_write.commit(work_pp, msg)
            git_info.update({"commit": commit_hash, "remote": git_write.remote_url(work_pp),
                             "isolated_workspace": work_pp != pp})
            job_store.append_log(job_id, "info", f"committed {commit_hash} on {branch}")
        except git_write.GitWriteError as e:
            _rollback("commit error")
            _finish(job_id, "failed", error=f"commit failed: {e}")
            return

    # 7) PUSH (policy)
    if branch and job.get("auto_push") and _idx(job["autonomy_level"]) >= _idx("execute_safe"):
        job_store.update_job(job_id, status="pushing")
        try:
            res = git_write.push(work_pp, branch)
            git_info["pushed"] = True
            job_store.append_log(job_id, "info", f"pushed {branch}")
        except git_write.GitWriteError as e:
            git_info["pushed"] = False
            git_info["push_error"] = str(e)[:300]
            job_store.append_log(job_id, "warn", f"push failed: {e}")
    else:
        git_info["pushed"] = False

    job_store.update_job(job_id, git_info=git_info)

    # 8) OUTCOME — what was actually achieved, distinct from lifecycle status.
    # A fallback plan is a document describing work, not the work: it is never
    # reported as a completed implementation and never feeds a release.
    if fallback_used:
        outcome = job_kinds.FALLBACK_PLAN_ONLY
        job_store.append_log(job_id, "warn",
                             "outcome=fallback_plan_only — a PLAN was recorded, the task is NOT "
                             "implemented; requeue it for real implementation")
    else:
        outcome = job_kinds.success_outcome_for(kind)
    # `terminal_status_for` downgrades a plan-only run to its own terminal
    # status; a real implementation keeps `completed`.
    status = job_kinds.terminal_status_for(outcome, job_kinds.STATUS_COMPLETED)
    _finish(job_id, status, outcome=outcome)
    job_store.append_log(job_id, "info", f"job finished (status={status} outcome={outcome})")


def execute_async(job_id: str) -> None:
    threading.Thread(target=execute, args=(job_id,), daemon=True).start()
