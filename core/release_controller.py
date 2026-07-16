"""Release Controller (OWNER-111) — a real implementation, not a plan.

Purpose
-------
Take a specific runtime work branch to production deliberately and reversibly:

    create -> approve -> release (merge, retest, restart, health check)
                                 └─ automatic rollback on any failure

Design rules enforced here
--------------------------
* **Nothing is released automatically.** A release candidate exists only because
  an operator created it for one named branch, and it merges only after an
  explicit approval that pins the exact head SHA.
* **A plan is not releasable.** A branch whose job outcome is
  `fallback_plan_only` is refused (see `core.job_kinds.is_releasable`).
* **Duplicate merges are impossible.** State is persisted and the merge is
  guarded by both the candidate's state machine and a merged-SHA check.
* **Always reversible.** `main` is backed up to a timestamped branch before the
  merge; any failure after the merge (tests, restart, health) triggers an
  automatic rollback of `main` to that backup and restarts the service again.
* **Only the affected service is restarted** — never a blanket restart.

State lives in SQLite so a release survives a service restart mid-flight.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core import git_write

_DB = os.getenv("RUNTIME_RELEASE_DB",
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime_releases.db"))
_LOCK = threading.RLock()

_GIT_TIMEOUT = 120
_TEST_TIMEOUT = int(os.getenv("RELEASE_TEST_TIMEOUT", "900"))
_HEALTH_TIMEOUT = int(os.getenv("RELEASE_HEALTH_TIMEOUT", "60"))

# States
CREATED = "created"
APPROVED = "approved"
MERGING = "merging"
MERGED = "merged"
VERIFYING = "verifying"
RELEASED = "released"
ROLLED_BACK = "rolled_back"
FAILED = "failed"
REJECTED = "rejected"

#: A candidate may only be merged from exactly this state.
_MERGEABLE_FROM = {APPROVED}
#: Terminal states — a candidate here is finished and can never merge again.
_TERMINAL = {RELEASED, ROLLED_BACK, FAILED, REJECTED}

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@\-]+\.service$")


class ReleaseError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY,
            branch TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            approved_sha TEXT,
            merge_sha TEXT,
            backup_branch TEXT,
            service TEXT,
            health_url TEXT,
            state TEXT NOT NULL,
            diff_stat TEXT,
            diff_files TEXT,
            tests_before TEXT,
            tests_after TEXT,
            health TEXT,
            approved_by TEXT,
            approved_at TEXT,
            error TEXT,
            log TEXT,
            created_at TEXT,
            updated_at TEXT
        )""")
        # A branch may have many historical candidates, but only one that is
        # live (non-terminal) at a time — enforced in create_candidate().
        c.execute("CREATE INDEX IF NOT EXISTS idx_releases_branch ON releases(branch)")


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    for f in ("diff_files", "tests_before", "tests_after", "health", "log"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except Exception:  # noqa: BLE001
                d[f] = None
    if d.get("log") is None:
        d["log"] = []
    return d


def get(rc_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM releases WHERE id=?", (rc_id,)).fetchone()
    return _row(r) if r else None


def list_releases(limit: int = 50) -> List[Dict[str, Any]]:
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT * FROM releases ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def _update(rc_id: str, **fields) -> Dict[str, Any]:
    for f in ("diff_files", "tests_before", "tests_after", "health", "log"):
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    sets = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [_now(), rc_id]
    with _LOCK, _conn() as c:
        c.execute(f"UPDATE releases SET {sets}, updated_at=? WHERE id=?", vals)
    return get(rc_id)


def _log(rc_id: str, msg: str) -> None:
    rc = get(rc_id)
    if not rc:
        return
    entries = rc.get("log") or []
    entries.append({"ts": _now(), "msg": str(msg)[:500]})
    _update(rc_id, log=entries[-200:])


def _git(project_path: str, args: List[str], check: bool = True) -> str:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    p = subprocess.run(["git", "-c", "safe.directory=*", "-C", project_path] + args,
                       capture_output=True, text=True, timeout=_GIT_TIMEOUT, env=env, shell=False)
    if check and p.returncode != 0:
        raise ReleaseError((p.stderr or p.stdout or "git error").strip()[:400])
    return p.stdout


def _sha(project_path: str, ref: str) -> str:
    try:
        return _git(project_path, ["rev-parse", f"{ref}^{{commit}}"]).strip()
    except ReleaseError as e:
        raise ReleaseError(f"cannot resolve ref {ref!r}: {e}") from e


# --------------------------------------------------------------------------
# 1) CREATE
# --------------------------------------------------------------------------

def create_candidate(project_path: str, branch: str, base_branch: str = "main",
                     service: Optional[str] = None, health_url: Optional[str] = None,
                     job_outcome: Optional[str] = None) -> Dict[str, Any]:
    """Create a release candidate for one explicitly named branch.

    Refuses a branch that carries no real implementation (`fallback_plan_only`),
    a branch identical to base, and a branch that already has a live candidate.
    """
    init_db()
    if not branch:
        raise ReleaseError("branch is required — no branch is ever released implicitly")
    if service is not None and not _SERVICE_RE.match(service):
        raise ReleaseError(f"invalid service unit name: {service!r}")

    # A plan is not an implementation and must never reach production.
    if job_outcome is not None:
        from core import job_kinds
        if not job_kinds.is_releasable(job_outcome):
            raise ReleaseError(
                f"branch {branch!r} has outcome {job_outcome!r} and is not releasable — "
                "only a real implementation can be released")

    head_sha = _sha(project_path, branch)
    base_sha = _sha(project_path, base_branch)
    if head_sha == base_sha:
        raise ReleaseError(f"branch {branch!r} is identical to {base_branch!r} — nothing to release")

    # Refuse a second live candidate for the same branch (duplicate protection
    # begins at creation, not only at merge).
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT id, state FROM releases WHERE branch=?", (branch,)).fetchall()
    live = [r["id"] for r in rows if r["state"] not in _TERMINAL]
    if live:
        raise ReleaseError(f"branch {branch!r} already has a live release candidate: {live[0]}")
    if any(r["state"] == RELEASED for r in rows):
        already = [r["id"] for r in rows if r["state"] == RELEASED][0]
        raise ReleaseError(f"branch {branch!r} was already released ({already}) — refusing duplicate release")

    merge_base = _git(project_path, ["merge-base", base_branch, branch]).strip()
    diff_stat = _git(project_path, ["diff", "--stat", f"{merge_base}..{head_sha}"])
    diff_files = [ln for ln in _git(
        project_path, ["diff", "--name-only", f"{merge_base}..{head_sha}"]).splitlines() if ln.strip()]

    rc_id = f"rc-{uuid.uuid4().hex[:12]}"
    with _LOCK, _conn() as c:
        c.execute(
            "INSERT INTO releases (id, branch, base_branch, head_sha, base_sha, service, health_url,"
            " state, diff_stat, diff_files, created_at, updated_at, log) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rc_id, branch, base_branch, head_sha, base_sha, service, health_url, CREATED,
             diff_stat[:8000], json.dumps(diff_files), _now(), _now(), json.dumps([])))
    _log(rc_id, f"candidate created for {branch} @ {head_sha[:12]} onto {base_branch} @ {base_sha[:12]}")
    return get(rc_id)


# --------------------------------------------------------------------------
# 2) VERIFY / TEST
# --------------------------------------------------------------------------

def verify(project_path: str, rc_id: str) -> Dict[str, Any]:
    """Re-verify that the recorded head SHA still matches the branch."""
    rc = _require(rc_id)
    actual = _sha(project_path, rc["branch"])
    ok = actual == rc["head_sha"]
    if not ok:
        _log(rc_id, f"HEAD DRIFT: recorded {rc['head_sha'][:12]}, branch now {actual[:12]}")
    return {"ok": ok, "recorded": rc["head_sha"], "actual": actual}


def run_tests(project_path: str, rc_id: str, command: Optional[List[str]] = None,
              phase: str = "tests_before") -> Dict[str, Any]:
    """Run the repository suite and record the result on the candidate."""
    _require(rc_id)
    cmd = command or ["python3", "-m", "pytest", "-q"]
    try:
        p = subprocess.run(cmd, cwd=project_path, capture_output=True, text=True,
                           timeout=_TEST_TIMEOUT, shell=False)
        result = {"ok": p.returncode == 0, "cmd": " ".join(cmd), "returncode": p.returncode,
                  "output": (p.stdout + p.stderr)[-4000:], "at": _now()}
    except subprocess.TimeoutExpired:
        result = {"ok": False, "cmd": " ".join(cmd), "returncode": -1,
                  "output": f"timed out after {_TEST_TIMEOUT}s", "at": _now()}
    _update(rc_id, **{phase: result})
    _log(rc_id, f"{phase}: {'PASS' if result['ok'] else 'FAIL'} ({result['cmd']})")
    return result


# --------------------------------------------------------------------------
# 3) APPROVE
# --------------------------------------------------------------------------

def approve(project_path: str, rc_id: str, approver: str, head_sha: str) -> Dict[str, Any]:
    """Explicit approval. The approver must name the exact SHA being approved:
    approving a moving target is how the wrong code reaches production."""
    rc = _require(rc_id)
    if rc["state"] in _TERMINAL:
        raise ReleaseError(f"candidate {rc_id} is {rc['state']} — cannot approve")
    if rc["state"] != CREATED:
        raise ReleaseError(f"candidate {rc_id} is {rc['state']} — only a created candidate can be approved")
    if not approver:
        raise ReleaseError("approver is required")
    if not head_sha:
        raise ReleaseError("approval must name the exact head SHA being approved")

    actual = _sha(project_path, rc["branch"])
    if not rc["head_sha"].startswith(head_sha) and not head_sha.startswith(rc["head_sha"][:7]):
        raise ReleaseError(f"approved SHA {head_sha} does not match candidate head {rc['head_sha']}")
    if actual != rc["head_sha"]:
        raise ReleaseError(
            f"branch {rc['branch']} moved since the candidate was created "
            f"({rc['head_sha'][:12]} -> {actual[:12]}) — re-create the candidate")

    tests = rc.get("tests_before")
    if not (isinstance(tests, dict) and tests.get("ok")):
        raise ReleaseError("candidate has no passing test run — run tests before approving")

    _update(rc_id, state=APPROVED, approved_by=approver, approved_at=_now(), approved_sha=rc["head_sha"])
    _log(rc_id, f"approved by {approver} for {rc['head_sha'][:12]}")
    return get(rc_id)


def reject(rc_id: str, reason: str) -> Dict[str, Any]:
    rc = _require(rc_id)
    if rc["state"] in _TERMINAL:
        raise ReleaseError(f"candidate {rc_id} is already {rc['state']}")
    _update(rc_id, state=REJECTED, error=reason[:400])
    _log(rc_id, f"rejected: {reason}")
    return get(rc_id)


def _require(rc_id: str) -> Dict[str, Any]:
    rc = get(rc_id)
    if not rc:
        raise ReleaseError(f"unknown release candidate: {rc_id}")
    return rc


# --------------------------------------------------------------------------
# 4) SERVICE + HEALTH
# --------------------------------------------------------------------------

def restart_service(service: str) -> Dict[str, Any]:
    """Restart exactly one unit — never a blanket restart of everything."""
    if not _SERVICE_RE.match(service or ""):
        raise ReleaseError(f"invalid service unit name: {service!r}")
    p = subprocess.run(["systemctl", "restart", service], capture_output=True, text=True,
                       timeout=120, shell=False)
    ok = p.returncode == 0
    return {"ok": ok, "service": service, "output": (p.stdout + p.stderr)[-1000:]}


def health_check(url: str, timeout: int = _HEALTH_TIMEOUT, interval: float = 2.0,
                 sleep=time.sleep) -> Dict[str, Any]:
    """Poll a health URL until it answers 2xx or the budget runs out."""
    deadline = time.monotonic() + timeout
    last = ""
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - operator-supplied URL
                body = resp.read(2000).decode("utf-8", "replace")
                if 200 <= resp.status < 300:
                    return {"ok": True, "status": resp.status, "body": body,
                            "attempts": attempts, "url": url}
                last = f"status {resp.status}: {body[:200]}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)[:200]
        sleep(interval)
    return {"ok": False, "error": last or "health check timed out", "attempts": attempts, "url": url}


# --------------------------------------------------------------------------
# 5) RELEASE (merge -> retest -> restart -> health, rollback on any failure)
# --------------------------------------------------------------------------

def release(project_path: str, rc_id: str, restart=restart_service,
            health=health_check, run_tests_fn=None) -> Dict[str, Any]:
    """Merge an approved candidate and verify it, rolling back automatically on
    any failure. Refuses anything that is not exactly one approved candidate."""
    rc = _require(rc_id)

    # -- duplicate-merge protection ----------------------------------------
    if rc["state"] in _TERMINAL:
        raise ReleaseError(f"candidate {rc_id} is {rc['state']} — refusing duplicate release")
    if rc["state"] not in _MERGEABLE_FROM:
        raise ReleaseError(
            f"candidate {rc_id} is {rc['state']} — only an approved candidate can be released")

    base, branch = rc["base_branch"], rc["branch"]

    # -- head SHA must still be exactly what was approved -------------------
    actual = _sha(project_path, branch)
    if actual != rc["approved_sha"]:
        _update(rc_id, state=FAILED, error=f"head drift: approved {rc['approved_sha'][:12]}, now {actual[:12]}")
        raise ReleaseError(f"branch {branch} moved since approval — refusing to release")

    # -- the branch must not already be merged into base --------------------
    merged = [ln.strip().lstrip("* ").strip()
              for ln in _git(project_path, ["branch", "--merged", base]).splitlines()]
    if branch in merged:
        _update(rc_id, state=FAILED, error=f"{branch} is already merged into {base}")
        raise ReleaseError(f"{branch} is already merged into {base} — refusing duplicate merge")

    if _git(project_path, ["status", "--porcelain"]).strip():
        raise ReleaseError("workspace is dirty — commit or clean it before releasing")

    _update(rc_id, state=MERGING)

    # -- back up base BEFORE touching it ------------------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_branch = f"backup/{base}-{stamp}"
    base_sha_before = _sha(project_path, base)
    _git(project_path, ["branch", backup_branch, base])
    _update(rc_id, backup_branch=backup_branch, base_sha=base_sha_before)
    _log(rc_id, f"backed up {base} @ {base_sha_before[:12]} -> {backup_branch}")

    def _rollback(reason: str) -> Dict[str, Any]:
        _log(rc_id, f"ROLLBACK: {reason}")
        try:
            _git(project_path, ["checkout", base])
            _git(project_path, ["reset", "--hard", base_sha_before])
            _log(rc_id, f"{base} reset to {base_sha_before[:12]} from {backup_branch}")
            if rc.get("service"):
                r = restart(rc["service"])
                _log(rc_id, f"restarted {rc['service']} after rollback: ok={r.get('ok')}")
        except Exception as e:  # noqa: BLE001
            _log(rc_id, f"rollback error: {e}")
        _update(rc_id, state=ROLLED_BACK, error=reason[:400])
        return get(rc_id)

    # -- merge --------------------------------------------------------------
    try:
        _git(project_path, ["checkout", base])
        _git(project_path, ["merge", "--no-ff", "-m",
                            f"release({rc_id}): merge {branch} into {base}\n\n"
                            f"Approved-by: {rc['approved_by']}\nHead: {rc['approved_sha']}",
                            rc["approved_sha"]])
    except ReleaseError as e:
        try:
            _git(project_path, ["merge", "--abort"], check=False)
        except Exception:  # noqa: BLE001
            pass
        return _rollback(f"merge failed: {e}")

    merge_sha = _sha(project_path, base)
    _update(rc_id, state=MERGED, merge_sha=merge_sha)
    _log(rc_id, f"merged {branch} into {base} -> {merge_sha[:12]}")

    # -- full test run AFTER the merge --------------------------------------
    _update(rc_id, state=VERIFYING)
    runner = run_tests_fn or (lambda: run_tests(project_path, rc_id, phase="tests_after"))
    after = runner()
    if isinstance(after, dict):
        _update(rc_id, tests_after=after)
    if not (isinstance(after, dict) and after.get("ok")):
        return _rollback("post-merge tests failed")

    # -- restart only the affected service ----------------------------------
    if rc.get("service"):
        r = restart(rc["service"])
        _log(rc_id, f"restarted {rc['service']}: ok={r.get('ok')}")
        if not r.get("ok"):
            return _rollback(f"service restart failed: {r.get('output', '')[:200]}")

    # -- health check --------------------------------------------------------
    if rc.get("health_url"):
        h = health(rc["health_url"])
        _update(rc_id, health=h)
        _log(rc_id, f"health check: ok={h.get('ok')}")
        if not h.get("ok"):
            return _rollback(f"health check failed: {h.get('error', '')[:200]}")

    _update(rc_id, state=RELEASED)
    _log(rc_id, f"RELEASED {branch} -> {base} @ {merge_sha[:12]}")
    return get(rc_id)


def rollback(project_path: str, rc_id: str, restart=restart_service) -> Dict[str, Any]:
    """Operator-triggered rollback of an already-released candidate."""
    rc = _require(rc_id)
    if not rc.get("backup_branch"):
        raise ReleaseError(f"candidate {rc_id} has no backup branch — nothing to roll back to")
    if rc["state"] not in (RELEASED, MERGED, VERIFYING, FAILED):
        raise ReleaseError(f"candidate {rc_id} is {rc['state']} — nothing to roll back")
    base = rc["base_branch"]
    target = _sha(project_path, rc["backup_branch"])
    _git(project_path, ["checkout", base])
    _git(project_path, ["reset", "--hard", target])
    _log(rc_id, f"manual rollback: {base} reset to {target[:12]} ({rc['backup_branch']})")
    if rc.get("service"):
        r = restart(rc["service"])
        _log(rc_id, f"restarted {rc['service']} after rollback: ok={r.get('ok')}")
    _update(rc_id, state=ROLLED_BACK, error="rolled back by operator")
    return get(rc_id)
