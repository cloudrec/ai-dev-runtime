"""PHASE 13 — controlled Git write executor (branch-based, selective staging).

subprocess only (no shell=True). Never `git add .`, never force-push. Scans the
staged diff for secrets before commit.
"""
from __future__ import annotations

import os
import re
import subprocess

from core import agent_privilege

_TIMEOUT = 60
_SECRET_CONTENT_RE = re.compile(
    r"(AKIA[0-9A-Z]{16})|(-----BEGIN [A-Z ]*PRIVATE KEY-----)|(sk-[A-Za-z0-9]{20,})|"
    r"(ghp_[A-Za-z0-9]{20,})|(xox[baprs]-[A-Za-z0-9-]{10,})")
_SECRET_PATH_RE = re.compile(r"(^|/)\.env|\.pem$|\.key$|id_rsa|credential|secret", re.IGNORECASE)


class GitWriteError(RuntimeError):
    pass


def _run(project_path: str, args: list[str], check: bool = True) -> str:
    try:
        agent_privilege.require_root_work(project_path)   # safe.directory=* trusts any owner
    except agent_privilege.AgentPrivilegeError as e:
        raise GitWriteError(f"agent_run_as:{e}") from None
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


#: Branches created by the runtime itself. One job's work branch must never
#: become another job's base: that is how OWNER-111/113..118/120 all ended up
#: pinned to the same fallback commit (5e3ec9e). A previous job's branch is only
#: acceptable as a base when the caller names it explicitly (e.g. a retry that
#: intends to continue that exact work).
_WORK_BRANCH_RE = re.compile(r"^(ai-runtime|repair)/")


def is_work_branch(name: str) -> bool:
    return bool(_WORK_BRANCH_RE.match((name or "").strip()))


def resolve_base_branch(project_path: str, explicit: str | None = None) -> str:
    """Never hardcode 'master'. Priority: explicit task/retry branch ->
    origin/HEAD default branch -> current workspace branch (only when it is not
    itself a runtime work branch) -> 'main' -> 'master' -> fail closed
    (GitWriteError) if none resolve to a real commit.

    The current-branch candidate is deliberately skipped for runtime work
    branches. Previously `base_branch='master'` (a branch that does not exist in
    this repository) fell through to the current workspace branch, so every job
    branched off whichever branch the previous job happened to leave checked
    out — stacking unrelated work and giving many branches an identical HEAD.
    """
    candidates: list[str] = [explicit, _origin_default_branch(project_path)]
    try:
        cur = current_branch(project_path)
        if cur != "HEAD" and not is_work_branch(cur):
            candidates.append(cur)
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


class BaseRefBlocked(GitWriteError):
    """No trustworthy base for this job: an actionable blocker, never a guess."""


#: Placeholder the API and supervisor put in `base_branch` when the caller named
#: nothing (`JobCreate.base_branch = "master"`). It is not an explicit choice.
_DEFAULT_SENTINELS = frozenset({"", "master"})

_BASE_REFS_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                                 "config", "project_base_refs.yaml")


def _strict_base_ref() -> bool:
    return os.getenv("RUNTIME_STRICT_BASE_REF", "0").strip().lower() in ("1", "true", "yes", "on")


def configured_base_ref(project_path: str) -> str | None:
    """The base the project's authorized config names for runtime jobs, or None.

    config/project_base_refs.yaml (path overridable via RUNTIME_BASE_REFS_CONFIG):
        projects:
          /abs/project/path: {base_ref: <branch, tag or full SHA>}
    """
    path = os.getenv("RUNTIME_BASE_REFS_CONFIG") or _BASE_REFS_CONFIG
    if not os.path.isfile(path):
        return None
    import yaml  # local: only needed when the file exists
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    entry = ((data.get("projects") or {}).get(os.path.realpath(project_path))
             or (data.get("projects") or {}).get(project_path))
    if isinstance(entry, dict):
        ref = entry.get("base_ref")
    else:
        ref = entry
    ref = str(ref).strip() if ref else ""
    return ref or None


def rev_sha(project_path: str, ref: str) -> str:
    return _run(project_path, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]).strip()


def _staleness(project_path: str, base: str) -> str | None:
    """Why `base` is not a credible base for work on this checkout, or None.

    The checked-out HEAD is the line the project actually runs. A guessed base that
    is not its ancestor, or trails it by more than RUNTIME_BASE_STALE_MAX commits,
    would build and test the job on code the project no longer runs (task 326: main
    was 449 commits behind and not an ancestor)."""
    try:
        head = rev_sha(project_path, "HEAD")
        base_sha = rev_sha(project_path, base)
    except GitWriteError:
        return None
    if head == base_sha:
        return None
    behind = int(_run(project_path, ["rev-list", "--count", f"{base_sha}..{head}"]).strip() or 0)
    ancestor = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", project_path, "merge-base", "--is-ancestor",
         base_sha, head], capture_output=True, timeout=_TIMEOUT).returncode == 0
    limit = int(os.getenv("RUNTIME_BASE_STALE_MAX", "50"))
    if not ancestor:
        return (f"{base}@{base_sha[:12]} is not an ancestor of the checked-out HEAD "
                f"@{head[:12]} ({behind} commits only on HEAD)")
    if behind > limit:
        return f"{base}@{base_sha[:12]} is {behind} commits behind the checked-out HEAD @{head[:12]}"
    return None


def resolve_job_base(project_path: str, explicit: str | None = None) -> dict:
    """The base for a runtime job, with its exact SHA and where it came from.

    1. An explicit base (anything but the "master" placeholder).
    2. The project's configured base_ref (config/project_base_refs.yaml).
       A configured ref that does not resolve is a blocker, never a fallback.
    3. The legacy guess (resolve_base_branch).

    With RUNTIME_STRICT_BASE_REF on, an explicit base that does not resolve and a
    guessed base that is stale or diverged from the checked-out HEAD are blockers
    too. Off (the default), steps 1 and 3 behave exactly as resolve_base_branch.

    Returns {"ref", "sha", "source"}; raises BaseRefBlocked with the fix in the text.
    """
    strict = _strict_base_ref()
    exp = (explicit or "").strip()
    if exp not in _DEFAULT_SENTINELS:
        for cand in (exp, f"origin/{exp}"):
            if _rev_ok(project_path, cand):
                return {"ref": cand, "sha": rev_sha(project_path, cand), "source": "explicit"}
        if strict:
            raise BaseRefBlocked(f"explicit base {exp!r} does not resolve in {project_path}")
    configured = configured_base_ref(project_path)
    if configured:
        for cand in (configured, f"origin/{configured}"):
            if _rev_ok(project_path, cand):
                return {"ref": cand, "sha": rev_sha(project_path, cand), "source": "config"}
        raise BaseRefBlocked(
            f"configured base_ref {configured!r} for {project_path} does not resolve; "
            "fix config/project_base_refs.yaml")
    ref = resolve_base_branch(project_path, explicit)
    if strict:
        why = _staleness(project_path, ref)
        if why:
            raise BaseRefBlocked(
                f"no authorized base for {project_path}: guessed {why}. Set base_ref for this "
                "project in config/project_base_refs.yaml or pass an explicit base_branch")
    return {"ref": ref, "sha": rev_sha(project_path, ref), "source": "guess"}


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
