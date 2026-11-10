"""Projects the GPT tech lead starts through MCP: spec in, own repo, own agent, worker tick."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

from core import agent_control as ac
from core import agent_creation_policy as acp
from core import wake_bridge as wb
from core.fullcycle import engine as fe
from core.fullcycle import intake
from core.fullcycle import spec as specs

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
BY = "api:client/chatgpt-tech-lead"
SPEC = {"title": "calc", "goal": "a calculator", "acceptance_criteria": ["adds"],
        "checks": [["python3", "-m", "pytest", "-q", "tests"]],
        "tasks": [{"key": "add", "title": "add", "goal": "add()", "acceptance_criteria": ["ok"],
                   "checks": [["python3", "-m", "pytest", "-q", "tests/test_add.py"]]}]}


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / "fc"
    root.mkdir()
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", str(root))
    monkeypatch.delenv("FULLCYCLE_INTAKE_MAX_ACTIVE", raising=False)
    return root


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True).stdout


# -- start ---------------------------------------------------------------------------
def test_off_without_a_root(box, monkeypatch):
    monkeypatch.delenv("FULLCYCLE_INTAKE_ROOT")
    with pytest.raises(specs.SpecError, match="intake_disabled"):
        intake.start("calc", SPEC, by=BY)


def test_a_symlinked_root_is_off(box, tmp_path, monkeypatch):
    link = tmp_path / "link"
    link.symlink_to(box)
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", str(link))
    assert intake.intake_root() is None


def test_start_makes_a_clean_repo_and_a_project(box):
    r = intake.start("calc", SPEC, by=BY)
    path = box / "calc"
    assert r["created"] and r["spec_version"] == 1 and r["session"] == "fc-calc"
    assert r["project"]["root"] == str(path)
    assert _git(path, "log", "--format=%s").split() == ["baseline"]
    assert _git(path, "status", "--porcelain") == ""
    assert ".fullcycle/" in (path / ".gitignore").read_text()
    assert specs.get_spec("calc")["by"] == BY


@pytest.mark.parametrize("bad", ["", "Calc", "1calc", "ab", "a/b", "../x", "a" * 40, "a.b"])
def test_bad_ids_are_refused(box, bad):
    with pytest.raises(specs.SpecError):
        intake.start(bad, SPEC, by=BY)
    assert os.listdir(box) == []


def test_only_an_authenticated_client_may_start(box):
    with pytest.raises(specs.SpecError, match="authenticated"):
        intake.start("calc", SPEC, by="owner")


def test_an_invalid_spec_creates_nothing(box):
    with pytest.raises(specs.SpecError):
        intake.start("calc", {"title": "x"}, by=BY)
    assert os.listdir(box) == []


def test_an_existing_directory_is_never_taken_over(box):
    (box / "calc").mkdir()
    (box / "calc" / "keep.txt").write_text("mine")
    with pytest.raises(specs.SpecError, match="intake_directory_exists"):
        intake.start("calc", SPEC, by=BY)
    assert os.listdir(box / "calc") == ["keep.txt"]


def test_a_second_start_adds_a_spec_version(box):
    intake.start("calc", SPEC, by=BY)
    more = {**SPEC, "tasks": SPEC["tasks"] + [{"key": "sub", "title": "s", "goal": "sub()",
                                                  "acceptance_criteria": ["ok"]}]}
    r = intake.start("calc", more, by=BY)
    assert not r["created"] and r["spec_version"] == 2


def test_a_project_outside_intake_cannot_be_reached(box, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    specs.create_project("taskboard", name="t", root=str(other), executor="claude")
    with pytest.raises(specs.SpecError, match="outside_intake"):
        intake.start("taskboard", SPEC, by=BY)
    assert specs.get_spec("taskboard") is None


def test_a_finished_project_takes_no_new_spec(box):
    intake.start("calc", SPEC, by=BY)
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES ('calc', 1, '', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(specs.SpecError, match="project_done"):
        intake.start("calc", SPEC, by=BY)


def test_at_most_max_active_projects(box, monkeypatch):
    monkeypatch.setenv("FULLCYCLE_INTAKE_MAX_ACTIVE", "1")
    intake.start("one", SPEC, by=BY)
    with pytest.raises(specs.SpecError, match="max_active"):
        intake.start("two", SPEC, by=BY)
    assert not (box / "two").exists()
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES ('one', 1, '', 0)")
    conn.commit()
    conn.close()
    assert intake.start("two", SPEC, by=BY)["created"]


def test_active_lists_only_unfinished_intake_projects(box, tmp_path):
    intake.start("one", SPEC, by=BY)
    intake.start("two", SPEC, by=BY)
    specs.create_project("elsewhere", name="e", root=str(tmp_path), executor="claude")
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_project_done VALUES ('one', 1, '', 0)")
    conn.commit()
    conn.close()
    assert [p["id"] for p in intake.active()] == ["two"]


# -- the reviewer's checks stay untrusted ----------------------------------------------
class _Adapter:
    session = name = "fc-calc"


def test_tasks_of_an_api_spec_are_untrusted(box):
    intake.start("calc", SPEC, by=BY)
    e = fe.Engine("calc", _Adapter(), holder="t")
    e._seed()
    (task,) = e._tasks()
    assert task["origin"] == BY
    assert e._trusted(task) is False


@pytest.mark.parametrize("argv,why", [
    (["python3", "-c", "import os; os.system('id')"], "check_inline_code_not_allowed"),
    (["sh", "-c", "id"], "check_runner_not_allowed:sh"),
    (["python3", "-m", "pytest", "/etc"], "check_path_outside_project"),
    (["python3", "-m", "pytest", "../x"], "check_path_escape"),
])
@pytest.mark.parametrize("level", ["spec", "task"])
def test_untrusted_checks_are_refused_at_start(box, argv, why, level):
    spec = {**SPEC, "checks": [argv]} if level == "spec" else \
        {**SPEC, "tasks": [{**SPEC["tasks"][0], "checks": [argv]}]}
    with pytest.raises(specs.SpecError, match=why):
        intake.start("calc", spec, by=BY)
    assert os.listdir(box) == []


def test_project_checks_of_an_api_spec_are_untrusted(box):
    intake.start("calc", SPEC, by=BY)
    assert fe.Engine("calc", _Adapter(), holder="t")._spec_trusted() is False


def test_project_checks_keep_the_trust_of_whoever_wrote_them(box, tmp_path):
    specs.create_project("own", name="own", root=str(tmp_path), executor="claude")
    specs.put_spec("own", SPEC, by="owner")
    e = fe.Engine("own", _Adapter(), holder="t")
    assert e._spec_trusted() is True
    specs.put_spec("own", {**SPEC, "goal": "more tasks, same checks"}, by=BY)
    assert e._spec_trusted() is True
    specs.put_spec("own", {**SPEC, "checks": [["sh", "-c", "id"]]}, by=BY)
    assert e._spec_trusted() is False
    specs.put_spec("own", {**SPEC, "checks": [["sh", "-c", "id"]], "goal": "g2"}, by="review:9")
    assert e._spec_trusted() is False


def test_tasks_of_an_owner_spec_stay_trusted(box, tmp_path):
    specs.create_project("own", name="own", root=str(tmp_path), executor="claude")
    specs.put_spec("own", SPEC, by="owner")
    e = fe.Engine("own", _Adapter(), holder="t")
    e._seed()
    assert e._trusted(e._tasks()[0]) is True


# -- creation policy: a scope with a root ---------------------------------------------
def _cfg(root):
    return {"scopes": {"fc": {"callers": ["fullcycle:engine"], "session_prefix": "fc-",
                              "root": str(root), "max_concurrent": 2}}}


@pytest.fixture
def no_reservations(monkeypatch):
    monkeypatch.setattr(acp, "reserved_cwds", lambda now=None: [])


def test_scope_root_allows_a_direct_subdirectory(box, no_reservations):
    (box / "calc").mkdir()
    r = acp.authorize(str(box / "calc"), "fc-calc", caller="fullcycle:engine", cfg=_cfg(box))
    assert r == {"ok": True, "reason": "scope", "scope": "fc"}


def test_scope_root_refuses_the_api(box, no_reservations):
    (box / "calc").mkdir()
    r = acp.authorize(str(box / "calc"), "fc-calc", caller="api", cfg=_cfg(box))
    assert r["reason"] == "creation_caller_not_allowed"


def test_scope_root_refuses_another_session_name(box, no_reservations):
    (box / "calc").mkdir()
    r = acp.authorize(str(box / "calc"), "gaika-ru", caller="fullcycle:engine", cfg=_cfg(box))
    assert r["reason"] == "creation_session_name_mismatch"


@pytest.mark.parametrize("where", ["", "calc/deeper"])
def test_scope_root_refuses_the_root_and_deeper_dirs(box, no_reservations, where):
    target = box / where if where else box
    target.mkdir(parents=True, exist_ok=True)
    r = acp.authorize(str(target), "fc-" + os.path.basename(str(target)),
                      caller="fullcycle:engine", cfg=_cfg(box))
    assert r["reason"] == "creation_not_in_any_scope"


def test_scope_root_refuses_a_symlinked_subdirectory(box, tmp_path, no_reservations):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (box / "calc").symlink_to(real)
    r = acp.authorize(str(box / "calc"), "fc-calc", caller="fullcycle:engine", cfg=_cfg(box))
    assert r["ok"] is False


def test_scope_root_counts_live_agents(box, no_reservations):
    for n in ("a1c", "b2c", "c3c"):
        (box / n).mkdir()
    live = [str(box / "a1c"), str(box / "b2c")]
    r = acp.authorize(str(box / "c3c"), "fc-c3c", caller="fullcycle:engine", cfg=_cfg(box),
                      live_cwds=live)
    assert r["reason"] == "creation_scope_at_max_concurrent"


def test_the_shipped_config_scopes_intake_to_the_worker():
    cfg = acp.load_config(os.path.join(ROOT, "config", "agent_creation.yaml"))
    sc = cfg["scopes"]["fullcycle-intake"]
    assert sc["callers"] == ["fullcycle:engine"] and sc["session_prefix"] == "fc-"
    assert sc["root"] == "/opt/fc-projects" and sc["max_concurrent"] == 8


# -- allowlist and wake filter prefixes --------------------------------------------------
@pytest.mark.parametrize("entry,session,caller,ok", [
    ("fc-*@fullcycle:engine", "fc-calc", "fullcycle:engine", True),
    ("fc-*@fullcycle:engine", "fc-calc", "api", False),
    ("fc-*@fullcycle:engine", "gaika-ru", "fullcycle:engine", False),
    ("fc-*", "fc-calc", "api", False),              # a prefix needs a named caller
    ("f*@fullcycle:engine", "fc-calc", "fullcycle:engine", False),   # too short
    ("f*c-*@fullcycle:engine", "fc-calc", "fullcycle:engine", False),
    ("fc-calc@fullcycle:engine", "fc-calc", "fullcycle:engine", True),
])
def test_allowlist_prefix(monkeypatch, entry, session, caller, ok):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", entry)
    assert ac._allowlisted(session, f"{session}:0.0", caller) is ok


@pytest.mark.parametrize("only,agent,ok", [
    ({"fc-*"}, "fc-calc", True), ({"fc-*"}, "fc-calc:0.0", True),
    ({"fc-*"}, "gaika-ru", False), ({"f*"}, "fc-calc", False),
    ({"gaika-ru"}, "gaika-ru:0.0", True), (set(), "anything", True), ({"fc-*"}, "", False),
])
def test_wake_filter_prefix(only, agent, ok):
    assert wb._offer_allowed(agent, only) is ok


# -- the worker --------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location(
    "fullcycle_worker_intake", os.path.join(ROOT, "tools", "fullcycle_worker.py"))
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)


class FakeIntake:
    def __init__(self, todo, alive=(), start=None):
        self.todo, self.alive, self.started, self.stopped = todo, set(alive), [], []
        self._start = start or {"agent_created": True}

    def active(self):
        return self.todo

    def projects(self):
        return self.todo

    def session_for(self, pid):
        return "fc-" + pid

    trust = False

    def agent_alive(self, s):
        return s in self.alive

    def answer_trust(self, p):
        return self.trust

    def start_agent(self, p):
        self.started.append(p["id"])
        return self._start

    def stop_agent(self, p):
        self.stopped.append(p["id"])
        return True


def test_worker_adds_unfinished_intake_projects():
    fi = FakeIntake([{"id": "calc"}, {"id": "tb"}])
    assert worker.round_projects({"tb": "tb-s"}, fi) == {"tb": "tb-s", "calc": {"id": "calc"}}


def test_worker_keeps_configured_projects_when_intake_breaks(capsys):
    class Broken(FakeIntake):
        def active(self):
            raise OSError("db")
    assert worker.round_projects({"tb": "tb-s"}, Broken([])) == {"tb": "tb-s"}
    assert "intake_error" in capsys.readouterr().out


@pytest.mark.parametrize("result,stopped", [
    ({"project": "done", "tasks": {"a": "VERIFIED_DONE", "b": "CANCELLED"}}, True),
    ({"project": "done", "tasks": {"a": "VERIFIED_DONE", "b": "WORKING"}}, False),
    ({"project": None, "tasks": {"a": "VERIFIED_DONE"}}, False),
    ({"project": "done", "tasks": {}}, False),
])
def test_worker_stops_the_agent_only_when_all_is_done(result, stopped):
    fi = FakeIntake([{"id": "calc"}])
    worker.settle_intake("calc", result, fi)
    assert fi.stopped == (["calc"] if stopped else [])


# -- stopping touches only the project's own pane -----------------------------------------
@pytest.mark.parametrize("pane_path,killed", [("{root}", True), ("{root}/sub", True),
                                               ("/opt/elsewhere", False), ("", False)])
def test_stop_agent_checks_where_the_pane_runs(box, monkeypatch, pane_path, killed):
    (box / "calc").mkdir()
    root = str(box / "calc")
    calls = []

    def fake_tmux(args, stdin=None):
        calls.append(args[0])
        if args[0] == "list-panes":
            return (0, pane_path.format(root=root) + "\n", "") if pane_path else (1, "", "")
        return (0, "", "")
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    assert intake.stop_agent({"id": "calc", "root": root}) is killed
    assert ("kill-session" in calls) is killed


TRUST_SCREEN = (" Accessing workspace:\n {root}\n Quick safety check\n"
                " ❯ No, exit\n   Yes, I trust this folder\n")


@pytest.mark.parametrize("screen,pane,answered", [
    (TRUST_SCREEN, "{root}", True),
    (TRUST_SCREEN.replace("{root}", "/opt/other"), "{root}", False),   # names another dir
    (TRUST_SCREEN, "/opt/elsewhere", False),                            # pane elsewhere
    ("❯ Try something", "{root}", False),                              # no dialog
])
def test_answer_trust_only_for_its_own_new_directory(box, monkeypatch, screen, pane, answered):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "fc-*@fullcycle:engine")
    (box / "calc").mkdir()
    root = str(box / "calc")
    sent = []

    def fake_tmux(args, stdin=None):
        if args[0] == "list-panes":
            return 0, pane.format(root=root) + "\n", ""
        if args[0] == "capture-pane":
            return 0, screen.format(root=root), ""
        if args[0] == "send-keys":
            sent.append(args[-1])
        return 0, "", ""
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    assert intake.answer_trust({"id": "calc", "root": root}) is answered
    assert sent == (["Down", "Enter"] if answered else [])


def test_answer_trust_reads_a_wrapped_path(box, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "fc-*@fullcycle:engine")
    (box / "calc").mkdir()
    root = str(box / "calc")
    wrapped = root[:10] + "\n " + root[10:]
    sent = []

    def fake_tmux(args, stdin=None):
        if args[0] == "list-panes":
            return 0, root + "\n", ""
        if args[0] == "capture-pane":
            return 0, TRUST_SCREEN.replace("{root}", wrapped), ""
        if args[0] == "send-keys":
            sent.append(args[-1])
        return 0, "", ""
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    assert intake.answer_trust({"id": "calc", "root": root}) is True


# -- ensure_agent and the adapter -----------------------------------------------------------
class Agents:
    """Stand-in for tmux + agent_control around one project's session."""

    def __init__(self, monkeypatch, *, alive=False, is_agent=True, states=("idle",),
                 create=True, trust=False, settle=0):
        self.alive, self.is_agent, self.states = alive, is_agent, list(states)
        self.create, self.trust, self.started, self.stopped = create, trust, 0, 0
        monkeypatch.setattr(intake, "agent_alive", lambda s: self.alive)
        monkeypatch.setattr(intake, "start_agent", self._start)
        monkeypatch.setattr(intake, "stop_agent", self._stop)
        monkeypatch.setattr(intake, "answer_trust", self._trust)
        monkeypatch.setattr(ac, "agent_status", self._status)
        monkeypatch.setattr(intake, "SETTLE_SECS", settle)

    def _start(self, p):
        if not self.create:
            return {"agent_created": False, "refused": "creation_scope_at_max_concurrent"}
        self.started += 1
        self.alive, self.is_agent = True, True
        return {"agent_created": True}

    def _stop(self, p):
        self.stopped += 1
        self.alive = False
        return True

    def _trust(self, p):
        t, self.trust = self.trust, False
        return t

    def _status(self, s, actor=None):
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"alive": self.alive, "is_agent": self.is_agent, "state": st}


P = {"id": "calc", "root": "/opt/fc-projects/calc"}


def _clock():
    t = [0.0]

    def now():
        t[0] += 3
        return t[0]
    return now


def test_ensure_starts_and_waits_for_idle(monkeypatch):
    a = Agents(monkeypatch, states=("starting", "starting", "idle"), trust=True)
    r = intake.ensure_agent(P, sleep=lambda s: None, clock=_clock())
    assert r == {"ok": True, "created": True, "reason": "ready"} and a.started == 1


def test_ensure_wants_a_new_agent_idle_for_a_while(monkeypatch):
    Agents(monkeypatch, states=("idle", "working") * 20, settle=6)   # never idle for long
    r = intake.ensure_agent(P, wait=60, sleep=lambda s: None, clock=_clock())
    assert r["reason"] == "agent_not_ready"


def test_ensure_gives_up_when_the_agent_never_settles(monkeypatch):
    Agents(monkeypatch, states=("starting",))
    r = intake.ensure_agent(P, wait=10, sleep=lambda s: None, clock=_clock())
    assert r == {"ok": False, "created": True, "reason": "agent_not_ready"}


def test_ensure_reports_a_refused_creation(monkeypatch):
    Agents(monkeypatch, create=False)
    r = intake.ensure_agent(P, sleep=lambda s: None, clock=_clock())
    assert r["ok"] is False and r["reason"] == "creation_scope_at_max_concurrent"


def test_ensure_does_not_wait_on_a_running_agent(monkeypatch):
    a = Agents(monkeypatch, alive=True, states=("working",))
    assert intake.ensure_agent(P)["reason"] == "running" and a.started == 0


def test_ensure_answers_trust_on_a_running_session_and_holds_the_tick(monkeypatch):
    Agents(monkeypatch, alive=True, trust=True)
    assert intake.ensure_agent(P) == {"ok": False, "created": False, "reason": "trust_answered"}


def test_ensure_replaces_a_session_whose_claude_died(monkeypatch):
    a = Agents(monkeypatch, alive=True, is_agent=False)
    r = intake.ensure_agent(P, sleep=lambda s: None, clock=_clock())
    assert r["ok"] and r["created"] and a.stopped == 1 and a.started == 1


def test_adapter_owns_its_agent_and_resends_what_the_old_one_had(monkeypatch, tmp_path):
    from core.fullcycle.executor import CRASHED, WORKING
    Agents(monkeypatch, states=("idle", "working"))
    ad = intake.adapter(P)
    assert ad.owns_agent is True and ad.target == "fc-calc"
    assert ad.prepare()["created"] is True
    old = {"key": "k1", "result": str(tmp_path / "none.json")}
    assert ad.poll(old).state == CRASHED                    # sent before this agent
    monkeypatch.setattr(type(ad).__mro__[1], "submit",
                        lambda self, env, key: {"ok": True, "handle": {}})
    ad.submit(None, "k2")
    assert ad.poll({"key": "k2", "result": str(tmp_path / "none.json")}).state == WORKING
    assert ad.restart(old) == {"ok": True, "reason": "running"}


def test_adapter_without_a_new_agent_trusts_the_running_one(monkeypatch, tmp_path):
    from core.fullcycle.executor import WORKING
    Agents(monkeypatch, alive=True, states=("working",))
    ad = intake.adapter(P)
    ad.prepare()
    assert ad.poll({"key": "k1", "result": str(tmp_path / "none.json")}).state == WORKING


def test_trust_answer_follows_the_automation_policy(box, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "")
    root = str(box / "calc")
    sent = []

    def fake_tmux(args, stdin=None):
        if args[0] == "capture-pane":
            return 0, TRUST_SCREEN.format(root=root), ""
        if args[0] == "send-keys":
            sent.append(args[-1])
        return 0, "", ""
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    r = ac.accept_folder_trust("fc-calc", root, actor="fullcycle:engine")
    assert r["refused"] == "automation_target_not_allowlisted" and sent == []


def test_intake_refuses_under_agent_run_as(box, monkeypatch):
    from core import agent_privilege
    monkeypatch.setattr(agent_privilege, "target", lambda: object())
    with pytest.raises(specs.SpecError, match="intake_off_under_agent_run_as"):
        intake.start("calc", SPEC, by=BY)
    assert os.listdir(box) == []


def test_project_acceptance_runs_api_checks_untrusted(box, monkeypatch):
    from core import task_contract as tc
    intake.start("calc", SPEC, by=BY)
    e = fe.Engine("calc", _Adapter(), holder="t")
    seen = []

    class Stop(Exception):
        pass

    def run(checks, trusted):
        seen.append(trusted)
        raise Stop
    monkeypatch.setattr(e, "_run_checks", run)
    with pytest.raises(Stop):
        e._project_step([{"state": tc.VERIFIED_DONE, "committed": 1, "key": "add"}])
    assert seen == [False]


def test_scope_root_refuses_an_alias_inside_the_root(box, no_reservations):
    (box / "calc").mkdir()
    (box / "alias").symlink_to(box / "calc")
    r = acp.authorize(str(box / "alias"), "fc-calc", caller="fullcycle:engine", cfg=_cfg(box))
    assert r["reason"] == "creation_scope_worktree_is_symlink"


# -- a pane the worker closed is not a crash (events 81605, 81620 on 2026-11-09) ----------
_WORKING = "✻ Compacting… (esc to interrupt · 32.1k tokens)"


class _Emit:
    def __init__(self):
        self.types = []

    def __call__(self, source, etype, **kw):
        self.types.append(etype)
        return {"event_id": len(self.types)}


def _watch_then_stop(box, monkeypatch, *, stop: bool):
    from core import agent_watch as aw
    (box / "calc").mkdir()
    root = str(box / "calc")
    target = "fc-calc:0.0"

    def fake_tmux(args, stdin=None):
        if args[0] == "list-panes":
            fmt = args[args.index("-F") + 1]
            return 0, (root if "path" in fmt else target) + "\n", ""
        return 0, "", ""
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    emit = _Emit()
    agent = {"target": target, "cwd": root, "claude_cwd": root, "alive": True,
             "is_agent": True, "state": "working"}
    aw.scan(agents=[agent], read_fn=lambda t: _WORKING, emit_fn=emit, now=1000.0)
    if stop:
        assert intake.stop_agent({"id": "calc", "root": root}) is True
    for now in (1100.0, 1200.0, 1300.0):
        aw.scan(agents=[], read_fn=lambda t: "", emit_fn=emit, now=now)
    return emit.types


def test_a_pane_stopped_by_the_worker_raises_no_crash(box, monkeypatch):
    assert "agent_process_failed" not in _watch_then_stop(box, monkeypatch, stop=True)


def test_the_same_pane_vanishing_on_its_own_is_still_a_crash(box, monkeypatch):
    assert "agent_process_failed" in _watch_then_stop(box, monkeypatch, stop=False)


def test_a_sweep_racing_the_stop_cannot_bring_the_alert_back(box, monkeypatch):
    """A sweep that read the pane before the kill writes its row after the retire."""
    from core import agent_watch as aw
    _watch_then_stop(box, monkeypatch, stop=True)
    emit = _Emit()
    agent = {"target": "fc-calc:0.0", "cwd": "/x", "claude_cwd": "/x", "alive": True,
             "is_agent": True, "state": "working"}
    aw.scan(agents=[agent], read_fn=lambda t: _WORKING, emit_fn=emit, now=1400.0)
    for now in (1500.0, 1600.0):
        aw.scan(agents=[], read_fn=lambda t: "", emit_fn=emit, now=now)
    assert "agent_process_failed" not in emit.types


def test_a_failed_kill_leaves_the_pane_watched(box, monkeypatch):
    from core import agent_watch as aw
    (box / "calc").mkdir()
    root = str(box / "calc")

    def fake_tmux(args, stdin=None):
        if args[0] == "list-panes":
            fmt = args[args.index("-F") + 1]
            return 0, (root if "path" in fmt else "fc-calc:0.0") + "\n", ""
        return 1, "", "no server"
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    assert intake.stop_agent({"id": "calc", "root": root}) is False
    assert aw.is_suppressed("fc-calc:0.0") is False


def test_the_stopped_pane_stays_quiet_after_the_suppression_ends(box, monkeypatch):
    import time as _t
    from core import agent_watch as aw
    _watch_then_stop(box, monkeypatch, stop=True)
    emit = _Emit()
    later = _t.time() + intake.UNWATCH_SECS + 3600
    for now in (later, later + 100, later + 200):
        aw.scan(agents=[], read_fn=lambda t: "", emit_fn=emit, now=now)
    assert "agent_process_failed" not in emit.types


# -- an agent only while the project needs it (slots) ---------------------------------------
class _E:
    """Just what needs_agent reads of an engine."""

    def __init__(self, tasks, parked=()):
        self.tasks, self.parked = tasks, set(parked)

    def _tasks(self):
        return self.tasks

    def _in_flight(self, t):
        return t["state"] not in ("VERIFIED_DONE", "CANCELLED", "CREATED") and \
            t["key"] not in self.parked


def _t(key, state, reason="", redispatch=0):
    return {"key": key, "state": state, "last_reason": reason, "redispatch": redispatch}


@pytest.mark.parametrize("tasks,parked,needed", [
    ([_t("a", "WORKING")], (), True),
    ([_t("a", "AGENT_DONE")], (), True),
    ([_t("a", "VERIFYING")], (), True),
    ([_t("a", "VERIFICATION_FAILED")], (), True),
    ([_t("a", "VERIFIED_DONE"), _t("b", "CREATED")], (), True),
    ([_t("a", "BLOCKED", "agent_permission_prompt")], (), True),
    ([_t("a", "OWNER_DECISION", "stall_hold_timeout:resend")], (), True),
    ([_t("a", "BLOCKED", "blocked_answered", redispatch=2)], (), True),
    # nothing to do: all finished, or waiting only on a decision
    ([_t("a", "VERIFIED_DONE"), _t("b", "CANCELLED")], (), False),
    ([_t("a", "CANCELLED"), _t("b", "OWNER_DECISION", "dirty_worktree:x"),
      _t("c", "CREATED")], (), False),
    ([_t("a", "BLOCKED", "max_attempts_3:blocked_answered")], (), False),
    ([_t("a", "BLOCKED", "dirty_worktree:x")], (), False),
    # a parked task does not hold the executor: the next one is sent
    ([_t("a", "OWNER_DECISION", "permission_denied:check_x"), _t("b", "CREATED")],
     ("a",), True),
    ([], (), False),
])
def test_needs_agent(tasks, parked, needed):
    assert intake.needs_agent(_E(tasks, parked)) is needed


def test_needs_agent_on_a_real_engine(box, monkeypatch):
    intake.start("calc", SPEC, by=BY)
    (p,) = intake.projects()
    e = fe.Engine("calc", intake.adapter(p), holder="t")
    e._seed()
    assert intake.needs_agent(e) is True                       # a task to send
    (t,) = e._tasks()
    e._owner(t, "dirty_worktree:x")
    assert intake.needs_agent(e) is False


def test_worker_parks_an_alive_agent_it_does_not_need(capsys):
    fi = FakeIntake([{"id": "calc"}, {"id": "tb"}], alive=("fc-calc",))
    worker.park_intake("calc", fi)
    worker.park_intake("tb", fi)                               # none running: nothing
    assert fi.stopped == ["calc"]
    assert "intake_agent_parked" in capsys.readouterr().out


def test_a_send_to_a_parked_project_starts_its_agent_first(monkeypatch):
    a = Agents(monkeypatch, alive=False)
    ad = intake.adapter(P)
    monkeypatch.setattr(type(ad).__mro__[1], "submit",
                        lambda self, env, key: {"ok": True, "handle": {"key": key}})
    env = type("Env", (), {"task_id": "calc:add", "attempt": 2})()
    assert ad.submit(env, "k2")["ok"] is True
    assert a.started == 1 and "k2" in ad.sent and ad.fresh


def test_a_send_whose_agent_cannot_start_says_agent_not_running(monkeypatch):
    Agents(monkeypatch, alive=False, create=False)
    ad = intake.adapter(P)
    called = []
    monkeypatch.setattr(type(ad).__mro__[1], "submit",
                        lambda self, env, key: called.append(key) or {"ok": True})
    env = type("Env", (), {"task_id": "calc:add", "attempt": 1})()
    r = ad.submit(env, "k1")
    assert r["ok"] is False and r["reason"] == "agent_not_running" and called == []
    assert r["handle"]["key"] == "k1"
