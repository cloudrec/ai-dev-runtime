"""Release from the phone: a signed one-time link in the owner's Telegram notice.

The owner's bot belongs to another system (its webhook takes text messages only), so a
button press cannot come back to Owner OS. The finished-project notice therefore carries a
link: HMAC-signed with a root-only key, bound to one project and the exact commit it was
made for, valid RELEASE_TTL, usable once. Opening it shows what would be released; only a
POST from that page releases.

Release = fast-forward the owner's repository's checked-out branch to the project's branch,
locally. Refused when that branch has commits the project does not hold, when the
repository has uncommitted changes to tracked files, or when the project's branch moved
since the link was made. Nothing is pushed or deployed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import time
from typing import Optional

from core.fullcycle import spec as specs

RELEASE_TTL = 24 * 3600
DEFAULT_KEY_FILE = "/etc/ai-runtime/release-link.key"
_NONCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_release_link (
    nonce TEXT PRIMARY KEY, project_id TEXT, sha TEXT, made_ts REAL, used_ts REAL,
    result TEXT
)"""
# A long signed token broke across lines in the owner's terminal (2026-11-09). The link now
# carries a short random id (128 bits, itself the capability); the row holds the rest.
_SHORT_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{20,32}$")


class ReleaseError(Exception):
    pass


def _key() -> Optional[bytes]:
    path = os.getenv("FULLCYCLE_RELEASE_KEY_FILE", "").strip() or DEFAULT_KEY_FILE
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return None
    with open(path, "rb") as f:
        k = f.read(256).strip()
    return k if len(k) >= 32 else None


def url_base() -> str:
    return os.getenv("FULLCYCLE_RELEASE_URL_BASE", "").strip().rstrip("/")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _conn(conn=None):
    from core.fullcycle import engine as fe
    conn, own = fe.connect(conn)
    conn.execute(_NONCE_SCHEMA)
    if "link_id" not in {r[1] for r in conn.execute("PRAGMA table_info(fc_release_link)")}:
        conn.execute("ALTER TABLE fc_release_link ADD COLUMN link_id TEXT")
    return conn, own


def make_link(project_id: str, sha: str, *, conn=None, now: Optional[float] = None) -> Optional[str]:
    """The link for the notice, or None when the feature is off (no key or no URL base)."""
    key, base = _key(), url_base()
    if not key or not base or len(sha or "") != 40:
        return None
    now = now if now is not None else time.time()
    nonce = secrets.token_urlsafe(12)
    link_id = secrets.token_urlsafe(16)
    conn, own = _conn(conn)
    try:
        conn.execute("INSERT INTO fc_release_link (nonce, project_id, sha, made_ts, link_id) "
                     "VALUES (?,?,?,?,?)", (nonce, project_id, sha, now, link_id))
        conn.commit()
    finally:
        if own:
            conn.close()
    return f"{base}/{link_id}"


def _claims(token: str, *, conn, now: float) -> dict:
    key = _key()
    if not key:
        raise ReleaseError("release_links_off")
    if _SHORT_RE.match(token or ""):
        row = conn.execute("SELECT project_id, sha, used_ts, result, nonce, made_ts FROM "
                           "fc_release_link WHERE link_id=?", (token,)).fetchone()
        if not row:
            raise ReleaseError("link_invalid")
        if now > float(row[5] or 0) + RELEASE_TTL:
            raise ReleaseError("link_expired")
        return {"project": row[0], "sha": row[1], "used": row[2] is not None,
                "result": json.loads(row[3]) if row[3] else None, "nonce": row[4],
                "expires": int(float(row[5]) + RELEASE_TTL)}
    try:
        body, sig = (token or "").split(".", 1)
        want = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, sig):
            raise ValueError
        c = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise ReleaseError("link_invalid") from None
    if not isinstance(c, dict) or now > float(c.get("e") or 0):
        raise ReleaseError("link_expired")
    row = conn.execute("SELECT project_id, sha, used_ts, result FROM fc_release_link WHERE "
                       "nonce=?", (str(c.get("n")),)).fetchone()
    if not row or row[0] != c.get("p") or row[1] != c.get("s"):
        raise ReleaseError("link_invalid")
    return {"project": row[0], "sha": row[1], "used": row[2] is not None,
            "result": json.loads(row[3]) if row[3] else None, "nonce": c["n"],
            "expires": int(c["e"])}


def preview(token: str, *, conn=None, now: Optional[float] = None) -> dict:
    """What the page shows before the owner presses the button. Spends nothing."""
    from core.fullcycle import engine as fe
    from core.fullcycle import intake
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        c = _claims(token, conn=conn, now=now)
        src = intake.source_of(c["project"], conn=conn) or {}
        st = fe.project_status(c["project"], conn=conn)
        return {"project": c["project"], "sha": c["sha"], "repo": src.get("repo"),
                "into": src.get("source"), "branch": src.get("branch"),
                "done": st.get("done"), "used": c["used"], "result": c["result"],
                "expires": c["expires"],
                "tasks": [{"key": t["key"], "state": t["state"]} for t in st["tasks"]]}
    finally:
        if own:
            conn.close()


def _git(cwd: str, *args: str, timeout: int = 120) -> str:
    r = subprocess.run(["git", "-c", "user.name=Owner OS", "-c", "user.email=owneros@localhost",
                        *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ReleaseError(f"git {args[0]}: {(r.stderr or '').strip()[:160]}")
    return r.stdout.strip()


def release(token: str, *, conn=None, now: Optional[float] = None, notify=None) -> dict:
    """Spend the link and fast-forward the owner's repository. Every refusal is a result
    the page shows; the link is spent either way, so a refused link is never retried."""
    from core import agent_privilege
    from core.fullcycle import intake, repos
    now = now if now is not None else time.time()
    conn, own = _conn(conn)
    try:
        c = _claims(token, conn=conn, now=now)
        if c["used"]:
            return {"ok": False, "reason": "link_already_used", "result": c["result"]}
        cur = conn.execute("UPDATE fc_release_link SET used_ts=? WHERE nonce=? AND used_ts IS "
                           "NULL", (now, c["nonce"]))
        conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "reason": "link_already_used"}
        try:
            out = _do_release(c, conn, repos, intake, agent_privilege)
        except (ReleaseError, repos.RepoError, OSError, subprocess.SubprocessError,
                agent_privilege.AgentPrivilegeError, specs.SpecError) as e:
            out = {"ok": False, "reason": str(e)[:240]}
        conn.execute("UPDATE fc_release_link SET result=? WHERE nonce=?",
                     (json.dumps(out), c["nonce"]))
        conn.commit()
    finally:
        if own:
            conn.close()
    if notify:
        try:
            notify(("🚀 Выпущено: " if out["ok"] else "⛔ Не выпущено: ")
                   + f"{c['project']} -> {out.get('into', '?')} "
                   + (f"на {out['sha'][:10]}" if out["ok"] else f"({out['reason']})"))
        except Exception:  # noqa: BLE001
            pass
    return out


def _do_release(c: dict, conn, repos, intake, agent_privilege) -> dict:
    pid, sha = c["project"], c["sha"]
    src = intake.source_of(pid, conn=conn)
    p = specs.get_project(pid, conn=conn)
    if not src or not p:
        raise ReleaseError("not_a_repo_project")
    entry = repos.get(src["repo"])                 # still allowed, still not payment, ...
    into = entry["path"]
    agent_privilege.require_root_work(into)
    bundle = repos.branch_bundle(p["root"], src["branch"], keep_raw=intake.intake_root() or "")
    try:
        ref = f"refs/fullcycle-release/{pid}"
        _git(into, "fetch", "-q", "--no-tags", bundle, f"+{src['branch']}:{ref}")
    finally:
        os.unlink(bundle)
    got = _git(into, "rev-parse", ref)
    if got != sha:
        raise ReleaseError("project_branch_moved_since_the_link")
    branch = _git(into, "symbolic-ref", "--short", "HEAD")
    if _git(into, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError(f"uncommitted_changes_in:{into}")
    head = _git(into, "rev-parse", "HEAD")
    r = subprocess.run(["git", "merge-base", "--is-ancestor", head, sha], cwd=into,
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        raise ReleaseError(f"{branch}_has_commits_the_project_does_not:start_a_new_integration")
    _git(into, "merge", "-q", "--ff-only", sha)
    return {"ok": True, "into": into, "branch": branch, "sha": sha, "was": head}


def head_of(project_id: str, *, conn=None) -> str:
    """The project branch's commit, read by the tree's owner inside its sandbox (root
    never opens an agent's repository)."""
    import pwd
    from core import agent_privilege as priv
    from core.fullcycle import intake
    p, src = specs.get_project(project_id, conn=conn), intake.source_of(project_id, conn=conn)
    if not p or not src:
        raise ReleaseError("not_a_repo_project")
    root = p["root"]
    argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", root,
            "rev-parse", "--verify", f"{src['branch']}^{{commit}}"]
    env = None
    st = os.lstat(root)
    if st.st_uid != 0:
        pw = pwd.getpwuid(st.st_uid)
        t = priv.Target(pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
        argv, env = priv.sandboxed_argv(t, intake.intake_root() or "", argv), priv.child_env(t)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env, cwd="/")
    if r.returncode != 0 or len(r.stdout.strip()) != 40:
        raise ReleaseError(f"cannot_read_branch:{r.stderr.strip()[:120]}")
    return r.stdout.strip()


def release_project(project_id: str, *, conn=None, notify=None) -> dict:
    """The owner pressed Release on the owner page (already authenticated): a link for
    the branch's current commit, spent at once, through the same checks."""
    link = make_link(project_id, head_of(project_id, conn=conn), conn=conn)
    if not link:
        raise ReleaseError("release_links_off")
    return release(link.rsplit("/", 1)[1], conn=conn, notify=notify)


def released(project_id: str, *, conn=None) -> Optional[dict]:
    """The last successful release of this project, if any."""
    conn, own = _conn(conn)
    try:
        for (res,) in conn.execute("SELECT result FROM fc_release_link WHERE project_id=? AND "
                                   "result IS NOT NULL ORDER BY used_ts DESC", (project_id,)):
            d = json.loads(res)
            if d.get("ok"):
                return d
        return None
    finally:
        if own:
            conn.close()


# ── publish: the owner presses Push or PR on the owner page ──────────────────────────
_PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_publish (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, mode TEXT, sha TEXT, ts REAL,
    result TEXT
)"""
PUBLISH_MODES = ("push", "pr")


def published(project_id: str, *, conn=None) -> Optional[dict]:
    conn, own = _conn(conn)
    try:
        conn.execute(_PUBLISH_SCHEMA)
        for (res,) in conn.execute("SELECT result FROM fc_publish WHERE project_id=? ORDER BY "
                                   "id DESC", (project_id,)):
            d = json.loads(res)
            if d.get("ok"):
                return d
        return None
    finally:
        if own:
            conn.close()


def publish(project_id: str, mode: str, *, conn=None, notify=None,
            run=subprocess.run) -> dict:
    """Send what was released to the repository's origin. push: the released branch,
    fast-forward only, never forced. pr: a branch owneros/<project> and a pull request into
    the released branch (gh). Only after a successful release, only the released commit."""
    from core import agent_privilege
    from core.fullcycle import intake, repos
    if mode not in PUBLISH_MODES:
        raise ReleaseError("bad_mode")
    conn, own = _conn(conn)
    try:
        conn.execute(_PUBLISH_SCHEMA)
        rel = released(project_id, conn=conn)
        src = intake.source_of(project_id, conn=conn)
        if not rel or not src:
            raise ReleaseError("not_released_yet")
        into = repos.get(src["repo"])["path"]          # still allowed, not payment, ...
        agent_privilege.require_root_work(into)
        sha, branch = rel["sha"], rel["branch"]
        try:
            out = _publish(into, sha, branch, project_id, mode, run)
        except (ReleaseError, OSError, subprocess.SubprocessError) as e:
            out = {"ok": False, "reason": str(e)[:240]}
        out.update(mode=mode, sha=sha)
        conn.execute("INSERT INTO fc_publish (project_id, mode, sha, ts, result) VALUES "
                     "(?,?,?,?,?)", (project_id, mode, sha, time.time(), json.dumps(out)))
        conn.commit()
    finally:
        if own:
            conn.close()
    if notify:
        try:
            notify(("📤 Опубликовано: " if out["ok"] else "⛔ Не опубликовано: ")
                   + f"{project_id} ({mode}) " + (out.get("url") or out.get("ref") or "")
                   + ("" if out["ok"] else f" — {out['reason']}"))
        except Exception:  # noqa: BLE001
            pass
    return out


def _run(run, argv, cwd, timeout=120) -> str:
    r = run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise ReleaseError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: "
                           f"{(r.stderr or r.stdout or '').strip()[:180]}")
    return (r.stdout or "").strip()


def _publish(into: str, sha: str, branch: str, pid: str, mode: str, run) -> dict:
    remote = _run(run, ["git", "remote", "get-url", "--push", "origin"], into)
    if not remote:
        raise ReleaseError("no_origin")
    if mode == "push":
        line = _run(run, ["git", "ls-remote", "--heads", "origin", branch], into)
        theirs = line.split()[0] if line else ""
        if theirs and theirs != sha:
            ok = run(["git", "merge-base", "--is-ancestor", theirs, sha], cwd=into,
                     capture_output=True, timeout=60).returncode == 0
            if not ok:
                raise ReleaseError(f"origin/{branch}_has_commits_this_release_lacks")
        if theirs == sha:
            return {"ok": True, "ref": f"origin/{branch}", "note": "already there"}
        _run(run, ["git", "push", "origin", f"{sha}:refs/heads/{branch}"],  # never forced
             into, timeout=300)
        return {"ok": True, "ref": f"origin/{branch}"}
    head = f"owneros/{pid}"
    _run(run, ["git", "push", "origin", f"{sha}:refs/heads/{head}"], into, timeout=300)
    url = _run(run, ["gh", "pr", "create", "--head", head, "--base", branch,
                     "--title", f"Owner OS: {pid}",
                     "--body", f"Released by the owner from Owner OS full cycle ({pid}, "
                               f"{sha[:12]}). Reviewed task by task by the tech lead."],
               into, timeout=120)
    return {"ok": True, "ref": head, "url": url.splitlines()[-1] if url else ""}
