"""Release from the phone: a signed one-time link fast-forwards the owner's repository."""
from __future__ import annotations

import os
import subprocess

import pytest

from core.fullcycle import intake
from core.fullcycle import release as rel
from core.fullcycle import repos
from core.fullcycle import spec as specs

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
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", str(root))
    monkeypatch.setenv("FULLCYCLE_REPOS_FILE", str(tmp_path / "etc" / "repos.json"))
    monkeypatch.delenv("FULLCYCLE_INTAKE_RUN_AS", raising=False)
    key = tmp_path / "release.key"
    key.write_bytes(os.urandom(32).hex().encode())
    os.chmod(key, 0o600)
    monkeypatch.setenv("FULLCYCLE_RELEASE_KEY_FILE", str(key))
    monkeypatch.setenv("FULLCYCLE_RELEASE_URL_BASE", "https://example.test/api/owner/release")
    src = tmp_path / "work" / "shop"
    src.mkdir(parents=True)
    _git(src, "init", "-q", "-b", "main")
    (src / "app.py").write_text("x = 1\n")
    _git(src, "add", "app.py")
    _git(src, "commit", "-q", "-m", "first")
    repos.allow("shop", str(src))
    intake.start("calc", SPEC, by=BY, repo="shop")
    proj = root / "calc"
    (proj / "add.py").write_text("def add(a, b):\n    return a + b\n")
    _git(proj, "add", "add.py")
    _git(proj, "commit", "-q", "-m", "fullcycle: add")
    return src, proj, _git(proj, "rev-parse", "HEAD"), key


def _token(link):
    return link.rsplit("/", 1)[1]


def test_the_link_releases_once_by_fast_forward(env):
    src, proj, sha, _ = env
    told = []
    link = rel.make_link("calc", sha)
    assert link.startswith("https://example.test/api/owner/release/")
    p = rel.preview(_token(link))
    assert p["project"] == "calc" and p["sha"] == sha and p["into"] == str(src)
    assert _git(src, "rev-parse", "HEAD") != sha                 # a preview spends nothing
    r = rel.release(_token(link), notify=told.append)
    assert r["ok"] and r["branch"] == "main" and _git(src, "rev-parse", "HEAD") == sha
    assert (src / "add.py").exists() and "Выпущено" in told[0]
    again = rel.release(_token(link))
    assert again["ok"] is False and again["reason"] == "link_already_used"
    assert again["result"]["ok"] is True and again["result"]["sha"] == sha
    assert rel.preview(_token(link))["used"] is True


def test_a_tampered_or_foreign_link_is_refused(env):
    _, _, sha, _ = env
    t = _token(rel.make_link("calc", sha))
    body, sig = t.split(".")
    for bad in (body + "." + sig[:-2] + "AA", body[:-2] + "AA." + sig, "x.y", ""):
        with pytest.raises(rel.ReleaseError, match="link_invalid"):
            rel.release(bad)


def test_an_expired_link_is_refused(env):
    _, _, sha, _ = env
    t = _token(rel.make_link("calc", sha, now=1000.0))
    with pytest.raises(rel.ReleaseError, match="link_expired"):
        rel.preview(t, now=1000.0 + rel.RELEASE_TTL + 1)


def test_no_key_or_no_url_means_no_link(env, monkeypatch):
    _, _, sha, key = env
    monkeypatch.setenv("FULLCYCLE_RELEASE_URL_BASE", "")
    assert rel.make_link("calc", sha) is None
    monkeypatch.setenv("FULLCYCLE_RELEASE_URL_BASE", "https://example.test/r")
    os.chmod(key, 0o644)
    assert rel.make_link("calc", sha) is None                     # an unsafe key is no key


def test_a_branch_moved_after_the_link_is_not_released(env):
    src, proj, sha, _ = env
    t = _token(rel.make_link("calc", sha))
    (proj / "more.py").write_text("y = 2\n")
    _git(proj, "add", "more.py")
    _git(proj, "commit", "-q", "-m", "later")
    r = rel.release(t)
    assert r["ok"] is False and "moved" in r["reason"]
    assert _git(src, "rev-parse", "HEAD") != sha


def test_a_repo_that_moved_on_is_not_overwritten(env):
    src, _, sha, _ = env
    before = _git(src, "rev-parse", "HEAD")
    (src / "owner.py").write_text("o = 1\n")
    _git(src, "add", "owner.py")
    _git(src, "commit", "-q", "-m", "owner work")
    r = rel.release(_token(rel.make_link("calc", sha)))
    assert r["ok"] is False and "new_integration" in r["reason"]
    assert _git(src, "log", "--format=%s", "-1") == "owner work" and before


def test_uncommitted_changes_in_the_repo_block_the_release(env):
    src, _, sha, _ = env
    (src / "app.py").write_text("x = 99  # owner editing\n")
    r = rel.release(_token(rel.make_link("calc", sha)))
    assert r["ok"] is False and "uncommitted" in r["reason"]
    assert (src / "app.py").read_text() == "x = 99  # owner editing\n"


def test_the_finished_notice_carries_the_link(env, monkeypatch):
    from core.fullcycle import engine as fe
    _, proj, sha, _ = env
    conn, _o = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES (?,?,?,?)", ("calc", 1, "", 5.0))
    conn.commit()
    monkeypatch.setattr(fe, "project_status",
                        lambda pid, conn=None: {"tasks": [{"key": "add",
                                                           "state": "VERIFIED_DONE"}]})
    text = intake.notice_text({"id": "calc", "root": str(proj)}, conn)
    conn.close()
    assert "Выпустить" in text and "https://example.test/api/owner/release/" in text


def test_the_api_routes_need_the_runtime_token():
    from fastapi.testclient import TestClient
    import api.main as m
    c = TestClient(m.app)
    assert c.get("/api/v1/fullcycle/release/x.y").status_code in (401, 403, 503)
    assert c.post("/api/v1/fullcycle/release/x.y").status_code in (401, 403, 503)


def test_a_link_whose_record_was_altered_is_refused(env):
    from core.fullcycle import engine as fe
    _, _, sha, _ = env
    t = _token(rel.make_link("calc", sha))
    conn, _o = fe.connect()
    conn.execute("UPDATE fc_release_link SET sha=?", ("0" * 40,))
    conn.commit()
    conn.close()
    with pytest.raises(rel.ReleaseError, match="link_invalid"):
        rel.preview(t)
