"""job 24 (task_id 88): `git checkout -b <work> master` blew up with
`fatal: 'master' is not a commit` on repos that never had a master branch.
Runtime must not hardcode 'master' — resolve_base_branch() picks, in order:
explicit task/retry branch -> origin/HEAD default branch -> current
workspace branch -> 'main' -> 'master' -> fail closed."""
import subprocess

import pytest

from core import git_write


def _git(path, *args):
    subprocess.run(["git", "-C", str(path)] + list(args), check=True,
                    capture_output=True, text=True)


def _init_repo(path, branch="main"):
    path.mkdir(exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "--allow-empty", "-m", "init")
    return path


def test_explicit_branch_wins_when_valid(tmp_path):
    repo = _init_repo(tmp_path / "r1", branch="main")
    _git(repo, "branch", "feature-x")
    assert git_write.resolve_base_branch(str(repo), "feature-x") == "feature-x"


def test_invalid_explicit_falls_back_to_origin_head(tmp_path):
    origin = _init_repo(tmp_path / "origin", branch="main")
    repo = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "checkout", "-q", "-b", "some-topic-branch")
    # explicit "master" does not exist anywhere in this repo -> must not raise,
    # must fall through to origin/HEAD ("main")
    assert git_write.resolve_base_branch(str(repo), "master") == "main"


def test_no_explicit_no_origin_falls_back_to_current_branch(tmp_path):
    repo = _init_repo(tmp_path / "r2", branch="my-workspace-branch")
    assert git_write.resolve_base_branch(str(repo), None) == "my-workspace-branch"


def test_prefers_main_over_master_when_no_other_signal(tmp_path):
    repo = _init_repo(tmp_path / "r3", branch="trunk")
    _git(repo, "branch", "main")
    _git(repo, "checkout", "-q", "--detach")  # current_branch() -> "HEAD", must be skipped
    assert git_write.resolve_base_branch(str(repo), None) == "main"


def test_fails_closed_when_nothing_resolves(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "unborn")  # no commits at all
    with pytest.raises(git_write.GitWriteError, match="no valid base branch"):
        git_write.resolve_base_branch(str(repo), "master")


def test_create_work_branch_never_hardcodes_master(tmp_path):
    """Reproduces job 24 / task 88: a repo with only 'main', no 'master'.
    The old code passed the literal 'master' straight to `git checkout -b`
    and crashed; the fix must resolve to 'main' first."""
    repo = _init_repo(tmp_path / "r4", branch="main")
    base = git_write.resolve_base_branch(str(repo), "master")
    assert base == "main"
    branch = git_write.create_work_branch(str(repo), 88, "fix planner timeout", base)
    assert git_write.current_branch(str(repo)) == branch
