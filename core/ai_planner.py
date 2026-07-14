"""PHASE 13 — real AI planning via the host Claude CLI.

Produces a validated structured plan (files + test commands). No shell=True.
If the CLI is unavailable, returns provider_not_configured — never fakes a plan.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

_CLAUDE = os.getenv("RUNTIME_CLAUDE_BIN", shutil.which("claude") or "/root/.local/bin/claude")
_MODEL = os.getenv("RUNTIME_CLAUDE_MODEL", "")
# Root cause of the 180s/900s planner hangs (issue #10): the planner invoked the
# full agentic `claude -p` CLI with the default toolset AND inherited the operator's
# $HOME/.claude user settings (output styles, permission overrides). Given the
# free-form instructions text, the model would attempt real tool use (explore the
# repo, chase multi-step "do this for real" phrasing) instead of just emitting a
# JSON plan, and burn the entire timeout without ever returning. Fix is `--tools ""`
# (the planner never needs tool access — it only emits text) plus an isolated
# settings/MCP scope so the operator's live session state can't leak into the
# subprocess. Timeout itself was never the bug; raising it only delayed the hang.
_TIMEOUT = int(os.getenv("RUNTIME_PLAN_TIMEOUT", "180"))
_HEARTBEAT_SECS = int(os.getenv("RUNTIME_PLAN_HEARTBEAT_SECS", "20"))
_MAX_OUTPUT_BYTES = int(os.getenv("RUNTIME_PLAN_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024)))

_ALLOWED_OPS = {"create", "replace", "patch", "delete", "mkdir"}
_SECRET_RE = re.compile(r"(^\.env)|(\.pem$)|(\.key$)|(id_rsa)|(secret)|(credential)|(token)", re.IGNORECASE)

_AUTH_PAT = re.compile(r"not authenticated|please (log|sign) in|/login|invalid api key|unauthorized|\b401\b", re.I)
_LIMIT_PAT = re.compile(r"rate.?limit|usage limit|quota exceeded|\b429\b|too many requests", re.I)
_SETUP_PAT = re.compile(r"first.?time setup|select (a |your )?theme|choose (a |your )?login method|onboarding", re.I)
_INTERACTIVE_PAT = re.compile(r"\(y/n\)|press enter|interactive terminal|tty required", re.I)


class PlannerError(RuntimeError):
    pass


def available() -> bool:
    return bool(_CLAUDE and os.path.exists(_CLAUDE))


def _build_prompt(goal: str, instructions: str, project_path: str,
                  allowed_paths: list, file_listing: str) -> str:
    ap = ", ".join(allowed_paths) if allowed_paths else "(any path inside the project root)"
    return f"""You are a senior engineer working through an automated runtime. Produce a concrete
implementation plan for this task. Output ONLY a single JSON object, no prose, no code fences.

TASK GOAL: {goal}
INSTRUCTIONS: {instructions}
PROJECT ROOT: {project_path}
ALLOWED PATHS (relative, must stay inside these): {ap}
EXISTING FILES (sample):
{file_listing}

Return JSON with this exact shape:
{{
  "summary": "one line",
  "risk_level": "low|medium|high",
  "files": [
    {{"path": "relative/path.py", "operation": "create|replace|delete|mkdir", "content": "FULL file content for create/replace"}}
  ],
  "test_commands": ["python3 -m pytest -q"],
  "expected_result": "what should be true after"
}}

Rules: use relative paths only; never touch .env, keys, secrets, credentials, or paths with '..'.
Prefer small, isolated, well-tested changes. For create/replace include the COMPLETE file content."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    # strip code fences if any
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise PlannerError("model did not return JSON")
    return json.loads(m.group(0))


def _validate(plan: dict, allowed_paths: list) -> dict:
    if not isinstance(plan, dict) or "files" not in plan or not isinstance(plan["files"], list):
        raise PlannerError("plan missing 'files' list")
    if not plan["files"]:
        raise PlannerError("plan has no file operations")
    for f in plan["files"]:
        path = (f.get("path") or "").strip()
        op = f.get("operation")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise PlannerError(f"invalid path: {path!r}")
        if op not in _ALLOWED_OPS:
            raise PlannerError(f"unsupported operation: {op!r}")
        base = os.path.basename(path)
        if _SECRET_RE.search(base) or _SECRET_RE.search(path):
            raise PlannerError(f"plan touches a secret-like path: {path}")
        if allowed_paths and not any(path == a or path.startswith(a.rstrip("/") + "/") for a in allowed_paths):
            raise PlannerError(f"path outside allow-list: {path}")
        if op in ("create", "replace") and not (f.get("content") or "").strip():
            raise PlannerError(f"empty content for {op} {path}")
    plan.setdefault("test_commands", [])
    plan.setdefault("risk_level", "medium")
    plan.setdefault("summary", "")
    return plan


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate the whole process group (the CLI may spawn its own children),
    not just the direct child that subprocess would otherwise leave orphaned).
    proc was started with start_new_session=True, so it is always the process
    group leader and pgid == proc.pid by construction — do NOT look this up via
    os.getpgid(proc.pid): once proc has already been wait()'d (reaped), that pid
    no longer refers to a live process and the lookup raises ProcessLookupError,
    silently skipping cleanup of any grandchildren it left behind."""
    pgid = proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _drain(pipe, cap: int) -> str:
    """Read a subprocess pipe to completion but keep at most `cap` bytes in
    memory, so a runaway/oversized response can't exhaust RAM. Must run on its
    own thread — reading stdout and stderr sequentially on one thread can
    deadlock once either pipe's OS buffer fills."""
    buf = bytearray()
    truncated = False
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            if len(buf) < cap:
                room = cap - len(buf)
                buf.extend(chunk[:room])
                truncated = truncated or len(chunk) > room
            else:
                truncated = True
    except (ValueError, OSError):
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass
    text = buf.decode("utf-8", "replace")
    return text + "\n...[output truncated]" if truncated else text


def _feed_stdin(proc: subprocess.Popen, data: str) -> None:
    """Write the prompt on its own thread so a large prompt can't deadlock the
    caller against a full pipe buffer while the child hasn't started reading yet."""
    try:
        proc.stdin.write(data.encode("utf-8"))
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass


def _classify_failure(stdout: str, stderr: str, envelope: dict | None) -> str:
    """Turn a failed provider invocation into a short, specific, non-secret-leaking
    error class instead of a raw dump — so callers can react (retry vs. alert vs.
    block-until-fixed) without grepping free text."""
    if envelope is not None:
        api_status = envelope.get("api_error_status")
        if api_status in (401, 403):
            return "provider_auth_required"
        if api_status == 429:
            return "provider_limit_exceeded"
        if envelope.get("permission_denials"):
            return "provider_permission_denied"
        subtype = envelope.get("subtype")
        if subtype and subtype != "success":
            return f"provider_error:{subtype}"
    blob = f"{stderr}\n{stdout}"
    if _AUTH_PAT.search(blob):
        return "provider_auth_required"
    if _LIMIT_PAT.search(blob):
        return "provider_limit_exceeded"
    if _SETUP_PAT.search(blob):
        return "provider_setup_required"
    if _INTERACTIVE_PAT.search(blob):
        return "provider_interactive_prompt_detected"
    return f"claude cli error: {blob.strip()[:300]}"


def plan(goal: str, instructions: str, project_path: str, allowed_paths: list,
        timeout: int | None = None, heartbeat_cb=None) -> dict:
    """heartbeat_cb(elapsed_seconds), if given, is called roughly every
    RUNTIME_PLAN_HEARTBEAT_SECS while the provider call is still running, so
    long-running plans surface progress instead of going silent until they
    finish or hit the timeout."""
    if not available():
        raise PlannerError("provider_not_configured")
    listing = ""
    try:
        for root, _dirs, files in os.walk(project_path):
            if any(seg in root for seg in (".git", "__pycache__", "node_modules", ".venv")):
                continue
            for fn in files[:50]:
                rel = os.path.relpath(os.path.join(root, fn), project_path)
                listing += rel + "\n"
                if listing.count("\n") > 80:
                    break
            if listing.count("\n") > 80:
                break
    except Exception:
        listing = "(unavailable)"
    prompt = _build_prompt(goal, instructions, project_path, allowed_paths, listing)
    cmd = [_CLAUDE, "-p"]
    if _MODEL:
        cmd += ["--model", _MODEL]
    # The planner only ever needs to emit text — it must never act. `--tools ""`
    # is what actually stops the CLI going agentic on task-shaped instructions.
    # `--setting-sources ""` and `--strict-mcp-config` stop the operator's live
    # $HOME/.claude session state (output styles, permission overrides, MCP
    # servers) from leaking into and corrupting this "stateless" subprocess call.
    cmd += ["--tools", "", "--setting-sources", "", "--strict-mcp-config", "--output-format", "json"]
    effective_timeout = _TIMEOUT if timeout is None else timeout

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=False, start_new_session=True)
    except FileNotFoundError:
        raise PlannerError("provider_not_configured")

    out_holder: dict = {}
    err_holder: dict = {}
    t_in = threading.Thread(target=_feed_stdin, args=(proc, prompt), daemon=True)
    t_out = threading.Thread(target=lambda: out_holder.__setitem__("v", _drain(proc.stdout, _MAX_OUTPUT_BYTES)), daemon=True)
    t_err = threading.Thread(target=lambda: err_holder.__setitem__("v", _drain(proc.stderr, _MAX_OUTPUT_BYTES)), daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    start = time.monotonic()
    timed_out = False
    while True:
        elapsed = time.monotonic() - start
        remaining = effective_timeout - elapsed
        if remaining <= 0:
            timed_out = True
            _kill_process_group(proc)
            break
        try:
            proc.wait(timeout=min(_HEARTBEAT_SECS, remaining))
            # Reap the whole process group immediately on exit, not just on
            # timeout: the CLI could exit cleanly while leaving a detached
            # grandchild running, and that must not survive the call either
            # (no leaked child processes). Done right after wait() returns,
            # before anything else can block, to minimize the window in
            # which the OS could recycle this pid for an unrelated process.
            _kill_process_group(proc)
            break
        except subprocess.TimeoutExpired:
            if heartbeat_cb:
                try:
                    heartbeat_cb(time.monotonic() - start)
                except Exception:  # noqa: BLE001
                    pass
            continue

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    stdout = out_holder.get("v", "")
    stderr = err_holder.get("v", "")

    if timed_out:
        raise PlannerError("planner timed out")

    try:
        envelope = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        envelope = None

    if proc.returncode != 0 or (envelope is not None and envelope.get("is_error")):
        raise PlannerError(_classify_failure(stdout, stderr, envelope))
    if not stdout.strip():
        raise PlannerError("planner produced empty output")

    plan_text = envelope["result"] if envelope is not None and "result" in envelope else stdout
    plan_obj = _extract_json(plan_text)
    return _validate(plan_obj, allowed_paths)
