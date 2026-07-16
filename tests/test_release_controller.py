"""Release Controller: state, approval, duplicate protection, rollback,
service restart selection, health checks."""
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

# isolate release state per test session before importing the module
os.environ["RUNTIME_RELEASE_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="release-tests-"), "releases.db")

from core import release_controller as rc  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repo: main + a feature branch with one commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(str(r), "init", "-q", "-b", "main")
    _git(str(r), "config", "user.email", "t@t")
    _git(str(r), "config", "user.name", "t")
    (r / "app.py").write_text("v = 1\n")
    _git(str(r), "add", "app.py")
    _git(str(r), "commit", "-qm", "base")
    _git(str(r), "checkout", "-qb", "ai-runtime/feature", "main")
    (r / "app.py").write_text("v = 2\n")
    _git(str(r), "add", "app.py")
    _git(str(r), "commit", "-qm", "feature work")
    _git(str(r), "checkout", "-q", "main")
    return str(r)


@pytest.fixture(autouse=True)
def _fresh_db():
    rc.init_db()
    with rc._conn() as c:
        c.execute("DELETE FROM releases")
    yield


def _passing_tests():
    return {"ok": True, "cmd": "pytest", "returncode": 0, "output": "ok", "at": "now"}


def _candidate(repo, **kw):
    cand = rc.create_candidate(repo, "ai-runtime/feature", "main", **kw)
    rc._update(cand["id"], tests_before=_passing_tests())
    return rc.get(cand["id"])


def _approved(repo, **kw):
    cand = _candidate(repo, **kw)
    return rc.approve(repo, cand["id"], "operator", cand["head_sha"])


# --------------------------------------------------------------------------
# creation / state
# --------------------------------------------------------------------------

def test_create_records_shas_and_diff(repo):
    cand = rc.create_candidate(repo, "ai-runtime/feature", "main")
    assert cand["state"] == rc.CREATED
    assert cand["head_sha"] == _git(repo, "rev-parse", "ai-runtime/feature")
    assert cand["base_sha"] == _git(repo, "rev-parse", "main")
    assert "app.py" in cand["diff_files"]
    assert "app.py" in cand["diff_stat"]


def test_release_state_is_persistent(repo):
    cand = rc.create_candidate(repo, "ai-runtime/feature", "main")
    assert rc.get(cand["id"])["state"] == rc.CREATED  # survives a fresh read


def test_create_refuses_unknown_branch(repo):
    with pytest.raises(rc.ReleaseError):
        rc.create_candidate(repo, "ai-runtime/does-not-exist", "main")


def test_create_refuses_branch_identical_to_base(repo):
    _git(repo, "branch", "same-as-main", "main")
    with pytest.raises(rc.ReleaseError, match="nothing to release"):
        rc.create_candidate(repo, "same-as-main", "main")


def test_create_refuses_a_fallback_plan_only_branch(repo):
    """OWNER-111 shipped a Markdown plan reported as completed. A plan branch
    must never become a release candidate."""
    with pytest.raises(rc.ReleaseError, match="not releasable"):
        rc.create_candidate(repo, "ai-runtime/feature", "main", job_outcome="fallback_plan_only")


def test_create_accepts_an_implemented_branch(repo):
    cand = rc.create_candidate(repo, "ai-runtime/feature", "main", job_outcome="implemented")
    assert cand["state"] == rc.CREATED


def test_create_refuses_invalid_service_name(repo):
    with pytest.raises(rc.ReleaseError, match="invalid service"):
        rc.create_candidate(repo, "ai-runtime/feature", "main", service="evil; rm -rf /")


def test_create_refuses_second_live_candidate_for_same_branch(repo):
    rc.create_candidate(repo, "ai-runtime/feature", "main")
    with pytest.raises(rc.ReleaseError, match="already has a live release candidate"):
        rc.create_candidate(repo, "ai-runtime/feature", "main")


# --------------------------------------------------------------------------
# approval
# --------------------------------------------------------------------------

def test_release_refuses_unapproved_candidate(repo):
    cand = _candidate(repo)
    with pytest.raises(rc.ReleaseError, match="only an approved candidate"):
        rc.release(repo, cand["id"])
    # main must be untouched
    assert _git(repo, "rev-parse", "main") == cand["base_sha"]


def test_approve_requires_passing_tests(repo):
    cand = rc.create_candidate(repo, "ai-runtime/feature", "main")
    with pytest.raises(rc.ReleaseError, match="no passing test run"):
        rc.approve(repo, cand["id"], "operator", cand["head_sha"])


def test_approve_rejects_wrong_sha(repo):
    cand = _candidate(repo)
    with pytest.raises(rc.ReleaseError, match="does not match"):
        rc.approve(repo, cand["id"], "operator", "deadbeefdeadbeef")


def test_approve_rejects_moved_branch(repo):
    cand = _candidate(repo)
    _git(repo, "checkout", "-q", "ai-runtime/feature")
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("v = 3\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "moved")
    _git(repo, "checkout", "-q", "main")
    with pytest.raises(rc.ReleaseError, match="moved since the candidate was created"):
        rc.approve(repo, cand["id"], "operator", cand["head_sha"])


def test_approve_requires_an_approver(repo):
    cand = _candidate(repo)
    with pytest.raises(rc.ReleaseError, match="approver is required"):
        rc.approve(repo, cand["id"], "", cand["head_sha"])


def test_approval_records_who_and_what(repo):
    cand = _approved(repo)
    assert cand["state"] == rc.APPROVED
    assert cand["approved_by"] == "operator"
    assert cand["approved_sha"] == cand["head_sha"]


# --------------------------------------------------------------------------
# release: merge, retest, restart, health
# --------------------------------------------------------------------------

def test_successful_release_merges_and_restarts_only_that_service(repo):
    restarted = []
    cand = _approved(repo, service="ai-runtime.service", health_url="http://x/health")
    out = rc.release(repo, cand["id"],
                     restart=lambda s: (restarted.append(s), {"ok": True, "service": s})[1],
                     health=lambda url, **kw: {"ok": True, "status": 200},
                     run_tests_fn=_passing_tests)
    assert out["state"] == rc.RELEASED
    assert restarted == ["ai-runtime.service"]  # exactly one unit, not a blanket restart
    assert "v = 2" in open(os.path.join(repo, "app.py")).read()
    assert out["merge_sha"] == _git(repo, "rev-parse", "main")


def test_release_backs_up_main_before_merging(repo):
    cand = _approved(repo)
    base_before = cand["base_sha"]
    out = rc.release(repo, cand["id"], restart=lambda s: {"ok": True},
                     health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    assert out["backup_branch"].startswith("backup/main-")
    assert _git(repo, "rev-parse", out["backup_branch"]) == base_before


def test_release_without_service_does_not_restart_anything(repo):
    restarted = []
    cand = _approved(repo)
    out = rc.release(repo, cand["id"],
                     restart=lambda s: (restarted.append(s), {"ok": True})[1],
                     health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    assert out["state"] == rc.RELEASED
    assert restarted == []


def test_release_refuses_dirty_workspace(repo):
    cand = _approved(repo)
    with open(os.path.join(repo, "untracked.txt"), "w") as f:
        f.write("dirty")
    with pytest.raises(rc.ReleaseError, match="workspace is dirty"):
        rc.release(repo, cand["id"])


# --------------------------------------------------------------------------
# duplicate release protection
# --------------------------------------------------------------------------

def test_a_released_candidate_cannot_be_released_twice(repo):
    cand = _approved(repo)
    rc.release(repo, cand["id"], restart=lambda s: {"ok": True},
               health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    with pytest.raises(rc.ReleaseError, match="refusing duplicate release"):
        rc.release(repo, cand["id"])


def test_already_merged_branch_is_refused(repo):
    """A second candidate for a branch already merged into main must not merge again."""
    cand = _approved(repo)
    rc.release(repo, cand["id"], restart=lambda s: {"ok": True},
               health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    with pytest.raises(rc.ReleaseError, match="already released"):
        rc.create_candidate(repo, "ai-runtime/feature", "main")


def test_release_refuses_when_head_moved_after_approval(repo):
    cand = _approved(repo)
    _git(repo, "checkout", "-q", "ai-runtime/feature")
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("v = 99\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "sneaky post-approval commit")
    _git(repo, "checkout", "-q", "main")
    with pytest.raises(rc.ReleaseError, match="moved since approval"):
        rc.release(repo, cand["id"])
    assert rc.get(cand["id"])["state"] == rc.FAILED


# --------------------------------------------------------------------------
# automatic rollback
# --------------------------------------------------------------------------

def test_failed_post_merge_tests_roll_main_back(repo):
    cand = _approved(repo, service="ai-runtime.service")
    base_before = cand["base_sha"]
    out = rc.release(repo, cand["id"], restart=lambda s: {"ok": True},
                     health=lambda url, **kw: {"ok": True},
                     run_tests_fn=lambda: {"ok": False, "output": "2 failed"})
    assert out["state"] == rc.ROLLED_BACK
    assert _git(repo, "rev-parse", "main") == base_before
    assert "v = 1" in open(os.path.join(repo, "app.py")).read()


def test_failed_health_check_rolls_main_back_and_restarts(repo):
    restarts = []
    cand = _approved(repo, service="ai-runtime.service", health_url="http://x/health")
    base_before = cand["base_sha"]
    out = rc.release(repo, cand["id"],
                     restart=lambda s: (restarts.append(s), {"ok": True})[1],
                     health=lambda url, **kw: {"ok": False, "error": "connection refused"},
                     run_tests_fn=_passing_tests)
    assert out["state"] == rc.ROLLED_BACK
    assert _git(repo, "rev-parse", "main") == base_before
    # restarted once for the release, once to restore the rolled-back code
    assert restarts == ["ai-runtime.service", "ai-runtime.service"]


def test_failed_restart_rolls_main_back(repo):
    cand = _approved(repo, service="ai-runtime.service")
    base_before = cand["base_sha"]
    out = rc.release(repo, cand["id"],
                     restart=lambda s: {"ok": False, "output": "unit failed"},
                     health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    assert out["state"] == rc.ROLLED_BACK
    assert _git(repo, "rev-parse", "main") == base_before


def test_manual_rollback_restores_backup(repo):
    cand = _approved(repo, service="ai-runtime.service")
    base_before = cand["base_sha"]
    rc.release(repo, cand["id"], restart=lambda s: {"ok": True},
               health=lambda url, **kw: {"ok": True}, run_tests_fn=_passing_tests)
    assert _git(repo, "rev-parse", "main") != base_before
    out = rc.rollback(repo, cand["id"], restart=lambda s: {"ok": True})
    assert out["state"] == rc.ROLLED_BACK
    assert _git(repo, "rev-parse", "main") == base_before


# --------------------------------------------------------------------------
# health check
# --------------------------------------------------------------------------

def test_health_check_gives_up_after_timeout():
    slept = []
    out = rc.health_check("http://127.0.0.1:1/health", timeout=0, sleep=slept.append)
    assert out["ok"] is False


def test_restart_service_rejects_injection():
    with pytest.raises(rc.ReleaseError, match="invalid service"):
        rc.restart_service("ai-runtime.service; rm -rf /")


def test_verify_detects_head_drift(repo):
    cand = _candidate(repo)
    assert rc.verify(repo, cand["id"])["ok"]
    _git(repo, "checkout", "-q", "ai-runtime/feature")
    with open(os.path.join(repo, "app.py"), "w") as f:
        f.write("v = 4\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "drift")
    _git(repo, "checkout", "-q", "main")
    assert not rc.verify(repo, cand["id"])["ok"]


def test_no_branch_is_released_without_an_explicit_candidate(repo):
    """There is no API that releases a branch by name: a release always needs a
    candidate id that an operator created and approved."""
    with pytest.raises(rc.ReleaseError, match="unknown release candidate"):
        rc.release(repo, "rc-does-not-exist")
