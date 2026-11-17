"""The owner's phone page and "what needs you" list.

One long-lived secret link only the owner has (a root-only key file, rotated with
tools/owner_page.py rotate). The page shows every full-cycle project, its tasks, open
reviews, what is ready to release, and what the loop watch sees wrong; a finished repo
project has a Release button there (core.fullcycle.release.release_project, same checks).

`todo()` is the short list of what waits for the owner; the loop watch sends it to
Telegram whenever it changes, so the owner never has to ask "what do you need from me".
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
from typing import Optional

from core.fullcycle import spec as specs

DEFAULT_KEY_FILE = "/etc/ai-runtime/owner-page.key"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_release_dismissed (project_id TEXT PRIMARY KEY, ts REAL, by TEXT)
"""
_ID_OK = __import__("re").compile(r"^[A-Za-z0-9_-]{32,64}$")


class PageError(Exception):
    pass


def _key_file() -> str:
    return os.getenv("FULLCYCLE_OWNER_PAGE_KEY_FILE", "").strip() or DEFAULT_KEY_FILE


def secret() -> Optional[str]:
    path = _key_file()
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return None
    with open(path, encoding="ascii") as f:
        s = f.read(128).strip()
    return s if _ID_OK.match(s) else None


def rotate() -> str:
    path = _key_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    new = secrets.token_urlsafe(32)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(new + "\n")
    os.replace(tmp, path)
    return new


def check(given: str) -> None:
    s = secret()
    if not s or not hmac.compare_digest(s.encode(), (given or "").encode()):
        raise PageError("not_found")


def url() -> Optional[str]:
    base = os.getenv("FULLCYCLE_OWNER_PAGE_BASE", "").strip().rstrip("/")
    s = secret()
    return f"{base}/{s}" if base and s else None


def _tables(conn) -> None:
    conn.execute(_SCHEMA)


def dismiss(project_id: str, *, by: str, conn=None) -> None:
    """The owner will not release this project on its own (e.g. it went out inside an
    integration that merged it by hand). It leaves the to-do list."""
    import time
    from core.fullcycle import engine as fe
    conn, own = fe.connect(conn)
    try:
        _tables(conn)
        conn.execute("INSERT OR REPLACE INTO fc_release_dismissed VALUES (?,?,?)",
                     (project_id, time.time(), by))
        conn.commit()
    finally:
        if own:
            conn.close()


def undismiss(project_id: str, conn=None) -> bool:
    """Undo `dismiss`: the project is back on the to-do list if it is still releasable.
    True when it had been dismissed."""
    from core.fullcycle import engine as fe
    conn, own = fe.connect(conn)
    try:
        _tables(conn)
        n = conn.execute("DELETE FROM fc_release_dismissed WHERE project_id=?",
                         (project_id,)).rowcount
        conn.commit()
        return bool(n)
    finally:
        if own:
            conn.close()


def _merged_into_released(conn, released_ids: set) -> set:
    """Projects brought into an integration (merge_from) that was released."""
    try:
        rows = conn.execute("SELECT into_id, from_id FROM fc_intake_merge").fetchall()
    except Exception:  # noqa: BLE001 — no integration made yet
        return set()
    return {f for i, f in rows if i in released_ids}


def _projects(conn) -> list:
    from core import task_contract as tc
    from core.fullcycle import engine as fe
    from core.fullcycle import intake, release
    _tables(conn)
    rows = conn.execute("SELECT id FROM fc_project ORDER BY rowid DESC").fetchall()
    done = {r[0] for r in conn.execute("SELECT project_id FROM fc_project_done")}
    dismissed = {r[0] for r in conn.execute("SELECT project_id FROM fc_release_dismissed")}
    rels = {pid: release.released(pid, conn=conn) for (pid,) in rows}
    via = _merged_into_released(conn, {p for p, r in rels.items() if r})
    out = []
    for (pid,) in rows:
        try:
            st = fe.project_status(pid, conn=conn)
        except Exception:  # noqa: BLE001 — one broken project must not hide the rest
            continue
        src = intake.source_of(pid, conn=conn)
        tasks = [{"key": t["key"], "state": t["state"]} for t in st.get("tasks") or []]
        rel = rels.get(pid) if src else None
        out.append({
            "project": pid, "done": pid in done, "repo": (src or {}).get("repo"),
            "branch": (src or {}).get("branch"), "tasks": tasks,
            "open_reviews": len(st.get("pending_reviews") or []),
            "needs_owner": [t["key"] for t in tasks if t["state"] == tc.OWNER_DECISION],
            "releasable": bool(src) and pid in done and tasks != [] and
                          all(t["state"] in ("VERIFIED_DONE", "CANCELLED") for t in tasks)
                          and not rel and pid not in via and pid not in dismissed,
            "dismissed": pid in dismissed, "in_released_integration": pid in via,
            "released": ({"into": rel.get("into"), "sha": rel.get("sha", "")[:12],
                          "branch": rel.get("branch")} if rel else None),
            "published": _published_view(pid, rel, conn)})
    return out


def _published_view(pid: str, rel: Optional[dict], conn) -> Optional[dict]:
    if not rel:
        return None
    from core.fullcycle import release
    pub = release.published(pid, conn=conn)
    if pub and pub.get("sha") == rel.get("sha"):
        return {"mode": pub.get("mode"), "ref": pub.get("ref"), "url": pub.get("url")}
    return {"mode": None}


def _loop_problems(conn) -> list:
    try:
        return [r[0] for r in conn.execute("SELECT text FROM loop_watch WHERE active=1")]
    except Exception:  # noqa: BLE001 — the loop watch has not run yet
        return []


def snapshot(conn=None) -> dict:
    from core.fullcycle import engine as fe
    conn, own = fe.connect(conn)
    try:
        projects = _projects(conn)
        return {"projects": projects, "loop_problems": _loop_problems(conn),
                "todo": _todo(projects)}
    finally:
        if own:
            conn.close()


def _todo(projects: list) -> list:
    items = []
    for p in projects:
        if p["releasable"]:
            items.append({"key": f"release:{p['project']}",
                          "text": f"готово к выпуску: {p['project']} ({p['repo']})"})
        for t in p["needs_owner"]:
            items.append({"key": f"owner:{p['project']}:{t}",
                          "text": f"нужно твоё решение: {p['project']} / {t}"})
    return items


def todo(conn=None) -> list:
    return snapshot(conn)["todo"]


def dismiss_from_page(given: str, project_id: str) -> dict:
    check(given)
    if not specs.KEY_RE.match(project_id or ""):
        raise PageError("bad_project")
    dismiss(project_id, by="owner-page")
    return {"ok": True}


def undismiss_from_page(given: str, project_id: str) -> dict:
    check(given)
    if not specs.KEY_RE.match(project_id or ""):
        raise PageError("bad_project")
    return {"ok": True, "was_dismissed": undismiss(project_id)}


def dismiss_finished(by: str = "owner") -> list:
    """Mark every project that is releasable right now as not to be released on its own."""
    out = [p["project"] for p in snapshot()["projects"] if p["releasable"]]
    for pid in out:
        dismiss(pid, by=by)
    return out


def publish_from_page(given: str, project_id: str, mode: str, notify=None) -> dict:
    from core.fullcycle import release
    check(given)
    if not specs.KEY_RE.match(project_id or "") or mode not in release.PUBLISH_MODES:
        raise PageError("bad_request")
    try:
        return release.publish(project_id, mode, notify=notify)
    except release.ReleaseError as e:
        return {"ok": False, "reason": str(e)}


def release_from_page(given: str, project_id: str, notify=None) -> dict:
    from core.fullcycle import release
    check(given)
    if not specs.KEY_RE.match(project_id or ""):
        raise PageError("bad_project")
    try:
        return release.release_project(project_id, notify=notify)
    except release.ReleaseError as e:
        return {"ok": False, "reason": str(e)}


def as_json(given: str) -> str:
    check(given)
    return json.dumps(snapshot(), ensure_ascii=False)
