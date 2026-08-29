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
    """Carries structured, non-secret diagnostics alongside the message so the
    caller can build a fallback plan (raw sanitized response, token/cost/timing
    when the provider returned an envelope, whether it was a hard timeout)
    without re-parsing free text or losing accounting."""

    def __init__(self, message: str, *, raw: str = "", tokens=None,
                 cost_usd=None, duration_ms=None, timed_out: bool = False):
        super().__init__(message)
        self.reason = message
        self.raw = raw
        self.tokens = tokens
        self.cost_usd = cost_usd
        self.duration_ms = duration_ms
        self.timed_out = timed_out


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


def _first_json_object(text: str) -> dict | None:
    """Return the first *balanced, parseable* top-level JSON object in `text`,
    or None. Brace-matches while respecting string literals/escapes so braces
    inside string values don't throw off the depth count, and tries json.loads
    on each complete top-level {...} — tolerating leading/trailing prose without
    a greedy `\\{.*\\}` grabbing an unparseable span from first '{' to last '}'."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = -1
                        continue
                    if isinstance(obj, dict):
                        return obj
                    start = -1
    return None


def _extract_json(text: str) -> dict:
    """Recover a plan object from a range of provider output shapes:
    bare JSON, JSON in a ```json fence, or JSON embedded in prose. Distinguishes
    'malformed planner JSON' (braces present but nothing parses) from
    'model did not return JSON' (no JSON object at all) so the caller/fallback
    can record the specific failure mode."""
    text = (text or "").strip()
    if not text:
        raise PlannerError("planner produced empty output")
    # 1) the whole payload is a JSON object
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2) fenced code block(s) — try each fence's body first
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE):
        obj = _first_json_object(m.group(1))
        if obj is not None:
            return obj
    # 3) first balanced JSON object anywhere in surrounding prose
    obj = _first_json_object(text)
    if obj is not None:
        return obj
    if "{" in text and "}" in text:
        raise PlannerError("malformed planner JSON")
    raise PlannerError("model did not return JSON")


def _validate(plan: dict, allowed_paths: list, allow_empty_files: bool = False) -> dict:
    if not isinstance(plan, dict) or "files" not in plan or not isinstance(plan["files"], list):
        raise PlannerError("plan missing 'files' list")
    if not plan["files"] and not allow_empty_files:
        # A code change that touches no file is a non-plan. For operational /
        # content / deployment work the opposite is true — the work happens
        # outside the repository — so those kinds pass allow_empty_files=True
        # rather than being forced to invent a file just to produce a valid plan.
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


def _invoke_cli(cmd: list[str], prompt: str, timeout: float, heartbeat_cb=None) -> tuple[str, str, bool, int]:
    """Run the Claude CLI as a stateless, non-agentic subprocess (stdin prompt,
    JSON envelope out) and return (stdout, stderr, timed_out, returncode).
    Shared by plan() and smoke() — same isolation/kill/drain semantics for
    both: `--tools ""` so the CLI can never act, `--setting-sources ""` +
    `--strict-mcp-config` so the operator's live session state can't leak in,
    a whole-process-group kill on timeout (start_new_session=True), and a
    capped drain so a runaway response can't exhaust memory."""
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
        remaining = timeout - elapsed
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
    return out_holder.get("v", ""), err_holder.get("v", ""), timed_out, (proc.returncode or 0)


def _accounting(envelope: dict | None) -> dict:
    """Pull token/cost/timing out of the provider JSON envelope when present, so
    it survives onto the raised PlannerError and into fallback job metadata even
    on a failed plan (requirement: preserve token/cost/timing when available)."""
    if not envelope:
        return {"tokens": None, "cost_usd": None, "duration_ms": None}
    usage = envelope.get("usage") or {}
    tokens = None
    if usage:
        tokens = {"input_tokens": usage.get("input_tokens"),
                  "output_tokens": usage.get("output_tokens")}
    return {"tokens": tokens, "cost_usd": envelope.get("total_cost_usd"),
            "duration_ms": envelope.get("duration_ms")}


def _sanitize_raw(text: str, cap: int = 4000) -> str:
    """Redact provider keys/bearer tokens and cap length before the raw planner
    response is ever stored as a diagnostic — never persist secrets."""
    return _redact(text or "")[:cap]


def plan(goal: str, instructions: str, project_path: str, allowed_paths: list,
        timeout: int | None = None, heartbeat_cb=None, kind: str | None = None,
        model: str | None = None) -> dict:
    """heartbeat_cb(elapsed_seconds), if given, is called roughly every
    RUNTIME_PLAN_HEARTBEAT_SECS while the provider call is still running, so
    long-running plans surface progress instead of going silent until they
    finish or hit the timeout.

    `kind` is the job kind (see core.job_kinds). A non-code kind is allowed to
    produce a plan with no file operations; code changes still must touch a file.

    `model`, if given (e.g. from core.model_router), overrides RUNTIME_CLAUDE_MODEL
    for this call only — same override precedence as smoke()'s `model` param.
    """
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
    use_model = model or _MODEL or None
    cmd = [_CLAUDE, "-p"]
    if use_model:
        cmd += ["--model", use_model]
    # The planner only ever needs to emit text — it must never act. `--tools ""`
    # is what actually stops the CLI going agentic on task-shaped instructions.
    # `--setting-sources ""` and `--strict-mcp-config` stop the operator's live
    # $HOME/.claude session state (output styles, permission overrides, MCP
    # servers) from leaking into and corrupting this "stateless" subprocess call.
    cmd += ["--tools", "", "--setting-sources", "", "--strict-mcp-config", "--output-format", "json"]
    effective_timeout = _TIMEOUT if timeout is None else timeout

    stdout, stderr, timed_out, returncode = _invoke_cli(cmd, prompt, effective_timeout, heartbeat_cb)

    try:
        envelope = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        envelope = None
    acct = _accounting(envelope)

    from core import job_kinds
    allow_empty = kind is not None and not job_kinds.requires_code_changes(kind)
    plan_text = envelope["result"] if envelope is not None and "result" in envelope else stdout

    if timed_out:
        # The deadline is on the *process*, not on the work. The CLI can emit a
        # complete plan and only then linger past the deadline (slow teardown, a
        # detached grandchild still holding the pipe) until the group is killed.
        # Discarding a plan the provider already delivered turns a satisfiable
        # job into a `fallback_plan_only` NON-implementation — that is what job
        # 86 (task_id=86) lost. So salvage a delivered plan that parses AND
        # validates; a timeout with nothing usable is still a planner failure.
        # The returncode check below is deliberately skipped here: a killed
        # process group always reports failure, which says nothing about whether
        # the bytes it already wrote are a good plan.
        if stdout.strip() and not (envelope is not None and envelope.get("is_error")):
            try:
                return _validate(_extract_json(plan_text), allowed_paths,
                                 allow_empty_files=allow_empty)
            except PlannerError:
                pass  # nothing usable was delivered — fall through to the timeout
        raise PlannerError("planner timed out", timed_out=True, **acct)

    if returncode != 0 or (envelope is not None and envelope.get("is_error")):
        raise PlannerError(_classify_failure(stdout, stderr, envelope), **acct)
    if not stdout.strip():
        raise PlannerError("planner produced empty output", **acct)

    raw = _sanitize_raw(plan_text)
    try:
        plan_obj = _extract_json(plan_text)
        return _validate(plan_obj, allowed_paths, allow_empty_files=allow_empty)
    except PlannerError as e:
        # Re-raise with the sanitized raw response + accounting attached so the
        # caller can record diagnostics and fall back deterministically.
        raise PlannerError(str(e), raw=raw, **acct) from e


# ── PHASE 45 — provider smoke test: one minimal, read-only, non-agentic call ──
_SMOKE_PROMPT = "Reply with exactly one word: pong"
_SMOKE_TIMEOUT_CAP = 60.0
# Defense in depth on top of _classify_failure's short, non-secret error classes:
# never let a raw provider key/token pattern reach the response, even truncated.
_SECRET_LEAK_RE = re.compile(r"(sk-ant-[a-zA-Z0-9_-]+)|(Bearer\s+[a-zA-Z0-9._-]+)|(api[_-]?key\S*)", re.IGNORECASE)


def _redact(text: str) -> str:
    return _SECRET_LEAK_RE.sub("[redacted]", text or "")


def smoke(model: str | None = None, timeout_seconds: float | None = None) -> dict:
    """Minimal real round-trip to the configured provider. Read-only: no
    project_path, no file/db writes, no plan, no coding job — just proves the
    provider answers. Exactly one subprocess call, hard-capped timeout, never
    retried internally (a caller-side retry loop is the caller's decision, not
    this function's)."""
    started = time.monotonic()
    hard_timeout = min(float(timeout_seconds) if timeout_seconds else _SMOKE_TIMEOUT_CAP, _SMOKE_TIMEOUT_CAP)
    use_model = model or _MODEL or None
    if not available():
        return {"ok": False, "provider": "claude-cli", "model": use_model, "latency_seconds": 0.0,
                "tokens": None, "cost_usd": None, "error": "provider_not_configured"}

    cmd = [_CLAUDE, "-p"]
    if use_model:
        cmd += ["--model", use_model]
    cmd += ["--tools", "", "--setting-sources", "", "--strict-mcp-config", "--output-format", "json"]

    try:
        stdout, stderr, timed_out, returncode = _invoke_cli(cmd, _SMOKE_PROMPT, hard_timeout)
    except PlannerError as e:
        return {"ok": False, "provider": "claude-cli", "model": use_model,
                "latency_seconds": round(time.monotonic() - started, 3),
                "tokens": None, "cost_usd": None, "error": _redact(str(e))[:300]}

    latency = round(time.monotonic() - started, 3)
    if timed_out:
        return {"ok": False, "provider": "claude-cli", "model": use_model, "latency_seconds": latency,
                "tokens": None, "cost_usd": None, "error": f"provider smoke test timed out after {hard_timeout}s"}

    try:
        envelope = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        envelope = None

    if returncode != 0 or (envelope is not None and envelope.get("is_error")) or not stdout.strip():
        return {"ok": False, "provider": "claude-cli", "model": use_model, "latency_seconds": latency,
                "tokens": None, "cost_usd": None, "error": _redact(_classify_failure(stdout, stderr, envelope))[:300]}

    usage = (envelope or {}).get("usage") or {}
    model_usage = (envelope or {}).get("modelUsage") or {}
    resolved_model = use_model
    if not resolved_model and model_usage:
        resolved_model = max(model_usage.items(), key=lambda kv: kv[1].get("outputTokens", 0))[0]
    return {
        "ok": True, "provider": "claude-cli", "model": resolved_model, "latency_seconds": latency,
        "tokens": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
        "cost_usd": (envelope or {}).get("total_cost_usd"), "error": None,
    }


# ── Deterministic local fallback plan ────────────────────────────────────────
# Used when the provider planner fails (timeout / empty / malformed / non-JSON /
# plain-text / provider error) so a planner failure never terminates the Runtime
# job. Fully deterministic: no provider call, no randomness — same inputs always
# yield the same plan. Conservative and safe by construction (one recorded
# planning artifact + the repo's own test suite; no fabricated code changes),
# and it stops short of any dangerous/irreversible action (never merges, never
# force-pushes, never deletes).

# The conservative workflow the fallback commits the runtime to — recorded in
# the plan and the artifact doc so downstream execution and any human reviewer
# see exactly what a fallback run does and does not do.
FALLBACK_STAGES = [
    "inspect repository",
    "create or preserve the correct task branch",
    "implement the requested change",
    "run focused tests",
    "run the relevant full suite",
    "commit",
    "push",
    "open or update a draft PR (never merge)",
    "stop on any dangerous or irreversible action",
]

_TEST_CONFIG_HINTS = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "Makefile")


def _slugify(text: str, n: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:n].strip("-")) or "task"


def repo_metadata(project_path: str) -> dict:
    """Best-effort, read-only repo facts for the fallback plan. Never raises."""
    meta = {"path": project_path, "is_git": False, "branch": None,
            "head": None, "remote": None, "has_tests_dir": False}
    try:
        meta["has_tests_dir"] = os.path.isdir(os.path.join(project_path, "tests"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import git_write
        if git_write.is_repo(project_path):
            meta["is_git"] = True
            meta["branch"] = git_write.current_branch(project_path)
            meta["head"] = git_write.rev_parse_short(project_path)
            meta["remote"] = git_write.remote_url(project_path)
    except Exception:  # noqa: BLE001
        pass
    return meta


def default_test_commands(project_path: str) -> list[str]:
    """Derive a safe full-suite command from repository metadata alone. Returns
    [] when nothing recognizable is present rather than inventing a command."""
    try:
        has_tests = os.path.isdir(os.path.join(project_path, "tests"))
        has_cfg = any(os.path.exists(os.path.join(project_path, h)) for h in _TEST_CONFIG_HINTS)
        if has_tests or has_cfg:
            return ["python3 -m pytest -q"]
    except Exception:  # noqa: BLE001
        pass
    return []


def _render_fallback_doc(goal: str, instructions: str, meta: dict,
                         test_commands: list, diag: dict) -> str:
    tokens = diag.get("tokens")
    lines = [
        "# Runtime fallback plan (deterministic, provider planner unavailable)",
        "",
        "The AI provider planner did not return a usable plan, so this Runtime",
        "job proceeded on a deterministic local fallback plan instead of failing.",
        "",
        f"- **Goal:** {goal or '(none)'}",
        f"- **Planner failure:** {diag.get('reason', 'unknown')}",
        f"- **Timed out:** {bool(diag.get('timed_out'))}",
        "",
        "## Task instructions (verbatim)",
        "",
        (instructions or "(none)").strip(),
        "",
        "## Repository metadata",
        "",
        f"- git repo: {meta.get('is_git')}",
        f"- branch at planning time: {meta.get('branch')}",
        f"- head: {meta.get('head')}",
        f"- remote: {meta.get('remote')}",
        f"- tests/ dir present: {meta.get('has_tests_dir')}",
        "",
        "## Conservative execution stages",
        "",
        *[f"{i+1}. {s}" for i, s in enumerate(FALLBACK_STAGES)],
        "",
        "## Test commands",
        "",
        *([f"- `{c}`" for c in test_commands] or ["- (none derived)"]),
        "",
        "## Planner accounting (preserved when available)",
        "",
        f"- output tokens: {tokens.get('output_tokens') if tokens else None}",
        f"- input tokens: {tokens.get('input_tokens') if tokens else None}",
        f"- cost_usd: {diag.get('cost_usd')}",
        f"- duration_ms: {diag.get('duration_ms')}",
        "",
        "## Sanitized raw planner response (secrets redacted, truncated)",
        "",
        "```",
        (diag.get("raw") or "(empty)"),
        "```",
        "",
        "> Fallback runs never merge, never force-push, and never delete. Any",
        "> dangerous or irreversible action is left for a human.",
    ]
    return "\n".join(lines) + "\n"


def build_fallback_plan(goal: str, instructions: str, project_path: str,
                        allowed_paths: list, test_commands: list | None = None,
                        task_id=None, diagnostics: dict | None = None) -> dict:
    """Build the deterministic fallback plan object. Shaped exactly like a real
    plan (passes _validate, so the normal pipeline applies/tests/commits it) but
    carries fallback markers + preserved planner diagnostics. The single file op
    records the plan under a safe in-repo path; combined with the repo's own test
    suite this reaches the coding/execution stage without fabricating code."""
    diag = dict(diagnostics or {})
    diag.setdefault("reason", "planner_failed")
    diag.setdefault("raw", "")
    meta = repo_metadata(project_path)
    tcmds = list(test_commands) if test_commands else default_test_commands(project_path)

    rel = f"reports/runtime/fallback/PLAN-{task_id or 'task'}-{_slugify(goal or instructions)}.md"
    # honour an explicit allow-list if the caller set one
    if allowed_paths and not any(
            rel == a or rel.startswith(a.rstrip("/") + "/") for a in allowed_paths):
        rel = allowed_paths[0].rstrip("/") + "/RUNTIME_FALLBACK_PLAN.md"

    # idempotent: replace if it already exists (safe re-run / update draft PR),
    # else create. Never errors on a pre-existing artifact.
    try:
        op = "replace" if os.path.exists(os.path.join(project_path, rel)) else "create"
    except Exception:  # noqa: BLE001
        op = "create"

    content = _render_fallback_doc(goal or "", instructions or "", meta, tcmds, diag)
    plan_obj = {
        "summary": f"[fallback] deterministic local plan for: {(goal or 'task')[:80]}",
        "risk_level": "low",
        "files": [{"path": rel, "operation": op, "content": content}],
        "test_commands": tcmds,
        "expected_result": "fallback plan recorded; tests run; draft PR (never merged)",
        # fallback markers / preserved diagnostics (persisted with the plan)
        "fallback": True,
        "fallback_reason": diag.get("reason"),
        "fallback_timed_out": bool(diag.get("timed_out")),
        "stages": list(FALLBACK_STAGES),
        "raw_planner_response": diag.get("raw", ""),
        "planner_tokens": diag.get("tokens"),
        "planner_cost_usd": diag.get("cost_usd"),
        "planner_duration_ms": diag.get("duration_ms"),
        "repo_metadata": meta,
    }
    return _validate(plan_obj, allowed_paths)
