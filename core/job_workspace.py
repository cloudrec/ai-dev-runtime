"""Isolated per-job git workspaces (worktrees).

The runtime used to `git checkout -b` inside the project's own working tree —
the same tree the service runs from and the owner edits. Any dirty file the
checkout would touch aborted the job (`branch failed: error: Your local changes
... would be overwritten by checkout`), which killed OWNER-151/180/182/192/193/
200, and a job that DID succeed left the control repo sitting on its work
branch. Worktrees end both failure modes: `git worktree add -b <branch> <dir>
<base>` materializes the new branch in a separate directory without ever
touching the primary working tree, so owner dirty files are preserved
byte-for-byte by construction.

The worktree shares the repository's object store, so commits made inside it
are ordinary commits on an ordinary branch — visible from the primary checkout,
pushable, and still there after the worktree directory is removed.
"""
from __future__ import annotations

import os
import shutil

from core import git_write
from core.git_write import GitWriteError, _run

# Worktrees live OUTSIDE every project tree so a job can never test/commit its
# own scaffolding into the project by accident. Resolved at call time so tests
# and services can redirect without a module reload.
def worktree_root() -> str:
    return os.getenv("RUNTIME_WORKTREE_ROOT", "/var/lib/ai-runtime/worktrees")


def workspace_path(project_path: str, job_id: str) -> str:
    repo = os.path.basename((project_path or "").rstrip("/")) or "repo"
    return os.path.join(worktree_root(), repo, job_id)


def create(project_path: str, job_id: str, task_id, goal: str, base: str) -> dict:
    """Create the job's work branch as an isolated worktree.

    Returns {"path", "branch", "base"}. Never checks anything out in
    `project_path` itself and never requires the primary tree to be clean."""
    name = f"ai-runtime/{task_id or 'job'}-{git_write.slug(goal)}"
    if _run(project_path, ["branch", "--list", name]).strip():
        name = f"{name}-{git_write.rev_parse_short(project_path)}"
    ws = workspace_path(project_path, job_id)
    if os.path.exists(ws):
        # a crashed prior attempt for this exact job id; remove the leftover
        remove(project_path, ws)
    os.makedirs(os.path.dirname(ws), exist_ok=True)
    _run(project_path, ["worktree", "add", "--no-track", "-b", name, ws, base])
    return {"path": ws, "branch": name, "base": base}


def remove(project_path: str, ws_path: str) -> bool:
    """Remove a job worktree. The branch and its commits survive — only the
    scratch directory goes. Force is safe here: everything worth keeping was
    committed; uncommitted content in a terminal job's worktree is exactly the
    debris the old shared-tree model leaked into the next job."""
    ok = True
    try:
        _run(project_path, ["worktree", "remove", "--force", ws_path])
    except GitWriteError:
        ok = False
        try:
            shutil.rmtree(ws_path, ignore_errors=True)
            _run(project_path, ["worktree", "prune"], check=False)
        except Exception:  # noqa: BLE001
            pass
    return ok or not os.path.exists(ws_path)
