"""Task 326: a runtime job for /root/ai-dev-runtime was built on `main`, 449 commits
behind and not an ancestor of the line the project runs, because the live branch is
named like a runtime work branch and the API's "master" placeholder resolves nowhere.

resolve_job_base(): explicit base -> project's configured base_ref -> legacy guess,
always with the exact SHA. A configured ref that does not resolve blocks; with
RUNTIME_STRICT_BASE_REF on, so does an unresolvable explicit base and a stale or
diverged guess. With no config and the flag off it returns what resolve_base_branch
returned before (contract preserved)."""
import subprocess

import pytest

from core import git_write


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
                          + list(args), check=True, capture_output=True, text=True).stdout.strip()


_SEQ = iter(range(10**6))


def _commit(path, n=1):
    # Unique messages: identical empty commits made in the same second hash the
    # same, which would silently make two "different" branches share history.
    for _ in range(n):
        _git(path, "commit", "-q", "--allow-empty", "-m", f"c{next(_SEQ)}")


def _task326_repo(tmp_path, ahead=5):
    """main, then a deployed line on an ai-runtime/ branch that main lacks."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo)
    _git(repo, "checkout", "-q", "-b", "ai-runtime/220-windows-bridge")
    _commit(repo, ahead)
    _git(repo, "checkout", "-q", "main")
    _commit(repo)                       # main has its own commit: not an ancestor
    _git(repo, "checkout", "-q", "ai-runtime/220-windows-bridge")
    return repo


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("RUNTIME_STRICT_BASE_REF", raising=False)
    monkeypatch.delenv("RUNTIME_BASE_STALE_MAX", raising=False)
    monkeypatch.setenv("RUNTIME_BASE_REFS_CONFIG", str(tmp_path / "absent.yaml"))


def _config(tmp_path, monkeypatch, project, ref):
    p = tmp_path / "refs.yaml"
    p.write_text(f"projects:\n  {project}:\n    base_ref: {ref}\n")
    monkeypatch.setenv("RUNTIME_BASE_REFS_CONFIG", str(p))


# ── contract preserved ──────────────────────────────────────────────────────
def test_without_config_or_flag_the_legacy_guess_is_unchanged(tmp_path):
    repo = _task326_repo(tmp_path)
    for explicit in (None, "master", "nope"):
        got = git_write.resolve_job_base(str(repo), explicit)
        assert got["ref"] == git_write.resolve_base_branch(str(repo), explicit) == "main"
        assert got["source"] == "guess"
        assert got["sha"] == _git(repo, "rev-parse", "main")


def test_a_real_explicit_branch_still_wins(tmp_path):
    repo = _task326_repo(tmp_path)
    _git(repo, "branch", "feature-x", "main")
    got = git_write.resolve_job_base(str(repo), "feature-x")
    assert (got["ref"], got["source"]) == ("feature-x", "explicit")


def test_the_master_placeholder_is_not_an_explicit_choice(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    _git(repo, "branch", "master", "main")
    _config(tmp_path, monkeypatch, repo, "ai-runtime/220-windows-bridge")
    assert git_write.resolve_job_base(str(repo), "master")["source"] == "config"


# ── configured base_ref ─────────────────────────────────────────────────────
def test_the_configured_base_ref_beats_the_stale_guess_and_carries_its_sha(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    _config(tmp_path, monkeypatch, repo, "ai-runtime/220-windows-bridge")
    got = git_write.resolve_job_base(str(repo), "master")
    assert got == {"ref": "ai-runtime/220-windows-bridge",
                   "sha": _git(repo, "rev-parse", "HEAD"), "source": "config"}


def test_a_configured_sha_pins_the_base(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD~2")
    _config(tmp_path, monkeypatch, repo, sha)
    assert git_write.resolve_job_base(str(repo), None)["sha"] == sha


def test_a_configured_ref_that_does_not_resolve_blocks_never_falls_back(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    _config(tmp_path, monkeypatch, repo, "gone-branch")
    with pytest.raises(git_write.BaseRefBlocked, match="gone-branch"):
        git_write.resolve_job_base(str(repo), None)


def test_another_projects_config_does_not_apply(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    _config(tmp_path, monkeypatch, "/some/other/project", "ai-runtime/220-windows-bridge")
    assert git_write.resolve_job_base(str(repo), None)["ref"] == "main"


# ── strict mode ─────────────────────────────────────────────────────────────
def test_strict_blocks_the_task326_guess(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    monkeypatch.setenv("RUNTIME_STRICT_BASE_REF", "1")
    with pytest.raises(git_write.BaseRefBlocked, match="not an ancestor"):
        git_write.resolve_job_base(str(repo), "master")


def test_strict_blocks_a_guess_far_behind_head(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo)
    _git(repo, "checkout", "-q", "-b", "ai-runtime/live")
    _commit(repo, 4)
    monkeypatch.setenv("RUNTIME_STRICT_BASE_REF", "1")
    monkeypatch.setenv("RUNTIME_BASE_STALE_MAX", "3")
    with pytest.raises(git_write.BaseRefBlocked, match="4 commits behind"):
        git_write.resolve_job_base(str(repo), None)
    monkeypatch.setenv("RUNTIME_BASE_STALE_MAX", "4")
    assert git_write.resolve_job_base(str(repo), None)["ref"] == "main"


def test_strict_accepts_a_current_guess(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo)
    monkeypatch.setenv("RUNTIME_STRICT_BASE_REF", "1")
    assert git_write.resolve_job_base(str(repo), None)["ref"] == "main"


def test_strict_blocks_an_explicit_base_that_does_not_resolve(tmp_path, monkeypatch):
    repo = _task326_repo(tmp_path)
    monkeypatch.setenv("RUNTIME_STRICT_BASE_REF", "1")
    with pytest.raises(git_write.BaseRefBlocked, match="'nope'"):
        git_write.resolve_job_base(str(repo), "nope")


def test_the_shipped_config_names_no_project(monkeypatch):
    # Promoting this code must not change any live job's base by itself.
    monkeypatch.delenv("RUNTIME_BASE_REFS_CONFIG", raising=False)
    assert git_write.configured_base_ref("/root/ai-dev-runtime") is None


def test_the_read_only_report_names_the_task326_shape(tmp_path):
    import importlib.util
    import os
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "base_ref_report", os.path.join(root, "tools", "base_ref_report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = _task326_repo(tmp_path, ahead=3)
    before = _git(repo, "for-each-ref")
    r = mod.report(str(repo))
    assert r["legacy_guess"] == "main" and r["guess_behind_head"] == 3
    assert "not an ancestor" in r["strict_verdict"]
    assert _git(repo, "for-each-ref") == before                  # read-only
    assert mod.report(str(tmp_path))["error"] == "not a git repository"
