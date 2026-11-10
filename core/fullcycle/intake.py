"""Projects started by the reviewer: a spec in, a project, an agent and a worker tick out.

Off unless FULLCYCLE_INTAKE_ROOT names an existing directory. Each project gets its own
directory `<root>/<id>` with a fresh git repository and nothing else in it; a spec can
never point the engine at an existing tree. Checks in such a spec come from the reviewer,
not the owner, so the engine treats them like reviewer-added checks: a test runner over
project files only (see `Engine._seed`).

The agent is `fc-<id>`, started through `agent_control.agent_resume` as caller
`fullcycle:engine`, so the creation policy decides: a scope with `root: <intake root>`
must list that caller. The worker stops the agent once the project is done and no task is
left, which frees the slot for the next project.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

from core.fullcycle import gates
from core.fullcycle import spec as specs

SESSION_PREFIX = "fc-"
ACTOR = "fullcycle:engine"
BY_PREFIX = "api:"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
_GITIGNORE = ".fullcycle/\n__pycache__/\n*.pyc\n.pytest_cache/\n"


def intake_root() -> Optional[str]:
    raw = os.getenv("FULLCYCLE_INTAKE_ROOT", "").strip()
    if not raw or not os.path.isabs(raw) or not os.path.isdir(raw):
        return None
    real = os.path.realpath(raw)
    return real if real == os.path.abspath(raw) else None   # a symlinked root is refused


def max_active() -> int:
    try:
        return max(1, int(os.getenv("FULLCYCLE_INTAKE_MAX_ACTIVE", "2")))
    except ValueError:
        return 2


def session_for(project_id: str) -> str:
    return SESSION_PREFIX + project_id


def is_intake(project: Optional[dict], root: Optional[str] = None) -> bool:
    root = root or intake_root()
    return bool(project and root and
                os.path.dirname(project["root"]) == root and
                os.path.basename(project["root"]) == project["id"])


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=Owner OS", "-c", "user.email=owneros@localhost",
                    *args], cwd=cwd, check=True, capture_output=True, timeout=30)


def _new_repo(path: str) -> None:
    # Root makes the repo before any agent exists in it. Under AGENT_RUN_AS the tree
    # would have to be the agent's own, which intake does not set up: refused there.
    from core import agent_privilege
    try:
        agent_privilege.require_root_work(path)
    except agent_privilege.AgentPrivilegeError as e:
        raise specs.SpecError(f"intake_off_under_agent_run_as:{e}")
    os.mkdir(path, 0o755)
    _git(path, "init", "-q", "-b", "main")
    with open(os.path.join(path, ".gitignore"), "w", encoding="utf-8") as f:
        f.write(_GITIGNORE)
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-q", "-m", "baseline")


_SOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_intake_source (
    project_id TEXT PRIMARY KEY, repo TEXT, source TEXT, base_sha TEXT, branch TEXT, ts REAL
)"""


def _source_table(conn) -> None:
    conn.execute(_SOURCE_SCHEMA)


def source_of(project_id: str, conn=None) -> Optional[dict]:
    """Where an intake project came from: an allowed repository, or None for a new one."""
    conn, own = specs.conn_with_schema(conn)
    try:
        _source_table(conn)
        r = conn.execute("SELECT repo, source, base_sha, branch FROM fc_intake_source "
                         "WHERE project_id=?", (project_id,)).fetchone()
        return dict(zip(("repo", "source", "base_sha", "branch"), r)) if r else None
    finally:
        if own:
            conn.close()


def run_as_owner() -> Optional[tuple]:
    """FULLCYCLE_INTAKE_RUN_AS: the user the sandboxed worker runs these agents as, so the
    process that makes a project (the API, root, without AGENT_RUN_AS) can hand the tree
    over. (uid, gid), or None when unset. Root or an unknown user refuses."""
    import pwd
    name = os.getenv("FULLCYCLE_INTAKE_RUN_AS", "").strip()
    if not name:
        return None
    try:
        p = pwd.getpwnam(name)
    except KeyError:
        raise specs.SpecError("intake_run_as_unknown_user")
    if p.pw_uid == 0 or p.pw_gid == 0:
        raise specs.SpecError("intake_run_as_is_root")
    return p.pw_uid, p.pw_gid


def _hand_to_agent(path: str) -> None:
    """Give a tree root just made, before any agent ran in it, to the agent's user. The
    sandboxed worker refuses a tree that is not the agent's own (agent_privilege._owns_tree);
    root never runs git in it again after this."""
    owner = run_as_owner()
    if not owner:
        return
    uid, gid = owner
    for top, dirs, files in os.walk(path, followlinks=False):
        os.lchown(top, uid, gid)
        for n in dirs + files:
            os.lchown(os.path.join(top, n), uid, gid)


def _merge_sources(merge_from: list, repo: Optional[str], conn) -> list:
    """[(project_id, root, branch)] for projects started on the same repo."""
    out = []
    root = intake_root()
    for pid in merge_from:
        p = specs.get_project(pid, conn=conn)
        src = source_of(pid, conn=conn)
        if not p or not is_intake(p, root) or not src:
            raise specs.SpecError(f"merge_from_unknown_project:{pid}")
        if src["repo"] != repo:
            raise specs.SpecError(f"merge_from_other_repo:{pid}")
        out.append((pid, p["root"], src["branch"]))
    return out


def _copy_repo(source: dict, path: str, project_id: str, conn,
               merge_from: Optional[list] = None) -> None:
    """The project's own repository, from an allowed one: see core.fullcycle.repos."""
    import shutil
    import time as _time
    from core import agent_privilege
    from core.fullcycle import repos
    try:
        agent_privilege.require_root_work(path)
    except agent_privilege.AgentPrivilegeError as e:
        raise specs.SpecError(f"intake_off_under_agent_run_as:{e}")
    os.mkdir(path, 0o755)         # ours from here on: removed again if the copy fails
    try:
        base = repos.make_copy(source["path"], path, project_id, _GITIGNORE)
        if source.get("seed"):
            repos.seed_copy(source["path"], path, source["seed"])
        for pid, proot, branch in merge_from or []:
            bundle = repos.branch_bundle(proot, branch, keep_raw=intake_root() or "")
            try:
                repos._git(path, "fetch", "-q", "--no-tags", bundle,
                           f"{branch}:refs/heads/fullcycle-in/{pid}")
            finally:
                os.unlink(bundle)
    except (repos.RepoError, OSError, subprocess.SubprocessError,
            agent_privilege.AgentPrivilegeError) as e:
        shutil.rmtree(path, ignore_errors=True)
        raise specs.SpecError(f"repo_copy_failed:{e}")
    conn.execute("INSERT OR REPLACE INTO fc_intake_source VALUES (?,?,?,?,?,?)",
                 (project_id, source["name"], source["path"], base,
                  repos.branch_for(project_id), _time.time()))


def projects(conn=None) -> list:
    """Every intake project, as stored."""
    root = intake_root()
    if not root:
        return []
    conn, own = specs.conn_with_schema(conn)
    try:
        rows = conn.execute("SELECT id,name,root,executor FROM fc_project").fetchall()
    finally:
        if own:
            conn.close()
    out = [dict(zip(("id", "name", "root", "executor"), r)) for r in rows]
    return [p for p in out if is_intake(p, root)]


def _done(project_id: str, conn) -> bool:
    try:
        return bool(conn.execute("SELECT 1 FROM fc_project_done WHERE project_id=?",
                                 (project_id,)).fetchone())
    except Exception:  # noqa: BLE001 — engine tables not created yet: nothing is done
        return False


def active(conn=None) -> list:
    """Intake projects that are not done yet."""
    conn, own = specs.conn_with_schema(conn)
    try:
        return [p for p in projects(conn) if not _done(p["id"], conn)]
    finally:
        if own:
            conn.close()


MERGE_FROM_MAX = 20


def start(project_id: str, spec: dict, *, by: str, repo: Optional[str] = None,
          merge_from: Optional[list] = None, conn=None) -> dict:
    """Create the project and store its spec, or add a new spec version to an intake
    project that exists. With `repo`, the project starts from a copy of that owner-allowed
    repository (core.fullcycle.repos) instead of an empty one. Raises SpecError."""
    root = intake_root()
    if not root:
        raise specs.SpecError("intake_disabled")
    if not by.startswith(BY_PREFIX):
        raise specs.SpecError("intake_needs_an_authenticated_client")
    run_as_owner()                     # a bad setting refuses before anything is made
    merge_from = list(merge_from or [])
    if merge_from:
        if repo is None:
            raise specs.SpecError("merge_from_needs_repo")
        if len(merge_from) > MERGE_FROM_MAX or len(set(merge_from)) != len(merge_from) or \
                project_id in merge_from or \
                not all(isinstance(m, str) and ID_RE.match(m) for m in merge_from):
            raise specs.SpecError(f"merge_from: up to {MERGE_FROM_MAX} distinct other "
                                  "project ids")
    if not ID_RE.match(project_id or ""):
        raise specs.SpecError(f"project id must match {ID_RE.pattern}")
    body = specs.validate_spec(spec)
    path = os.path.join(root, project_id)
    for where, checks in [("spec", body.get("checks") or [])] + \
            [(t["key"], t.get("checks") or []) for t in body["tasks"]]:
        for argv in checks:
            why = gates.check_argv(argv, root=path, trusted=False)
            if why:
                raise specs.SpecError(
                    f"check refused in {where}: {why}. Checks run a test runner on project "
                    f"files, e.g. [\"python3\", \"-m\", \"pytest\", \"-q\", \"tests/\"]; "
                    f"no inline code, nothing outside the project")
    source = None
    if repo is not None:
        from core.fullcycle import repos
        try:
            source = repos.get(repo)
        except repos.RepoError as e:
            raise specs.SpecError(str(e))
    conn, own = specs.conn_with_schema(conn)
    try:
        _source_table(conn)
        existing = specs.get_project(project_id, conn=conn)
        if existing and not is_intake(existing, root):
            raise specs.SpecError("project_exists_outside_intake")
        if existing and _done(project_id, conn):
            raise specs.SpecError("project_done:use_a_new_project_id")
        if existing and merge_from:
            raise specs.SpecError("merge_from_only_when_the_project_is_made")
        if existing and repo is not None:
            had = source_of(project_id, conn=conn)
            if not had or had["repo"] != repo:
                raise specs.SpecError("project_exists_with_another_source")
        created = False
        if not existing:
            if len(active(conn)) >= max_active():
                raise specs.SpecError(f"intake_at_max_active:{max_active()}")
            if os.path.lexists(path):
                raise specs.SpecError("intake_directory_exists")
            if source:
                _copy_repo(source, path, project_id, conn,
                           merge_from=_merge_sources(merge_from, repo, conn))
                if merge_from:
                    conn.execute("CREATE TABLE IF NOT EXISTS fc_intake_merge (into_id TEXT, "
                                 "from_id TEXT, PRIMARY KEY (into_id, from_id))")
                    conn.executemany("INSERT OR IGNORE INTO fc_intake_merge VALUES (?,?)",
                                     [(project_id, m) for m in merge_from])
            else:
                _new_repo(path)
            created = True
            _hand_to_agent(path)
        refused = gates.check_project_root(path)
        if refused:
            raise specs.SpecError(refused)
        p = specs.create_project(project_id, name=body.get("title") or project_id,
                                 root=path, executor="claude", conn=conn)
        s = specs.put_spec(project_id, body, by=by, conn=conn)
        out = {"ok": True, "project": p, "created": created, "spec_version": s["version"],
               "session": session_for(project_id)}
        src = source_of(project_id, conn=conn)
        if src:
            out.update(repo=src["repo"], branch=src["branch"], base_sha=src["base_sha"])
        if merge_from:
            out["merge_branches"] = [f"fullcycle-in/{m}" for m in merge_from]
        return out
    finally:
        if own:
            conn.close()


# -- the agent ------------------------------------------------------------------------
def agent_alive(session: str) -> bool:
    from core import agent_control as ac
    rc, _, _ = ac._tmux(["has-session", "-t", f"={session}"])
    return rc == 0


def start_agent(project: dict) -> dict:
    """Through the creation policy, never around it."""
    from core import agent_control as ac
    return ac.agent_resume(project["root"], session_name=session_for(project["id"]),
                           actor=ACTOR)


def _own_pane(project: dict) -> bool:
    """The `fc-<id>` session exists and every pane of it runs in the project directory."""
    from core import agent_control as ac
    session = session_for(project["id"])
    rc, out, _ = ac._tmux(["list-panes", "-t", f"={session}:", "-F", "#{pane_current_path}"])
    if rc != 0:
        return False
    paths = {os.path.realpath(x.strip()) for x in out.splitlines() if x.strip()}
    real = os.path.realpath(project["root"])
    return bool(paths) and all(p == real or p.startswith(real + os.sep) for p in paths)


def answer_trust(project: dict) -> bool:
    """A new directory makes `claude` ask whether to trust it, with "No, exit" selected;
    the first task's Enter would pick it and the agent would quit. The worker made this
    directory itself, so it answers yes, here and nowhere else: only in the project's own
    session, only when the screen names this very directory."""
    from core import agent_control as ac
    if not _own_pane(project):
        return False
    return bool(ac.accept_folder_trust(session_for(project["id"]), project["root"],
                                       actor=ACTOR).get("ok"))


def stop_agent(project: dict) -> bool:
    """Stop this project's own agent: only the `fc-<id>` session whose pane runs in the
    project's directory. Anything else is left alone."""
    from core import agent_control as ac
    session = session_for(project["id"])
    if not _own_pane(project):
        return False
    _, out, _ = ac._tmux(["list-panes", "-t", f"={session}:", "-F",
                          "#{session_name}:#{window_index}.#{pane_index}"])
    panes = [x.strip() for x in (out or "").splitlines() if x.strip()]
    rc, _, _ = ac._tmux(["kill-session", "-t", f"={session}"])
    if rc == 0:
        ac.audit("agent_stop", session, stopped=True, actor=ACTOR,
                 cwd=os.path.realpath(project["root"]),
                 reason="fullcycle_intake_project_done")
        _unwatch(panes)
    return rc == 0


# A pane the worker closed on purpose is not a crash. Without this, agent-watch saw the
# pane vanish on its next sweeps and published `agent_process_failed` (critical, owner
# action required) for every finished project (events 81605, 81620 on 2026-11-09).
UNWATCH_SECS = 6 * 3600


def _unwatch(panes: list) -> None:
    try:
        from core import agent_watch as aw
        for target in panes:
            aw.retire(target, reason="fullcycle_intake_project_done", by=ACTOR)
            # a sweep that read the pane just before the kill can write its state back
            # after the retire; the suppression keeps that stale row from alerting
            aw.suppress(target, ttl_secs=UNWATCH_SECS,
                        reason="fullcycle_intake_project_done", by=ACTOR)
    except Exception:  # noqa: BLE001 — a missed unwatch costs a false alert, not the stop
        pass


# A claude that has just drawn its prompt can drop the first Enter: the task text then sits
# typed in the composer and nothing runs (seen 2026-11-07 on the stand, sent 1 s after
# start). A new agent counts as ready only after this long idle.
SETTLE_SECS = float(os.getenv("FULLCYCLE_INTAKE_SETTLE_SECS", "15"))


def ensure_agent(project: dict, *, wait: float = 90.0, sleep=None, clock=None) -> dict:
    """The project's agent running, past the trust prompt, idle at its prompt.
    {"ok", "created", "reason"}. Creation goes through the policy; nothing else is done."""
    import time as _time
    from core import agent_control as ac
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    session = session_for(project["id"])
    created = False
    if agent_alive(session):
        if answer_trust(project):
            return {"ok": False, "created": False, "reason": "trust_answered"}
        try:
            st = ac.agent_status(session, actor=ACTOR)
        except Exception:  # noqa: BLE001
            return {"ok": False, "created": False, "reason": "agent_status_unreadable"}
        if st.get("is_agent"):
            return {"ok": True, "created": False, "reason": "running"}
        # the session outlived its claude: clear it, a new agent is started below
        if not stop_agent(project):
            return {"ok": False, "created": False, "reason": "session_without_agent"}
    if not agent_alive(session):
        r = start_agent(project)
        if not r.get("agent_created"):
            return {"ok": False, "created": False,
                    "reason": r.get("refused") or r.get("error") or r.get("reason")}
        created = True
    end = clock() + wait
    idle_since = None
    while True:
        if answer_trust(project):
            idle_since = None
        else:
            try:
                st = ac.agent_status(session, actor=ACTOR)
            except Exception:  # noqa: BLE001 — still starting
                st = {}
            if st.get("alive") and st.get("is_agent") and st.get("state") == "idle":
                now = clock()
                idle_since = now if idle_since is None else idle_since
                if now - idle_since >= SETTLE_SECS:
                    return {"ok": True, "created": created, "reason": "ready"}
            else:
                idle_since = None
        if clock() >= end:
            return {"ok": False, "created": created, "reason": "agent_not_ready"}
        sleep(3)


def adapter(project: dict):
    """The engine's adapter for an intake project: it owns the agent."""
    from core.fullcycle.tmux_adapter import AgentControlAdapter

    class IntakeAdapter(AgentControlAdapter):
        name = "intake"
        owns_agent = True
        lead_resolves_gates = True    # its own sandboxed copy and agent

        def __init__(self, p: dict):
            super().__init__(session_for(p["id"]), actor=ACTOR)
            self.project = p
            self.fresh = False        # this adapter started the running agent
            self.sent = set()         # keys sent to that agent

        def prepare(self) -> dict:
            """Before a tick: an agent ready for a send. A newly started agent cannot
            hold an attempt sent to the one before it."""
            r = ensure_agent(self.project)
            if r.get("created"):
                self.fresh, self.sent = True, set()
            return r

        def submit(self, env, idempotency_key: str) -> dict:
            r = super().submit(env, idempotency_key)
            if r.get("ok"):
                self.sent.add(idempotency_key)
            return r

        def poll(self, handle: dict):
            from core.fullcycle.executor import CRASHED, DONE, Observation
            obs = super().poll(handle)
            if obs.state != DONE and self.fresh and handle.get("key") not in self.sent:
                # sent to an agent that is gone: the engine's crash path resends it
                return Observation(CRASHED, {"reason": "agent_replaced"})
            return obs

        def restart(self, handle: dict) -> dict:
            r = self.prepare()
            return {"ok": bool(r.get("ok")), "reason": r.get("reason")}

    return IntakeAdapter(project)


# -- the owner hears when a project is finished ----------------------------------------
_NOTICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_intake_notice (
    project_id TEXT PRIMARY KEY, state TEXT, attempts INTEGER DEFAULT 0, ts REAL,
    receipt TEXT DEFAULT '', error TEXT DEFAULT ''
)"""
NOTICE_MAX_ATTEMPTS = 5
NOTICE_RETRY_SECS = 120
_DONE_STATES = ("VERIFIED_DONE", "CANCELLED")


def _notice_table(conn) -> None:
    fresh = not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='fc_intake_notice'").fetchone()
    conn.execute(_NOTICE_SCHEMA)
    if fresh:
        # projects finished before this existed were seen by the owner already
        import time as _time
        for p in projects(conn):
            if _done(p["id"], conn):
                conn.execute("INSERT OR IGNORE INTO fc_intake_notice (project_id, state, ts) "
                             "VALUES (?,?,?)", (p["id"], "preexisting", _time.time()))
        conn.commit()


def _git_out(cwd: str, *args: str) -> str:
    """git in a project tree. In a sandboxed worker it runs as the agent: the tree is the
    agent's, and root git would load its config."""
    from core import agent_privilege as priv
    argv, env = ["git", *args], None
    try:
        t = priv.target()
    except priv.AgentPrivilegeError:
        return ""
    if t:
        argv, env = priv.host_argv(t, ["git", "--no-pager", *args]), priv.child_env(t)
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30,
                           env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def notice_text(project: dict, conn) -> str:
    """What the owner reads on the phone. Facts from the engine and from git only."""
    from core.fullcycle import engine as fe
    st = fe.project_status(project["id"], conn=conn)
    tasks = st.get("tasks") or []
    done = [t for t in tasks if t["state"] == "VERIFIED_DONE"]
    cancelled = [t for t in tasks if t["state"] == "CANCELLED"]
    rows = conn.execute("SELECT decision, COUNT(*) FROM fc_review WHERE project_id=? AND "
                        "decision IS NOT NULL GROUP BY decision", (project["id"],)).fetchall()
    decisions = ", ".join(f"{d} {n}" for d, n in sorted(rows)) or "none"
    src = source_of(project["id"], conn=conn)
    root = project["root"]
    base = src["base_sha"] if src else \
        (_git_out(root, "rev-list", "--max-parents=0", "HEAD").splitlines() or [""])[0]
    log = _git_out(root, "log", "--no-decorate", "--format=%h %s", "-n", "12",
                   f"{base}..HEAD") if base else ""
    head = _git_out(root, "rev-parse", "--short", "HEAD")
    mark = "✅" if tasks and not cancelled else "⚠️"
    lines = [f"{mark} Full Cycle: {project['id']} finished",
             f"Tasks: {len(done)} done, {len(cancelled)} cancelled, of {len(tasks)}"]
    lines += [f"  {t['key']}: {t['state']}" for t in tasks[:15]]
    lines.append(f"Reviews: {decisions}")
    lines.append(f"Checks: passed for every done task (engine-run)" if done else
                 "Checks: no task passed")
    if src:
        lines.append(f"Repo: {src['repo']}, from {src['base_sha'][:7]}, "
                     f"branch {src['branch']} at {head}")
    else:
        lines.append(f"New repo: {root}, head {head}")
    lines.append("Commits:" if log else "Commits: none")
    lines += ["  " + x[:100] for x in log.splitlines()]
    if src and log:
        lines.append(f"Take it: git -C {src['source']} fetch {root} {src['branch']}")
        full = _git_out(root, "rev-parse", "HEAD")
        try:
            from core.fullcycle import release
            link = release.make_link(project["id"], full, conn=conn)
        except Exception:  # noqa: BLE001 — a notice without a link is still a notice
            link = None
        if link:
            lines.append(f"Выпустить (fast-forward {src['source']}, одноразово, 24 ч): {link}")
    lines.append("Nothing was pushed or deployed.")
    return "\n".join(lines)


def notify_finished(send=None, conn=None, now: Optional[float] = None) -> list:
    """Tell the owner once about each finished intake project: done, and no task left
    in flight. Retries a failed send a few times, then gives up and records why."""
    import time as _time
    if send is None:
        from core.control_plane.delivery import _send_owner_push as send
    from core.fullcycle import engine as fe
    now = now if now is not None else _time.time()
    conn, own = fe.connect(conn)
    out = []
    try:
        if not intake_root():
            return out
        _notice_table(conn)
        for p in projects(conn):
            if not _done(p["id"], conn):
                continue
            row = conn.execute("SELECT state, attempts, ts FROM fc_intake_notice WHERE "
                               "project_id=?", (p["id"],)).fetchone()
            if row and (row[0] in ("sent", "preexisting", "gave_up") or
                        now - (row[2] or 0) < NOTICE_RETRY_SECS):
                continue
            states = [t["state"] for t in fe.project_status(p["id"], conn=conn)["tasks"]]
            if not states or any(s not in _DONE_STATES for s in states):
                continue
            attempts = (row[1] if row else 0) + 1
            try:
                ok, receipt, err = send(notice_text(p, conn))
            except Exception as e:  # noqa: BLE001 — a failed notice never stops the worker
                ok, receipt, err = False, None, f"{type(e).__name__}: {e}"
            state = "sent" if ok else ("gave_up" if attempts >= NOTICE_MAX_ATTEMPTS
                                       else "retry")
            conn.execute("INSERT OR REPLACE INTO fc_intake_notice VALUES (?,?,?,?,?,?)",
                         (p["id"], state, attempts, now, receipt or "",
                          (err or "")[:300]))
            conn.commit()
            out.append({"project": p["id"], "state": state, "attempts": attempts,
                        **({"error": (err or "")[:160]} if not ok else {})})
    finally:
        if own:
            conn.close()
    return out
