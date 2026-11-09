"""Owner-allowed repositories the tech lead may start full-cycle projects on.

Only the owner adds a repository (`tools/fullcycle_repo.py allow`, root, on the server).
The list lives in a root-owned file outside every agent tree; the API and MCP can read the
names, never change them. An unsafe file (symlink, not root's, writable by others) counts
as an empty list.

A project on an allowed repository never runs in the repository itself. Intake makes a new
repository under the intake root, fetches the source's committed HEAD into it on branch
`fullcycle/<project id>` and keeps no remote, so the agent has nowhere to push and the
source is never written. Uncommitted work in the source is not taken. The owner takes the
result with `git fetch <project dir> fullcycle/<project id>`.

Refused whatever the owner wrote: payment directories (the creation policy's hard_deny_dirs
and any path naming payment or payorch), the live Owner OS tree and the other denied roots
(core.fullcycle.gates), symlinked paths, the intake root itself, anything that is not a
git checkout.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
from typing import Optional

from core.fullcycle import gates

DEFAULT_FILE = "/etc/ai-runtime/fullcycle-repos.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_PAYMENT_WORDS = ("payment", "payorch", "pay-orch", "pay_orch")


class RepoError(Exception):
    pass


def repos_file() -> str:
    return os.getenv("FULLCYCLE_REPOS_FILE", "").strip() or DEFAULT_FILE


def _safe_file(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and
            not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def load() -> dict:
    """{name: {"path", "added"}}; empty when the file is missing or unsafe."""
    path = repos_file()
    if not _safe_file(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {n: r for n, r in repos.items()
            if isinstance(n, str) and NAME_RE.match(n) and isinstance(r, dict)
            and isinstance(r.get("path"), str)}


def check_source(path: str) -> Optional[str]:
    """None when `path` may be a source, else the reason."""
    if not path or not os.path.isabs(path):
        return "repo_path_not_absolute"
    if not os.path.isdir(path):
        return "repo_path_missing"
    real = os.path.realpath(path)
    if real != os.path.normpath(os.path.abspath(path)):
        return "repo_path_is_symlink"
    low = real.lower()
    if any(w in low for w in _PAYMENT_WORDS):
        return "repo_payment_denied"
    refused = gates.check_project_root(real)
    if refused:
        return refused
    from core.fullcycle import intake
    root = intake.intake_root() or os.getenv("FULLCYCLE_INTAKE_ROOT", "").strip()
    if root and (gates._under(real, root) or gates._under(root, real)):
        return "repo_overlaps_intake_root"
    if not os.path.exists(os.path.join(real, ".git")):
        return "repo_not_a_git_checkout"
    try:
        _git(real, "rev-parse", "--verify", "-q", "HEAD^{commit}", timeout=30)
    except RepoError as e:
        return "repo_root_git_off" if "root_tooling_disabled" in str(e) else "repo_has_no_commit"
    return None


def get(name: str) -> dict:
    """The allowed repository `name`, checked again now. Raises RepoError."""
    if not NAME_RE.match(name or ""):
        raise RepoError("repo name must match " + NAME_RE.pattern)
    entry = load().get(name)
    if not entry:
        raise RepoError(f"repo_not_allowed:{name}")
    why = check_source(entry["path"])
    if why:
        raise RepoError(f"repo_refused:{name}:{why}")
    return {"name": name, "path": os.path.realpath(entry["path"])}


def listing() -> list:
    """For the reviewer: names, and whether each can be used right now. No paths."""
    out = []
    for name, entry in sorted(load().items()):
        why = check_source(entry["path"])
        out.append({"repo": name, "usable": why is None, **({"reason": why} if why else {})})
    return out


# -- owner side ------------------------------------------------------------------------
def _write(repos: dict) -> None:
    path = repos_file()
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.lexists(path) and not _safe_file(path):
        raise RepoError(f"unsafe_repos_file:{path}")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".fullcycle-repos.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"repos": repos}, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def allow(name: str, path: str) -> dict:
    if not NAME_RE.match(name or ""):
        raise RepoError("repo name must match " + NAME_RE.pattern)
    why = check_source(path)
    if why:
        raise RepoError(why)
    repos = load()
    repos[name] = {"path": os.path.realpath(path), "added": round(time.time())}
    _write(repos)
    return {"repo": name, "path": repos[name]["path"]}


def remove(name: str) -> bool:
    repos = load()
    if name not in repos:
        return False
    del repos[name]
    _write(repos)
    return True


# -- the project's own repository --------------------------------------------------------
def _git(cwd: str, *args: str, timeout: int = 300) -> str:
    # root git loads the repo's config; under AGENT_RUN_AS root tooling does not run
    from core import agent_privilege
    try:
        agent_privilege.require_root_work(cwd)
    except agent_privilege.AgentPrivilegeError as e:
        raise RepoError(f"root_tooling_disabled:{e}")
    r = subprocess.run(["git", "-c", "user.name=Owner OS", "-c", "user.email=owneros@localhost",
                        *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RepoError(f"git {args[0]} failed: {(r.stderr or '').strip()[:200]}")
    return r.stdout.strip()


def branch_for(project_id: str) -> str:
    return f"fullcycle/{project_id}"


def make_copy(source: str, path: str, project_id: str, exclude: str) -> str:
    """Into the new empty directory `path`: a repository holding the source's committed HEAD on branch
    fullcycle/<id>, with no remote. Returns the base sha."""
    base = _git(source, "rev-parse", "--verify", "HEAD^{commit}", timeout=30)
    _git(path, "init", "-q", "-b", branch_for(project_id))
    _git(path, "fetch", "-q", "--no-tags", "--no-recurse-submodules", source, base)
    got = _git(path, "rev-parse", "FETCH_HEAD", timeout=30)
    if got != base:
        raise RepoError("repo_head_moved_during_copy")
    _git(path, "reset", "-q", "--hard", base)
    if _git(path, "remote", timeout=30):
        raise RepoError("repo_copy_has_a_remote")
    with open(os.path.join(path, ".git", "info", "exclude"), "a", encoding="utf-8") as f:
        f.write(exclude)
    return base
