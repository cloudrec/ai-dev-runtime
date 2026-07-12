"""PHASE 13 — real AI planning via the host Claude CLI.

Produces a validated structured plan (files + test commands). No shell=True.
If the CLI is unavailable, returns provider_not_configured — never fakes a plan.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

_CLAUDE = os.getenv("RUNTIME_CLAUDE_BIN", shutil.which("claude") or "/root/.local/bin/claude")
_MODEL = os.getenv("RUNTIME_CLAUDE_MODEL", "")
_TIMEOUT = int(os.getenv("RUNTIME_PLAN_TIMEOUT", "180"))

_ALLOWED_OPS = {"create", "replace", "patch", "delete", "mkdir"}
_SECRET_RE = re.compile(r"(^\.env)|(\.pem$)|(\.key$)|(id_rsa)|(secret)|(credential)|(token)", re.IGNORECASE)


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


def plan(goal: str, instructions: str, project_path: str, allowed_paths: list) -> dict:
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
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=_TIMEOUT, shell=False)
    except subprocess.TimeoutExpired:
        raise PlannerError("planner timed out")
    except FileNotFoundError:
        raise PlannerError("provider_not_configured")
    if p.returncode != 0:
        raise PlannerError(f"claude cli error: {(p.stderr or p.stdout)[:300]}")
    plan_obj = _extract_json(p.stdout)
    return _validate(plan_obj, allowed_paths)
