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
    return conn, own


def make_link(project_id: str, sha: str, *, conn=None, now: Optional[float] = None) -> Optional[str]:
    """The link for the notice, or None when the feature is off (no key or no URL base)."""
    key, base = _key(), url_base()
    if not key or not base or len(sha or "") != 40:
        return None
    now = now if now is not None else time.time()
    nonce = secrets.token_urlsafe(12)
    body = _b64(json.dumps({"p": project_id, "s": sha, "e": int(now + RELEASE_TTL),
                            "n": nonce}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    conn, own = _conn(conn)
    try:
        conn.execute("INSERT INTO fc_release_link (nonce, project_id, sha, made_ts) "
                     "VALUES (?,?,?,?)", (nonce, project_id, sha, now))
        conn.commit()
    finally:
        if own:
            conn.close()
    return f"{base}/{body}.{sig}"


def _claims(token: str, *, conn, now: float) -> dict:
    key = _key()
    if not key:
        raise ReleaseError("release_links_off")
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
