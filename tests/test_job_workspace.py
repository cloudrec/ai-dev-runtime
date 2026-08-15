"""Isolated worktree execution — regression for the OWNER-151/180/182/192/193/200
dirty-checkout failures.

The fixture reproduces runtime job 888f5266 (task OWNER-193, Venture Radar,
2026-08-15 07:05Z) exactly: the control repo sits on a feature branch with
uncommitted owner files that DIFFER on the resolved base branch, so the old
`git checkout -b work base` in the shared tree aborts with "Your local changes
... would be overwritten by checkout". The worktree path must succeed under the
identical preconditions and leave every dirty file byte-for-byte intact.
"""
import os
import subprocess

import pytest

from core import git_write, job_workspace


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def dirty_repo(tmp_path, monkeypatch):
    """A repo shaped like the control repo at the moment job 75 failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    report = repo / "reports"
    report.mkdir()
    f = report / "OWNER_OS_WAKE_BRIDGE_REPAIR.md"
    f.write_text("base version\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    # feature branch changes the file and commits, then leaves MORE dirty edits
    _git(repo, "checkout", "-b", "feature/wake-repair")
    f.write_text("feature version\n")
    _git(repo, "commit", "-am", "feature")
    dirty_bytes = "feature version\nplus uncommitted owner notes \xe2\x80\x94 do not touch\n"
    f.write_text(dirty_bytes)
    monkeypatch.setenv("RUNTIME_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    return {"repo": str(repo), "file": str(f), "dirty_bytes": dirty_bytes}


def test_old_shared_tree_checkout_reproduces_job75_failure(dirty_repo):
    """Positive control for the regression: the pre-fix path fails exactly as
    the production jobs did."""
    with pytest.raises(git_write.GitWriteError, match="would be overwritten by checkout"):
        git_write.create_work_branch(dirty_repo["repo"], 193, "Venture Radar", "main")
    # and the dirty file survives the failed attempt
    assert open(dirty_repo["file"]).read() == dirty_repo["dirty_bytes"]


def test_worktree_isolation_succeeds_where_checkout_failed(dirty_repo):
    ws = job_workspace.create(dirty_repo["repo"], "job-75", 193, "Venture Radar", "main")
    try:
        # the primary tree was never touched: same branch, same dirty bytes
        assert git_write.current_branch(dirty_repo["repo"]) == "feature/wake-repair"
        assert open(dirty_repo["file"]).read() == dirty_repo["dirty_bytes"]
        # the worktree is on the new work branch, based on main
        assert git_write.current_branch(ws["path"]) == ws["branch"]
        assert ws["branch"].startswith("ai-runtime/193-")
        base_content = open(os.path.join(
            ws["path"], "reports", "OWNER_OS_WAKE_BRIDGE_REPAIR.md")).read()
        assert base_content == "base version\n"
    finally:
        job_workspace.remove(dirty_repo["repo"], ws["path"])


def test_commit_in_worktree_lands_on_branch_visible_from_primary(dirty_repo):
    ws = job_workspace.create(dirty_repo["repo"], "job-75", 193, "Venture Radar", "main")
    new_file = os.path.join(ws["path"], "core_venture_radar.py")
    with open(new_file, "w") as fh:
        fh.write("VERSION = 1\n")
    git_write.add_paths(ws["path"], ["core_venture_radar.py"])
    sha = git_write.commit(ws["path"], "feat(runtime): venture radar")
    assert job_workspace.remove(dirty_repo["repo"], ws["path"])
    # branch and commit survive the worktree's removal, visible from the repo
    out = subprocess.run(["git", "-C", dirty_repo["repo"], "log", "--oneline", ws["branch"]],
                         check=True, capture_output=True, text=True).stdout
    assert sha in out
    # and the primary tree still never moved
    assert git_write.current_branch(dirty_repo["repo"]) == "feature/wake-repair"
    assert open(dirty_repo["file"]).read() == dirty_repo["dirty_bytes"]


def test_recreate_after_crash_replaces_leftover_worktree(dirty_repo):
    ws1 = job_workspace.create(dirty_repo["repo"], "job-x", 200, "SEO validation", "main")
    # simulated crash: no remove. The same job id must be able to start over.
    ws2 = job_workspace.create(dirty_repo["repo"], "job-x", 200, "SEO validation", "main")
    try:
        assert ws2["path"] == ws1["path"]
        assert os.path.isdir(ws2["path"])
    finally:
        job_workspace.remove(dirty_repo["repo"], ws2["path"])


def test_existing_branch_gets_suffix(dirty_repo):
    ws1 = job_workspace.create(dirty_repo["repo"], "job-a", 193, "Venture Radar", "main")
    ws2 = job_workspace.create(dirty_repo["repo"], "job-b", 193, "Venture Radar", "main")
    try:
        assert ws1["branch"] != ws2["branch"]
        assert ws2["branch"].startswith(ws1["branch"])
    finally:
        job_workspace.remove(dirty_repo["repo"], ws1["path"])
        job_workspace.remove(dirty_repo["repo"], ws2["path"])
