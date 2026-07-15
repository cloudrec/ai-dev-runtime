"""PHASE 13 — controlled Git write executor (branch-based, selective staging).

subprocess only (no shell=True). Never `git add .`, never force-push. Scans the
staged diff for secrets before commit.
"""
from __future__ import annotations

import os
import re
import subprocess

_TIMEOUT = 60
_SECRET_CONTENT_RE = re.compile(
    r"(AKIA[0-9A-Z]{16})|(-----BEGIN [A-Z ]*PRIVATE KEY-----)|(sk-[A-Za-z0-9]{20,})|"
    r"(ghp_[A-Za-z0-9]{20,})|(xox[baprs]-[A-Za-z0-9-]{10,})")
_SECRET_PATH_RE = re.compile(r"(^|/)\.env|\.pem$|\.key$|id_rsa|credential|secret", re.IGNORECASE)


class GitWriteError(RuntimeError):
    pass


def _run(project_path: str, args: list[str], check: bool = True) -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    cmd = ["git", "-c", "safe.directory=*", "-C", project_path] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT, env=env, shell=False)
    if check and p.returncode != 0:
        raise GitWriteError((p.stderr or p.stdout or "git error").strip()[:400])
    return p.stdout


def slug(text: str, n: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n] or "task"


def is_repo(project_path: str) -> bool:
    return os.path.isdir(os.path.join(project_path, ".git"))


def current_branch(project_path: str) -> str:
    return _run(project_path, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()


def rev_parse_short(project_path: str) -> str:
    return _run(project_path, ["rev-parse", "--short", "HEAD"]).strip()


def remote_url(project_path: str) -> str:
    try:
        url = _run(project_path, ["remote", "get-url", "origin"]).strip()
    except GitWriteError:
        return ""
    return re.sub(r"(://)[^@/\s]+@", r"\1***@", url)


def dirty_files(project_path: str) -> list[str]:
    return [ln for ln in _run(project_path, ["status", "--porcelain"]).splitlines() if ln.strip()]


def fetch(project_path: str) -> None:
    try:
        _run(project_path, ["fetch", "--quiet", "origin"])
    except GitWriteError:
        pass  # offline / no remote is acceptable for local-only repos


def _rev_ok(project_path: str, ref: str) -> bool:
    try:
        _run(project_path, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        return True
    except GitWriteError:
        return False


def _origin_default_branch(project_path: str) -> str | None:
    try:
        ref = _run(project_path, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]).strip()
    except GitWriteError:
        return None
    return ref.rsplit("/", 1)[-1] if ref else None


def resolve_base_branch(project_path: str, explicit: str | None = None) -> str:
    """Never hardcode 'master'. Priority: explicit task/retry branch ->
    origin/HEAD default branch -> current workspace branch -> 'main' ->
    'master' -> fail closed (GitWriteError) if none resolve to a real commit."""
    candidates: list[str] = [explicit, _origin_default_branch(project_path)]
    try:
        cur = current_branch(project_path)
        candidates.append(cur if cur != "HEAD" else None)
    except GitWriteError:
        pass
    candidates += ["main", "master"]

    seen: set[str] = set()
    tried: list[str] = []
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        tried.append(name)
        if _rev_ok(project_path, name):
            return name
        if _rev_ok(project_path, f"origin/{name}"):
            return f"origin/{name}"
    raise GitWriteError(f"no valid base branch found (tried: {', '.join(tried)})")


def create_work_branch(project_path: str, task_id, goal: str, base: str) -> str:
    name = f"ai-runtime/{task_id or 'job'}-{slug(goal)}"
    # if branch exists, append a short suffix
    existing = _run(project_path, ["branch", "--list", name]).strip()
    if existing:
        name = f"{name}-{rev_parse_short(project_path)}"
    _run(project_path, ["checkout", "-b", name, base])
    return name


def add_paths(project_path: str, paths: list[str]) -> None:
    if not paths:
        raise GitWriteError("no paths to stage (refusing 'git add .')")
    _run(project_path, ["add", "--"] + list(paths))


def scan_staged_for_secrets(project_path: str) -> list[str]:
    findings = []
    names = _run(project_path, ["diff", "--cached", "--name-only"]).splitlines()
    for n in names:
        if _SECRET_PATH_RE.search(n):
            findings.append(f"secret-like path staged: {n}")
    diff = _run(project_path, ["diff", "--cached"])
    if _SECRET_CONTENT_RE.search(diff):
        findings.append("secret-like content in staged diff")
    return findings


def commit(project_path: str, message: str) -> str:
    _run(project_path, ["-c", "user.name=AI Runtime", "-c", "user.email=runtime@local", "commit", "-m", message])
    return rev_parse_short(project_path)


def push(project_path: str, branch: str) -> dict:
    out = _run(project_path, ["push", "-u", "origin", branch])
    return {"pushed": True, "branch": branch, "output": out[:400]}
