"""Projects on owner-allowed repositories, and the owner's notice when a project finishes.

The source repository is never written: the project gets its own copy of the source's
committed HEAD on branch fullcycle/<id>, with no remote. Payment paths and the live tree
are refused whatever the owner's list says.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from core.fullcycle import engine as fe
from core.fullcycle import intake
from core.fullcycle import repos
from core.fullcycle import spec as specs

BY = "api:client/chatgpt-tech-lead"
SPEC = {"title": "calc", "goal": "a calculator", "acceptance_criteria": ["adds"],
        "tasks": [{"key": "add", "title": "add", "goal": "add()", "acceptance_criteria": ["ok"],
                   "checks": [["python3", "-m", "pytest", "-q", "tests"]]}]}


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c",
                           "user.email=t@t", *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "fc"
    root.mkdir()
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", str(root))
    monkeypatch.setenv("FULLCYCLE_REPOS_FILE", str(tmp_path / "etc" / "repos.json"))
    monkeypatch.delenv("FULLCYCLE_INTAKE_MAX_ACTIVE", raising=False)
    return root


@pytest.fixture
def src(tmp_path):
    s = tmp_path / "work" / "shop"
    s.mkdir(parents=True)
    _git(s, "init", "-q", "-b", "main")
    (s / "app.py").write_text("x = 1\n")
    _git(s, "add", "app.py")
    _git(s, "commit", "-q", "-m", "first")
    _git(s, "remote", "add", "origin", "https://example.invalid/shop.git")
    return s


# -- the owner's list --------------------------------------------------------------------
def test_allow_then_get_and_the_listing_shows_no_paths(box, src):
    repos.allow("shop", str(src))
    assert repos.get("shop") == {"name": "shop", "path": str(src), "seed": []}
    assert repos.listing() == [{"repo": "shop", "usable": True}]
    assert oct(os.stat(repos.repos_file()).st_mode & 0o777) == "0o600"


def test_a_repo_not_in_the_list_is_refused(box, src):
    with pytest.raises(repos.RepoError, match="repo_not_allowed"):
        repos.get("shop")


@pytest.mark.parametrize("word", ["payment-orchestrator", "payorch-ha", "PayOrch"])
def test_money_dirs_by_name_are_refused(box, tmp_path, word):
    p = tmp_path / word
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "commit", "-q", "--allow-empty", "-m", "a")
    with pytest.raises(repos.RepoError, match="payment"):
        repos.allow("pay", str(p))


def test_the_policy_hard_deny_dirs_are_refused(box, src, monkeypatch):
    from core import agent_creation_policy as acp
    monkeypatch.setattr(acp, "load_config", lambda *a, **k: {"hard_deny_dirs": [str(src)]})
    assert repos.check_source(str(src)) == "payment_hard_denied"


def test_the_live_tree_is_refused(box):
    assert repos.check_source("/root/ai-dev-runtime") is not None


def test_symlink_non_git_no_commit_and_intake_root_are_refused(box, src, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(src)
    assert repos.check_source(str(link)) == "repo_path_is_symlink"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repos.check_source(str(plain)) == "repo_not_a_git_checkout"
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert repos.check_source(str(empty)) == "repo_has_no_commit"
    assert repos.check_source(str(box)) == "repo_overlaps_intake_root"
    assert repos.check_source("relative/path") == "repo_path_not_absolute"


def test_an_unsafe_list_file_counts_as_empty(box, src):
    repos.allow("shop", str(src))
    os.chmod(repos.repos_file(), 0o666)
    assert repos.load() == {}
    with pytest.raises(repos.RepoError, match="unsafe_repos_file"):
        repos.allow("other", str(src))


def test_a_hand_edited_entry_is_checked_again_when_used(box, tmp_path):
    p = tmp_path / "payments"
    p.mkdir()
    os.makedirs(os.path.dirname(repos.repos_file()), exist_ok=True)
    with open(repos.repos_file(), "w") as f:
        json.dump({"repos": {"pay": {"path": str(p)}}}, f)
    os.chmod(repos.repos_file(), 0o600)
    with pytest.raises(repos.RepoError, match="repo_refused:pay:repo_payment_denied"):
        repos.get("pay")
    with pytest.raises(specs.SpecError, match="repo_refused"):
        intake.start("calc", SPEC, by=BY, repo="pay")
    assert os.listdir(box) == []


# -- a project on an allowed repo --------------------------------------------------------
def test_start_copies_the_committed_head_without_a_remote(box, src):
    (src / "app.py").write_text("x = 2  # uncommitted\n")
    refs_before = _git(src, "show-ref")
    repos.allow("shop", str(src))
    r = intake.start("calc", SPEC, by=BY, repo="shop")
    path = box / "calc"
    head = _git(src, "rev-parse", "HEAD")
    assert r["created"] and r["repo"] == "shop" and r["branch"] == "fullcycle/calc"
    assert r["base_sha"] == head
    assert _git(path, "rev-parse", "HEAD") == head
    assert _git(path, "branch", "--show-current") == "fullcycle/calc"
    assert _git(path, "remote") == ""
    assert (path / "app.py").read_text() == "x = 1\n"
    assert os.path.isdir(path / ".git") and not os.path.islink(path / ".git")
    assert ".fullcycle/" in (path / ".git" / "info" / "exclude").read_text()
    assert _git(src, "show-ref") == refs_before
    assert intake.source_of("calc")["source"] == str(src)


def test_a_new_spec_version_cannot_switch_the_source(box, src, tmp_path):
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY, repo="shop")
    other = tmp_path / "work" / "blog"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "commit", "-q", "--allow-empty", "-m", "a")
    repos.allow("blog", str(other))
    with pytest.raises(specs.SpecError, match="another_source"):
        intake.start("calc", SPEC, by=BY, repo="blog")
    assert intake.start("calc", SPEC, by=BY, repo="shop")["created"] is False
    assert intake.start("calc", SPEC, by=BY)["created"] is False


def test_a_new_repo_project_cannot_be_turned_into_a_copy(box, src):
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY)
    with pytest.raises(specs.SpecError, match="another_source"):
        intake.start("calc", SPEC, by=BY, repo="shop")


def test_a_failed_copy_leaves_nothing_behind(box, src, monkeypatch):
    repos.allow("shop", str(src))

    def boom(*a, **k):
        raise repos.RepoError("repo_head_moved_during_copy")
    monkeypatch.setattr(repos, "make_copy", boom)
    with pytest.raises(specs.SpecError, match="repo_copy_failed"):
        intake.start("calc", SPEC, by=BY, repo="shop")
    assert os.listdir(box) == []
    assert specs.get_project("calc") is None


def test_the_copy_refuses_when_the_source_moved(box, src, monkeypatch):
    real = repos._git

    def moved(cwd, *args, **kw):
        out = real(cwd, *args, **kw)
        return "0" * 40 if args[:2] == ("rev-parse", "FETCH_HEAD") else out
    monkeypatch.setattr(repos, "_git", moved)
    repos.allow("shop", str(src))
    with pytest.raises(specs.SpecError, match="repo_head_moved"):
        intake.start("calc", SPEC, by=BY, repo="shop")
    assert os.listdir(box) == []


# -- the owner's notice ------------------------------------------------------------------
class _Send:
    def __init__(self, results=None):
        self.texts, self.results = [], list(results or [])

    def __call__(self, text):
        self.texts.append(text)
        return self.results.pop(0) if self.results else (True, "telegram:1", None)


def _finish(pid, states, monkeypatch, conn=None):
    conn, own = fe.connect(conn)
    conn.execute("INSERT OR IGNORE INTO fc_project_done VALUES (?,?,?,?)", (pid, 1, "", 0))
    conn.commit()
    if own:
        conn.close()
    real = fe.project_status

    def status(project_id, conn=None):
        if project_id in states:
            return {"tasks": [{"key": k, "state": s} for k, s in states[project_id].items()]}
        return real(project_id, conn=conn)
    monkeypatch.setattr(fe, "project_status", status)


def test_the_owner_hears_once_when_a_copy_project_finishes(box, src, monkeypatch):
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY, repo="shop")
    intake.notify_finished(send=_Send())                       # table exists before the end
    path = box / "calc"
    (path / "add.py").write_text("def add(a, b):\n    return a + b\n")
    _git(path, "add", "add.py")
    _git(path, "commit", "-q", "-m", "fullcycle: add (attempt 1)")
    _finish("calc", {"calc": {"add": "VERIFIED_DONE"}}, monkeypatch)
    send = _Send()
    out = intake.notify_finished(send=send, now=1000.0)
    assert out == [{"project": "calc", "state": "sent", "attempts": 1}]
    text = send.texts[0]
    assert "calc finished" in text and "add: VERIFIED_DONE" in text
    assert "fullcycle: add (attempt 1)" in text and "first" not in text
    assert f"git -C {src} fetch {path} fullcycle/calc" in text
    assert "Nothing was pushed" in text
    assert intake.notify_finished(send=send, now=5000.0) == []
    assert len(send.texts) == 1


def test_a_new_repo_project_notice_lists_its_commits(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    intake.notify_finished(send=_Send())
    path = box / "calc"
    (path / "a.py").write_text("a = 1\n")
    _git(path, "add", "a.py")
    _git(path, "commit", "-q", "-m", "fullcycle: add (attempt 2)")
    _finish("calc", {"calc": {"add": "VERIFIED_DONE", "b": "CANCELLED"}}, monkeypatch)
    send = _Send()
    intake.notify_finished(send=send)
    assert "fullcycle: add (attempt 2)" in send.texts[0]
    assert "baseline" not in send.texts[0]
    assert send.texts[0].startswith("⚠️") and "1 cancelled" in send.texts[0]
    assert "Take it" not in send.texts[0]


def test_no_notice_while_a_task_is_in_flight_or_the_project_is_open(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    intake.start("other", SPEC, by=BY)
    intake.notify_finished(send=_Send())
    _finish("calc", {"calc": {"add": "VERIFYING"}}, monkeypatch)
    send = _Send()
    assert intake.notify_finished(send=send) == [] and send.texts == []


def test_projects_finished_before_the_notice_existed_stay_quiet(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    _finish("calc", {"calc": {"add": "VERIFIED_DONE"}}, monkeypatch)
    send = _Send()
    assert intake.notify_finished(send=send) == [] and send.texts == []


def test_a_failed_send_is_retried_then_given_up(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    intake.notify_finished(send=_Send())
    _finish("calc", {"calc": {"add": "VERIFIED_DONE"}}, monkeypatch)
    send = _Send([(False, None, "telegram rejected")] * 10)
    t = 1000.0
    states = []
    for _ in range(intake.NOTICE_MAX_ATTEMPTS + 2):
        states += [n["state"] for n in intake.notify_finished(send=send, now=t)]
        assert intake.notify_finished(send=send, now=t + 1) == []   # spaced, not hammered
        t += intake.NOTICE_RETRY_SECS + 1
    assert states == ["retry"] * (intake.NOTICE_MAX_ATTEMPTS - 1) + ["gave_up"]


def test_a_send_that_raises_does_not_escape(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    intake.notify_finished(send=_Send())
    _finish("calc", {"calc": {"add": "VERIFIED_DONE"}}, monkeypatch)

    def boom(text):
        raise OSError("network down")
    out = intake.notify_finished(send=boom)
    assert out[0]["state"] == "retry" and "network down" in out[0]["error"]


def test_the_worker_round_never_raises(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fcw", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "fullcycle_worker.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)

    class Broken:
        def notify_finished(self):
            raise RuntimeError("db locked")
    w.notify_round(Broken())


# -- the owner's command line ------------------------------------------------------------
def test_owner_cli_allow_list_remove(box, src, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fcr", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tools", "fullcycle_repo.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    if os.geteuid() != 0:
        pytest.skip("allow needs root")
    assert cli.main(["allow", "shop", str(src)]) == 0
    assert cli.main(["list"]) == 0
    assert f"shop\t{src}\tok" in capsys.readouterr().out
    assert cli.main(["allow", "pay", "/opt/payment-orchestrator"]) == 1
    assert cli.main(["remove", "shop"]) == 0
    assert repos.load() == {}


def test_no_notice_until_the_engine_marks_the_project_done(box, monkeypatch):
    """Every task can be terminal while the project's own acceptance has not passed."""
    intake.start("calc", SPEC, by=BY)
    intake.notify_finished(send=_Send())
    monkeypatch.setattr(fe, "project_status",
                        lambda pid, conn=None: {"tasks": [{"key": "add",
                                                           "state": "VERIFIED_DONE"}]})
    send = _Send()
    assert intake.notify_finished(send=send) == [] and send.texts == []


def test_no_root_git_on_a_repo_under_agent_run_as(box, src, monkeypatch):
    from core import agent_privilege as priv
    repos.allow("shop", str(src))
    monkeypatch.setattr(priv, "target", lambda: priv.Target("agent", 1000, 1000, "/home/a"))
    ran = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or real(*a, **k))
    assert repos.check_source(str(src)) == "repo_root_git_off"
    with pytest.raises(specs.SpecError):
        intake.start("calc", SPEC, by=BY, repo="shop")
    assert not [a for a in ran if a and a[0] and a[0][0] == "git"]
    assert os.listdir(box) == []


# -- the sandboxed worker's projects belong to its agent user ----------------------------
needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="chown needs root")


@needs_root
def test_a_new_tree_is_handed_to_the_agent_user_whole(box, src, monkeypatch):
    import pwd
    n = pwd.getpwnam("nobody")
    os.symlink("/etc/passwd", src / "passwd-link")
    _git(src, "add", "passwd-link")
    _git(src, "commit", "-q", "-m", "a link")
    monkeypatch.setenv("FULLCYCLE_INTAKE_RUN_AS", "nobody")
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY, repo="shop")
    intake.start("plain", SPEC, by=BY)
    for root in (box / "calc", box / "plain"):
        for top, dirs, files in os.walk(root):
            for name in [top] + [os.path.join(top, x) for x in dirs + files]:
                assert os.lstat(name).st_uid == n.pw_uid, name
    assert os.stat("/etc/passwd").st_uid == 0          # the link was not followed
    assert os.lstat(src / "app.py").st_uid == 0          # the source stays root's


@pytest.mark.parametrize("who,err", [("no-such-user-x", "unknown_user"), ("root", "is_root")])
def test_a_bad_agent_user_refuses_before_anything_is_made(box, monkeypatch, who, err):
    monkeypatch.setenv("FULLCYCLE_INTAKE_RUN_AS", who)
    with pytest.raises(specs.SpecError, match=err):
        intake.start("calc", SPEC, by=BY)
    assert os.listdir(box) == []


def test_unset_leaves_trees_as_they_were(box, monkeypatch):
    monkeypatch.delenv("FULLCYCLE_INTAKE_RUN_AS", raising=False)
    intake.start("calc", SPEC, by=BY)
    assert os.lstat(box / "calc").st_uid == os.geteuid()


def test_the_notice_runs_git_as_the_agent_in_a_sandboxed_worker(box, monkeypatch):
    from core import agent_privilege as priv
    t = priv.Target("nobody", 65534, 65534, "/nonexistent")
    monkeypatch.setattr(priv, "target", lambda: t)
    seen = []

    class R:
        returncode, stdout = 0, "abc"
    monkeypatch.setattr(intake.subprocess, "run", lambda argv, **k: seen.append(argv) or R())
    intake._git_out("/x", "rev-parse", "HEAD")
    assert seen[0][0].endswith("setpriv") or seen[0][0].endswith("unshare")
    assert "--reuid=65534" in seen[0] and seen[0][-3:] == ["--no-pager", "rev-parse", "HEAD"]


def test_the_notice_runs_no_git_when_the_setting_is_broken(box, monkeypatch):
    from core import agent_privilege as priv

    def broken():
        raise priv.AgentPrivilegeError("pin_unsafe")
    monkeypatch.setattr(priv, "target", broken)
    monkeypatch.setattr(intake.subprocess, "run", lambda *a, **k: pytest.fail("ran git"))
    assert intake._git_out("/x", "log") == ""


@needs_root
def test_the_hand_over_does_not_walk_through_a_directory_link(box, src, tmp_path, monkeypatch):
    outside = tmp_path / "root-only"
    outside.mkdir()
    (outside / "keep.txt").write_text("root's\n")
    os.symlink(str(outside), src / "dir-link")
    _git(src, "add", "dir-link")
    _git(src, "commit", "-q", "-m", "a dir link")
    monkeypatch.setenv("FULLCYCLE_INTAKE_RUN_AS", "nobody")
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY, repo="shop")
    assert os.path.islink(box / "calc" / "dir-link")
    assert os.lstat(outside).st_uid == 0 and os.lstat(outside / "keep.txt").st_uid == 0


# -- integration: other projects' branches brought into a new copy ----------------------
def _project_commit(box, pid, fname, text):
    path = box / pid
    (path / fname).write_text(text)
    _git(path, "add", fname)
    _git(path, "commit", "-q", "-m", f"{pid}: {fname}")
    return _git(path, "rev-parse", "HEAD")


def test_an_integration_copy_holds_each_projects_branch(box, src, monkeypatch):
    monkeypatch.setenv("FULLCYCLE_INTAKE_MAX_ACTIVE", "10")
    repos.allow("shop", str(src))
    intake.start("calc-a", SPEC, by=BY, repo="shop")
    intake.start("calc-b", SPEC, by=BY, repo="shop")
    a = _project_commit(box, "calc-a", "a.py", "a = 1\n")
    b = _project_commit(box, "calc-b", "b.py", "b = 2\n")
    r = intake.start("calc-all", SPEC, by=BY, repo="shop", merge_from=["calc-a", "calc-b"])
    assert r["merge_branches"] == ["fullcycle-in/calc-a", "fullcycle-in/calc-b"]
    path = box / "calc-all"
    assert _git(path, "rev-parse", "fullcycle-in/calc-a") == a
    assert _git(path, "rev-parse", "fullcycle-in/calc-b") == b
    assert _git(path, "branch", "--show-current") == "fullcycle/calc-all"
    assert not (path / "a.py").exists()              # brought in, not merged: that is a task
    assert _git(path, "remote") == ""


@pytest.mark.parametrize("merge_from,err", [
    (["calc"], "distinct other"), (["nope-x"], "merge_from_unknown_project"),
    (["calc-a", "calc-a"], "merge_from"), (["Bad"], "merge_from"),
    ([f"p{i:03d}" for i in range(intake.MERGE_FROM_MAX + 1)], "merge_from")])
def test_bad_merge_lists_refuse_before_anything_is_made(box, src, merge_from, err):
    repos.allow("shop", str(src))
    intake.start("calc-a", SPEC, by=BY, repo="shop")
    before = sorted(os.listdir(box))
    with pytest.raises(specs.SpecError, match=err):
        intake.start("calc", SPEC, by=BY, repo="shop", merge_from=merge_from)
    assert sorted(os.listdir(box)) == before


def test_merge_from_needs_the_same_repo_and_a_new_project(box, src, tmp_path, monkeypatch):
    monkeypatch.setenv("FULLCYCLE_INTAKE_MAX_ACTIVE", "10")
    repos.allow("shop", str(src))
    other = tmp_path / "work" / "blog"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "commit", "-q", "--allow-empty", "-m", "a")
    repos.allow("blog", str(other))
    intake.start("calc-a", SPEC, by=BY, repo="shop")
    intake.start("blog-a", SPEC, by=BY, repo="blog")
    intake.start("plain", SPEC, by=BY)
    with pytest.raises(specs.SpecError, match="merge_from_other_repo"):
        intake.start("calc-all", SPEC, by=BY, repo="shop", merge_from=["blog-a"])
    with pytest.raises(specs.SpecError, match="merge_from_unknown_project"):
        intake.start("calc-all", SPEC, by=BY, repo="shop", merge_from=["plain"])
    with pytest.raises(specs.SpecError, match="merge_from_needs_repo"):
        intake.start("calc-all", SPEC, by=BY, merge_from=["calc-a"])
    with pytest.raises(specs.SpecError, match="only_when_the_project_is_made"):
        intake.start("calc-a", SPEC, by=BY, repo="shop", merge_from=["blog-a"])


@needs_root
def test_an_agent_owned_branch_is_read_by_the_agent_inside_its_sandbox(box, src, tmp_path,
                                                                       monkeypatch):
    """Root never opens the agent's repository: the bundle comes from the agent's side."""
    import pwd
    import shutil as _sh
    if not (_sh.which("unshare") and _sh.which("setpriv")):
        pytest.skip("needs unshare and setpriv")
    os.chmod(tmp_path, 0o755)
    home = tmp_path / "nobody-home"
    home.mkdir()
    real = pwd.getpwuid

    class P:
        def __init__(self, p):
            self.pw_name, self.pw_uid, self.pw_gid, self.pw_dir = (p.pw_name, p.pw_uid,
                                                                   p.pw_gid, str(home))
    monkeypatch.setattr(pwd, "getpwuid",
                        lambda uid: P(real(uid)) if uid == real(65534).pw_uid else real(uid))
    monkeypatch.setenv("FULLCYCLE_INTAKE_RUN_AS", "nobody")
    repos.allow("shop", str(src))
    intake.start("calc-a", SPEC, by=BY, repo="shop")
    for d in (tmp_path, box):
        os.chmod(d, 0o755)
    ran = []
    real_popen = repos.subprocess.Popen

    def spy(argv, **kw):
        ran.append(list(argv))
        return real_popen(argv, **kw)
    monkeypatch.setattr(repos.subprocess, "Popen", spy)
    intake.start("calc-all", SPEC, by=BY, repo="shop", merge_from=["calc-a"])
    (bundle,) = [a for a in ran if "bundle" in a]
    assert bundle[0].endswith("unshare") and "--reuid=65534" in bundle
    assert "core.hooksPath=/dev/null" in bundle
    got = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(box / "calc-all"),
                          "rev-parse", "--verify", "fullcycle-in/calc-a"],
                         capture_output=True, text=True)
    assert got.returncode == 0 and got.stdout.strip()
    assert os.lstat(box / "calc-all").st_uid == 65534          # handed over after the merge


# -- seed: build output the tests need, copied into every copy --------------------------
def _with_release(src):
    (src / ".gitignore").write_text("release/\n")
    _git(src, "add", ".gitignore")
    _git(src, "commit", "-q", "-m", "ignore release")
    rel = src / "release"
    (rel / "sub").mkdir(parents=True)
    (rel / "PROVENANCE.json").write_text('{"v": 1}\n')
    (rel / "sub" / "pkg.zip").write_bytes(b"PK\x03\x04data")
    return rel


def test_a_seeded_path_lands_in_every_copy(box, src):
    _with_release(src)
    repos.allow("shop", str(src), seed=["release"])
    intake.start("calc", SPEC, by=BY, repo="shop")
    path = box / "calc"
    assert (path / "release" / "PROVENANCE.json").read_text() == '{"v": 1}\n'
    assert (path / "release" / "sub" / "pkg.zip").read_bytes() == b"PK\x03\x04data"
    assert _git(path, "status", "--porcelain") == ""          # ignored, not a change


def test_links_in_a_seed_are_not_copied(box, src):
    rel = _with_release(src)
    os.symlink("/etc/passwd", rel / "passwd")
    os.symlink("/etc", rel / "etc-dir")
    repos.allow("shop", str(src), seed=["release"])
    intake.start("calc", SPEC, by=BY, repo="shop")
    path = box / "calc" / "release"
    assert not os.path.lexists(path / "passwd") and not os.path.lexists(path / "etc-dir")


@pytest.mark.parametrize("bad,err", [("../etc", "seed_path_bad"), ("/etc", "seed_path_bad"),
                                     (".git", "seed_path_bad"), ("nope", "seed_path_missing"),
                                     ("a b", "seed_path_bad")])
def test_bad_seed_paths_are_refused_when_allowed(box, src, bad, err):
    with pytest.raises(repos.RepoError, match=err):
        repos.allow("shop", str(src), seed=[bad])


def test_a_seed_through_a_link_is_refused(box, src, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, src / "release")
    with pytest.raises(repos.RepoError, match="link"):
        repos.allow("shop", str(src), seed=["release"])


def test_a_seed_over_the_cap_refuses_and_leaves_nothing(box, src, monkeypatch):
    _with_release(src)
    repos.allow("shop", str(src), seed=["release"])
    monkeypatch.setattr(repos, "SEED_MAX_BYTES", 5)
    with pytest.raises(specs.SpecError, match="seed_too_big"):
        intake.start("calc", SPEC, by=BY, repo="shop")
    assert os.listdir(box) == []
