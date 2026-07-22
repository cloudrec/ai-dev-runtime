"""git_push_policy — narrow routine-push auto-approval. Uses a real local repo +
bare remote (no network, no GitHub, no token)."""
from __future__ import annotations

import subprocess

import pytest

from core import git_push_policy as gp


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()


@pytest.fixture
def repo(tmp_path):
    """A work repo on branch `feat/x` tracking a local bare `origin`, one commit
    ahead, clean tree. Returns (workdir, project_record)."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(work, "config", k, v)
    (work / "a.txt").write_text("hello")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "feat/x")
    (work / "b.txt").write_text("feature")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "feat b")
    _git(work, "push", "-u", "origin", "feat/x")
    # add one more local commit so HEAD is ahead of upstream by 1
    (work / "c.txt").write_text("more")
    _git(work, "add", "c.txt")
    _git(work, "commit", "-m", "feat c")
    project = {"push_repo": str(bare), "protected_branches": ["main", "master"]}
    return work, project


# ── allowed routine shapes ──────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "git push",
    "git push origin HEAD",
    "git push origin feat/x",
    "git push -q origin HEAD",
    "cd . && git push origin HEAD",
])
def test_routine_push_allowed(repo, cmd):
    work, project = repo
    r = gp.evaluate_push(cmd, str(work), project)
    assert r["allowed"] is True, r["reason"]
    assert r["checks"]["branch"] == "feat/x"


def test_verify_push_matches_after_push(repo):
    work, project = repo
    assert gp.evaluate_push("git push origin HEAD", str(work), project)["allowed"]
    head = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "push", "origin", "HEAD")                 # perform the push
    v = gp.verify_push(str(work), "feat/x", head)
    assert v["ok"] is True and v["remote_sha"] == head


# ── denied shapes (fail closed, exact reason) ───────────────────────────────
@pytest.mark.parametrize("cmd,frag", [
    ("git push --force origin HEAD", "forbidden push flag"),
    ("git push -f origin feat/x", "forbidden push flag"),
    ("git push --force-with-lease origin HEAD", "forbidden push flag"),
    ("git push --tags", "forbidden push flag"),
    ("git push --delete origin feat/x", "forbidden push flag"),
    ("git push --mirror origin", "forbidden push flag"),
    ("git push --all origin", "forbidden push flag"),
    ("git push -u origin feat/x", "forbidden push flag"),
    ("git push --set-upstream neworigin feat/x", "forbidden push flag"),
    ("git push origin +feat/x", "force refspec"),
    ("git push origin feat/x:other", "src:dst refspec"),
    ("git push origin :feat/x", "src:dst refspec"),
    ("git push origin --no-verify HEAD", "forbidden push flag"),
    ("git push origin a b", "too many push arguments"),
])
def test_denied_shapes(repo, cmd, frag):
    work, project = repo
    r = gp.evaluate_push(cmd, str(work), project)
    assert r["allowed"] is False and frag in r["reason"]


def test_denied_other_branch(repo):
    work, project = repo
    r = gp.evaluate_push("git push origin main", str(work), project)
    assert r["allowed"] is False and "current branch" in r["reason"]


def test_denied_protected_branch(repo):
    work, project = repo
    _git(work, "checkout", "main")
    r = gp.evaluate_push("git push origin HEAD", str(work), project)
    assert r["allowed"] is False and "protected" in r["reason"]


def test_denied_remote_mismatch(repo):
    work, project = repo
    r = gp.evaluate_push("git push origin HEAD", str(work), {**project, "push_repo": "github.com/someone/else"})
    assert r["allowed"] is False and "does not match project record" in r["reason"]


def test_denied_uncommitted_changes(repo):
    work, project = repo
    (work / "b.txt").write_text("dirty edit")            # modify a tracked file
    r = gp.evaluate_push("git push origin HEAD", str(work), project)
    assert r["allowed"] is False and "uncommitted tracked changes" in r["reason"]


def test_denied_secret_file_in_push(repo):
    work, project = repo
    (work / ".env").write_text("SECRET=x")
    _git(work, "add", ".env")
    _git(work, "commit", "-m", "add env")
    r = gp.evaluate_push("git push origin HEAD", str(work), project)
    assert r["allowed"] is False and "secret-named file" in r["reason"]


def test_denied_behind_upstream(repo):
    work, project = repo
    # push current, then reset local behind by making the remote ahead via a 2nd clone
    _git(work, "push", "origin", "HEAD")
    other = work.parent / "other"
    subprocess.run(["git", "clone", "-b", "feat/x", str(project["push_repo"]), str(other)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(other, "config", k, v)
    (other / "d.txt").write_text("remote ahead")
    _git(other, "add", "d.txt"); _git(other, "commit", "-m", "remote"); _git(other, "push", "origin", "HEAD")
    _git(work, "fetch", "origin")
    r = gp.evaluate_push("git push origin HEAD", str(work), project)
    assert r["allowed"] is False and "behind upstream" in r["reason"]


def test_denied_non_push_git_and_wrapped_writes(repo):
    work, project = repo
    assert gp.evaluate_push("git push origin HEAD && rm -rf x", str(work), project)["allowed"] is False
    assert gp.evaluate_push("git commit -am x", str(work), project)["allowed"] is False
    assert gp.is_push_command("git status") is False
    assert gp.is_push_command("git push") is True
