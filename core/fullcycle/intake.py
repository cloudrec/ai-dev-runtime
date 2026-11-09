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


def start(project_id: str, spec: dict, *, by: str, conn=None) -> dict:
    """Create the project and store its spec, or add a new spec version to an intake
    project that exists. Raises SpecError with the reason."""
    root = intake_root()
    if not root:
        raise specs.SpecError("intake_disabled")
    if not by.startswith(BY_PREFIX):
        raise specs.SpecError("intake_needs_an_authenticated_client")
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
    conn, own = specs.conn_with_schema(conn)
    try:
        existing = specs.get_project(project_id, conn=conn)
        if existing and not is_intake(existing, root):
            raise specs.SpecError("project_exists_outside_intake")
        if existing and _done(project_id, conn):
            raise specs.SpecError("project_done:use_a_new_project_id")
        created = False
        if not existing:
            if len(active(conn)) >= max_active():
                raise specs.SpecError(f"intake_at_max_active:{max_active()}")
            if os.path.lexists(path):
                raise specs.SpecError("intake_directory_exists")
            _new_repo(path)
            created = True
        refused = gates.check_project_root(path)
        if refused:
            raise specs.SpecError(refused)
        p = specs.create_project(project_id, name=body.get("title") or project_id,
                                 root=path, executor="claude", conn=conn)
        s = specs.put_spec(project_id, body, by=by, conn=conn)
        return {"ok": True, "project": p, "created": created, "spec_version": s["version"],
                "session": session_for(project_id)}
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
