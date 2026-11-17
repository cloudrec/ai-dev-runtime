"""The owner's phone page and the "what needs you" Telegram list."""
from __future__ import annotations

import os
import subprocess

import pytest

from core import loop_watch as lw
from core.fullcycle import engine as fe
from core.fullcycle import intake, owner_page, repos
from core.fullcycle import release as rel

BY = "api:client/chatgpt-tech-lead"
SPEC = {"title": "calc", "goal": "g", "acceptance_criteria": ["ok"],
        "tasks": [{"key": "add", "title": "add", "goal": "add()", "acceptance_criteria": ["ok"]}]}


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "fc"
    root.mkdir()
    for k, v in {"CONTROL_PLANE_DB": tmp_path / "cp.db", "AGENT_CONTROL_DB": tmp_path / "ac.db",
                 "FULLCYCLE_INTAKE_ROOT": root,
                 "FULLCYCLE_REPOS_FILE": tmp_path / "etc" / "repos.json",
                 "FULLCYCLE_RELEASE_KEY_FILE": tmp_path / "rel.key",
                 "FULLCYCLE_OWNER_PAGE_KEY_FILE": tmp_path / "page.key"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("FULLCYCLE_INTAKE_MAX_ACTIVE", "10")
    monkeypatch.setenv("FULLCYCLE_RELEASE_URL_BASE", "https://ex.test/r")
    monkeypatch.setenv("FULLCYCLE_OWNER_PAGE_BASE", "https://ex.test/p")
    monkeypatch.delenv("FULLCYCLE_INTAKE_RUN_AS", raising=False)
    (tmp_path / "rel.key").write_text(os.urandom(32).hex())
    os.chmod(tmp_path / "rel.key", 0o600)
    src = tmp_path / "work" / "shop"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "commit", "-q", "--allow-empty", "-m", "first")
    repos.allow("shop", str(src))
    return root, src


def _finish(root, pid, merge_from=None):
    intake.start(pid, SPEC, by=BY, repo="shop", merge_from=merge_from)
    (root / pid / f"{pid}.py").write_text("x = 1\n")
    _git(root / pid, "add", ".")
    _git(root / pid, "commit", "-q", "-m", f"work {pid}")
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES (?,?,?,?)", (pid, 1, "", 5.0))
    conn.commit()
    conn.close()


@pytest.fixture
def done_status(monkeypatch):
    real = fe.project_status

    def st(pid, conn=None):
        s = real(pid, conn=conn)
        s["tasks"] = [{"key": "add", "state": "VERIFIED_DONE", "attempt": 1, "origin": "spec",
                       "last_reason": ""}]
        return s
    monkeypatch.setattr(fe, "project_status", st)


def test_the_key_is_root_only_and_rotation_kills_the_old_link(env):
    owner_page.rotate()
    first = owner_page.url()
    assert first.startswith("https://ex.test/p/") and len(first.rsplit("/", 1)[1]) >= 32
    owner_page.check(first.rsplit("/", 1)[1])
    owner_page.rotate()
    with pytest.raises(owner_page.PageError):
        owner_page.check(first.rsplit("/", 1)[1])
    os.chmod(os.environ["FULLCYCLE_OWNER_PAGE_KEY_FILE"], 0o644)
    assert owner_page.secret() is None and owner_page.url() is None


def test_a_finished_repo_project_is_releasable_then_released_from_the_page(env, done_status):
    root, src = env
    _finish(root, "calc")
    key = owner_page.rotate()
    snap = owner_page.snapshot()
    (p,) = snap["projects"]
    assert p["releasable"] and snap["todo"][0]["key"] == "release:calc"
    r = owner_page.release_from_page(key, "calc")
    assert r["ok"] and _git(src, "log", "-1", "--format=%s") == "work calc"
    (p,) = owner_page.snapshot()["projects"]
    assert not p["releasable"] and p["released"]["into"] == str(src)
    assert owner_page.todo() == []


def test_a_wrong_key_does_nothing(env, done_status):
    root, src = env
    _finish(root, "calc")
    owner_page.rotate()
    before = _git(src, "rev-parse", "HEAD")
    with pytest.raises(owner_page.PageError):
        owner_page.release_from_page("x" * 43, "calc")
    with pytest.raises(owner_page.PageError):
        owner_page.dismiss_from_page("x" * 43, "calc")
    assert _git(src, "rev-parse", "HEAD") == before


def test_dismissed_and_integrated_projects_leave_the_list(env, done_status):
    root, _ = env
    _finish(root, "calc-a")
    _finish(root, "calc-b")
    _finish(root, "calc-c")
    key = owner_page.rotate()
    owner_page.dismiss_from_page(key, "calc-c")
    _finish(root, "calc-all", merge_from=["calc-a", "calc-b"])
    (root / "calc-all" / "m.py").write_text("m = 1\n")
    rel_ok = owner_page.release_from_page(key, "calc-all")
    assert rel_ok["ok"], rel_ok
    by = {p["project"]: p for p in owner_page.snapshot()["projects"]}
    assert by["calc-a"]["in_released_integration"] and not by["calc-a"]["releasable"]
    assert by["calc-c"]["dismissed"] and not by["calc-c"]["releasable"]
    assert owner_page.todo() == []


def test_dismiss_finished_clears_everything_releasable_now(env, done_status):
    root, _ = env
    _finish(root, "calc-a")
    _finish(root, "calc-b")
    assert sorted(owner_page.dismiss_finished()) == ["calc-a", "calc-b"]
    assert owner_page.todo() == []


def test_the_digest_goes_out_when_the_list_changes_and_only_then(env, done_status):
    root, _ = env
    owner_page.rotate()
    conn, _ = fe.connect()
    told = []
    assert lw.owner_digest(conn, told.append, 1000.0, []) is None      # nothing, never sent
    _finish(root, "calc")
    t = lw.owner_digest(conn, told.append, 2000.0, [])
    assert t and "готово к выпуску: calc" in t and "https://ex.test/p/" in t
    assert lw.owner_digest(conn, told.append, 2100.0, []) is None      # same list
    owner_page.dismiss("calc", by="test")
    assert lw.owner_digest(conn, told.append, 2000.0 + 10, []) is None  # inside the gap
    t = lw.owner_digest(conn, told.append, 2000.0 + lw.DIGEST_GAP + 1, [])
    assert t and "ничего не нужно" in t
    assert lw.owner_digest(conn, told.append, 9000.0, []) is None
    conn.close()


def test_an_unsent_digest_is_tried_again(env, done_status):
    root, _ = env
    _finish(root, "calc")
    conn, _ = fe.connect()
    refused = lambda m: (False, None, "telegram down")
    assert lw.owner_digest(conn, refused, 1000.0, []) is None
    told = []
    assert lw.owner_digest(conn, told.append, 1001.0, []) is not None
    conn.close()


# ── publish: Push or PR after a release ─────────────────────────────────────────────
@pytest.fixture
def released(env, done_status, tmp_path):
    root, src = env
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(src, "remote", "add", "origin", str(origin))
    _git(src, "push", "-q", "origin", "main")
    _finish(root, "calc")
    key = owner_page.rotate()
    assert owner_page.release_from_page(key, "calc")["ok"]
    return src, origin, key


def test_push_sends_the_released_commit_and_never_forces(released):
    src, origin, key = released
    (p,) = owner_page.snapshot()["projects"]
    assert p["published"] == {"mode": None}
    r = owner_page.publish_from_page(key, "calc", "push")
    assert r["ok"], r
    assert _git(origin, "rev-parse", "main") == _git(src, "rev-parse", "main")
    (p,) = owner_page.snapshot()["projects"]
    assert p["published"]["mode"] == "push"
    again = owner_page.publish_from_page(key, "calc", "push")
    assert again["ok"] and again.get("note") == "already there"


def test_push_refuses_when_origin_has_other_commits(released, tmp_path):
    src, origin, key = released
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(other)], check=True)
    (other / "theirs.py").write_text("t = 1\n")
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "someone else")
    _git(other, "push", "-q", "origin", "main")
    theirs = _git(origin, "rev-parse", "main")
    r = owner_page.publish_from_page(key, "calc", "push")
    assert r["ok"] is False and "lacks" in r["reason"]
    assert _git(origin, "rev-parse", "main") == theirs


def test_pr_pushes_a_branch_and_asks_gh(released, monkeypatch):
    src, origin, key = released
    real = subprocess.run
    seen = []

    def run(argv, **kw):
        if argv[0] == "gh":
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "https://github.com/x/y/pull/7\n", "")
        return real(argv, **kw)
    r = rel.publish("calc", "pr", run=run)
    assert r["ok"] and r["url"].endswith("/pull/7") and r["ref"] == "owneros/calc"
    assert _git(origin, "rev-parse", "owneros/calc") == _git(src, "rev-parse", "main")
    assert seen[0][:3] == ["gh", "pr", "create"] and "--base" in seen[0]
    assert _git(origin, "rev-parse", "main") != _git(src, "rev-parse", "main")  # main untouched


def test_nothing_is_published_before_a_release(env, done_status):
    root, _ = env
    _finish(root, "calc")
    key = owner_page.rotate()
    r = owner_page.publish_from_page(key, "calc", "push")
    assert r["ok"] is False and r["reason"] == "not_released_yet"


def test_publish_needs_the_key_and_a_known_mode(released):
    _, _, key = released
    with pytest.raises(owner_page.PageError):
        owner_page.publish_from_page("x" * 43, "calc", "push")
    with pytest.raises(owner_page.PageError):
        owner_page.publish_from_page(key, "calc", "force")


def test_publish_itself_refuses_an_unknown_mode(released):
    with pytest.raises(rel.ReleaseError, match="bad_mode"):
        rel.publish("calc", "force")


def test_undismiss_puts_a_dismissed_project_back_on_the_list(env, done_status):
    root, _ = env
    _finish(root, "calc")
    key = owner_page.rotate()
    owner_page.dismiss_from_page(key, "calc")
    assert owner_page.todo() == []
    assert owner_page.undismiss_from_page(key, "calc") == {"ok": True, "was_dismissed": True}
    by = {p["project"]: p for p in owner_page.snapshot()["projects"]}
    assert by["calc"]["releasable"] and not by["calc"]["dismissed"]
    assert owner_page.todo()
    assert owner_page.undismiss("calc") is False                  # nothing to undo twice
    with pytest.raises(owner_page.PageError):
        owner_page.undismiss_from_page("x" * 43, "calc")          # wrong key: refused


def test_undismiss_cli(env, done_status, monkeypatch, capsys):
    import tools.owner_page as cli
    root, _ = env
    _finish(root, "calc")
    owner_page.dismiss("calc", by="test")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(["undismiss", "calc"]) == 0
    assert "back on the list" in capsys.readouterr().out
    assert cli.main(["undismiss"]) == 2
