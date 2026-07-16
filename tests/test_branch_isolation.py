"""Branch isolation and duplicate-HEAD detection.

Regression cover for the failure that gave OWNER-111 and OWNER-113..118/120 an
identical HEAD (5e3ec9e): `base_branch='master'` does not exist in this
repository, so base resolution fell through to the *current workspace branch* —
whichever branch the previous job happened to leave checked out.
"""
import subprocess
import sys

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import git_write  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(str(r), "init", "-q", "-b", "main")
    _git(str(r), "config", "user.email", "t@t")
    _git(str(r), "config", "user.name", "t")
    (r / "f.txt").write_text("1\n")
    _git(str(r), "add", "f.txt")
    _git(str(r), "commit", "-qm", "main base")
    return str(r)


def _commit_on(repo, branch, text):
    _git(repo, "checkout", "-qb", branch)
    with open(f"{repo}/f.txt", "w") as f:
        f.write(text)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", f"work on {branch}")


def test_work_branch_is_recognised():
    assert git_write.is_work_branch("ai-runtime/111-build-release-controller")
    assert git_write.is_work_branch("repair/runtime-recovery-20260716")
    assert not git_write.is_work_branch("main")
    assert not git_write.is_work_branch("develop")


def test_nonexistent_master_resolves_to_main_not_current_work_branch(repo):
    """The exact production bug: base_branch='master' with no master branch."""
    _commit_on(repo, "ai-runtime/111-previous-job", "previous job work\n")
    # workspace is now left on the previous job's branch, as after a real job
    assert git_write.current_branch(repo) == "ai-runtime/111-previous-job"
    assert git_write.resolve_base_branch(repo, "master") == "main"


def test_new_branch_does_not_inherit_previous_job_head(repo):
    """Two consecutive jobs must not end up pinned to the same commit."""
    _commit_on(repo, "ai-runtime/111-previous-job", "previous job work\n")
    prev_head = _git(repo, "rev-parse", "HEAD")
    main_head = _git(repo, "rev-parse", "main")

    base = git_write.resolve_base_branch(repo, "master")
    new_branch = git_write.create_work_branch(repo, 113, "Run first Prospect Audit batch", base)

    new_head = _git(repo, "rev-parse", "HEAD")
    assert new_head == main_head, "new job must branch from main"
    assert new_head != prev_head, "new job must NOT inherit the previous job's HEAD"
    assert new_branch.startswith("ai-runtime/113-")


def test_current_branch_is_used_as_base_when_it_is_not_a_work_branch(repo):
    """A human-style branch stays a legitimate base — only runtime work branches
    are excluded."""
    _git(repo, "checkout", "-qb", "develop")
    with open(f"{repo}/f.txt", "w") as f:
        f.write("dev\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "dev work")
    assert git_write.resolve_base_branch(repo, "nonexistent") == "develop"


def test_explicit_base_is_still_honoured_for_retries(repo):
    """A retry that intends to continue an exact branch may name it explicitly."""
    _commit_on(repo, "ai-runtime/88-original", "original\n")
    assert git_write.resolve_base_branch(repo, "ai-runtime/88-original") == "ai-runtime/88-original"


def test_duplicate_head_across_branches_is_detectable(repo):
    """The corrupted state itself: several branches pinned to one commit."""
    _git(repo, "branch", "ai-runtime/113-a", "main")
    _git(repo, "branch", "ai-runtime/114-b", "main")
    _git(repo, "branch", "ai-runtime/115-c", "main")

    heads = {}
    for b in ("ai-runtime/113-a", "ai-runtime/114-b", "ai-runtime/115-c"):
        heads.setdefault(_git(repo, "rev-parse", b), []).append(b)
    duplicates = {sha: bs for sha, bs in heads.items() if len(bs) > 1}
    assert duplicates, "three branches on one commit must be detected as duplicates"
    assert len(next(iter(duplicates.values()))) == 3


def test_distinct_jobs_from_main_get_distinct_heads(repo):
    """After the fix, two jobs branching from main have distinct commits."""
    base = git_write.resolve_base_branch(repo, "master")
    git_write.create_work_branch(repo, 113, "job one", base)
    with open(f"{repo}/a.txt", "w") as f:
        f.write("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "job one work")
    head_one = _git(repo, "rev-parse", "HEAD")

    base = git_write.resolve_base_branch(repo, "master")
    assert base == "main", "second job must not base on the first job's branch"
    git_write.create_work_branch(repo, 114, "job two", base)
    with open(f"{repo}/b.txt", "w") as f:
        f.write("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "job two work")
    head_two = _git(repo, "rev-parse", "HEAD")

    assert head_one != head_two
