"""Technical stops the tech lead lifts instead of the owner (sandboxed intake projects):
leftovers in the copy before a first dispatch, and an agent's permission dialog."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

import pytest

from core import task_contract as tc
from core.fullcycle import engine as fe
from core.fullcycle import spec as specs
from core.fullcycle.executor import WAITING_PERMISSION, WORKING, Observation

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures", "fullcycle_demo"))
import harness as h  # noqa: E402

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def _task(e, key):
    return next(t for t in e._tasks() if t["key"] == key)


def _wip(root):
    with open(os.path.join(root, "README.md"), "a") as fh:
        fh.write("LEFTOVER\n")
    with open(os.path.join(root, "stray.txt"), "w") as fh:
        fh.write("stray\n")


def _git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout


@pytest.fixture
def fc(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    adapters = []

    def make(behaviours=None, *, before_spec=None, lead=True):
        root = h.make_project(str(tmp_path))
        if before_spec:
            before_spec(root)
        specs.create_project("tinycalc", name="tinycalc", root=root, executor="fixture")
        specs.put_spec("tinycalc", h.load_spec(), by="test")
        a, log = h.make_adapter(str(tmp_path), root, behaviours or {})
        a.lead_resolves_gates = lead
        adapters.append(a)
        return fe.Engine("tinycalc", a, holder="e1"), a, log, root

    yield make
    for a in adapters:
        a.kill()


def _review(kind):
    (rv,) = [r for r in fe.pending_reviews("tinycalc") if r["kind"] == kind]
    return rv


# ── leftovers ─────────────────────────────────────────────────────────────────
@needs_tmux
def test_without_the_flag_leftovers_still_go_to_the_owner(fc):
    e, a, log, root = fc(before_spec=_wip, lead=False)
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]
    assert not fe.pending_reviews("tinycalc")


@needs_tmux
def test_leftovers_go_to_the_tech_lead_not_the_owner(fc):
    e, a, log, root = fc(before_spec=_wip)
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and not t["gate_id"]
    assert t["last_reason"] == "dirty_worktree:README.md,stray.txt"
    rv = _review("dirty")
    assert rv["decisions"] == ["approve", "reject", "cancel"]
    e.tick()                                    # waits; no second review, no dispatch
    assert len(fe.pending_reviews("tinycalc")) == 1 and h.agent_log(log) == []


@needs_tmux
def test_keep_commits_the_leftovers_then_sends_the_task(fc):
    e, a, log, root = fc(before_spec=_wip)
    e.tick()
    fe.submit_decision(_review("dirty")["review_id"], "approve", key="k1", by="gpt")
    e.tick()
    assert _git(root, "log", "-1", "--format=%s").strip() == \
        "fullcycle: leftovers kept before add (tech lead)"
    assert "stray.txt" in _git(root, "show", "--name-only", "--format=")
    assert h.wait_for(lambda: len(h.agent_log(log)) == 1, 10)
    assert _task(e, "add")["state"] not in (tc.BLOCKED, tc.OWNER_DECISION)


@needs_tmux
def test_discard_saves_a_patch_resets_the_tree_then_sends_the_task(fc):
    e, a, log, root = fc(before_spec=_wip)
    head = _git(root, "rev-parse", "HEAD")
    e.tick()
    with pytest.raises(fe.EngineError):           # a discard says why
        fe.submit_decision(_review("dirty")["review_id"], "reject", key="k0", by="gpt")
    fe.submit_decision(_review("dirty")["review_id"], "reject", key="k1", by="gpt",
                       feedback="cancelled work")
    e.tick()
    assert _git(root, "rev-parse", "HEAD") == head
    assert not os.path.exists(os.path.join(root, "stray.txt"))
    assert "LEFTOVER" not in open(os.path.join(root, "README.md")).read()
    (patch,) = glob.glob(os.path.join(root, ".fullcycle", "leftovers", "add-*.patch"))
    body = open(patch).read()
    assert "LEFTOVER" in body and "stray.txt" in body
    assert h.wait_for(lambda: len(h.agent_log(log)) == 1, 10)


@needs_tmux
def test_a_failed_save_discards_nothing(fc, monkeypatch):
    e, a, log, root = fc(before_spec=_wip)
    e.tick()
    fe.submit_decision(_review("dirty")["review_id"], "reject", key="k1", by="gpt",
                       feedback="x")

    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(e.host, "write_file", boom)
    e.tick()
    assert os.path.exists(os.path.join(root, "stray.txt"))
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["last_reason"].startswith(
        "leftovers_reject_failed:save_failed")
    assert _git(root, "diff", "--cached", "--name-only") == ""     # staging undone


@needs_tmux
def test_cancel_cancels_the_task(fc):
    e, a, log, root = fc(before_spec=_wip)
    e.tick()
    fe.submit_decision(_review("dirty")["review_id"], "cancel", key="k1", by="gpt")
    e.tick()
    assert _task(e, "add")["state"] == tc.CANCELLED
    assert os.path.exists(os.path.join(root, "stray.txt"))


@needs_tmux
def test_still_dirty_after_the_lead_goes_to_the_owner(fc, monkeypatch):
    e, a, log, root = fc(before_spec=_wip)
    e.tick()
    fe.submit_decision(_review("dirty")["review_id"], "approve", key="k1", by="gpt")
    monkeypatch.setattr(e, "_keep_leftovers", lambda task: None)   # "kept" but not
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]
    assert t["last_reason"].startswith("dirty_worktree:")


# ── permission dialog ─────────────────────────────────────────────────────────
def _at_dialog(e, a, log, monkeypatch, command):
    h.wait_for(lambda: e.tick() and len(h.agent_log(log)) == 1, 15)
    seen = {"obs": Observation(WAITING_PERMISSION, {"command": command})}
    monkeypatch.setattr(a, "poll", lambda handle: seen["obs"])
    pressed = []
    monkeypatch.setattr(a, "answer_permission",
                        lambda handle, allow: pressed.append(allow) or {"ok": True})
    e.tick()
    return seen, pressed


@needs_tmux
def test_a_plain_command_dialog_goes_to_the_tech_lead(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "npm test")
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and not t["gate_id"]
    assert t["last_reason"] == "agent_permission_prompt"
    assert fe.attempt_in_flight_for_agent(a.target) is True     # the pane is not rotated
    rv = _review("permission")
    assert "npm test" in rv["payload"]["command"] and "UNTRUSTED" in rv["payload"]["command"]
    fe.submit_decision(rv["review_id"], "approve", key="k1", by="gpt")
    e.tick()                                  # the dialog is still drawn right after
    assert pressed == [True]
    t = _task(e, "add")
    assert t["state"] == tc.WORKING and t["attempt"] == 1 and not t["gate_id"]
    seen["obs"] = Observation(WORKING, {})
    e.tick()
    assert _task(e, "add")["state"] == tc.WORKING


@needs_tmux
def test_approve_after_the_dialog_went_away_presses_nothing(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "npm test")
    fe.submit_decision(_review("permission")["review_id"], "approve", key="k1", by="gpt")
    seen["obs"] = Observation(WORKING, {})
    e.tick()
    assert pressed == [] and _task(e, "add")["state"] == tc.WORKING


@needs_tmux
def test_deny_interrupts_and_resends_with_the_feedback(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "rm -rf build")
    fe.submit_decision(_review("permission")["review_id"], "reject", key="k1", by="gpt",
                       feedback="use the test runner instead")
    e.tick()
    assert pressed == [False]
    t = _task(e, "add")
    assert t["redispatch"] == 2 or t["attempt"] == 2
    assert "use the test runner instead" in t["feedback"]


@needs_tmux
@pytest.mark.parametrize("command", [
    "git push origin main", "git -C x push", "ssh host", "/usr/bin/ssh host",
    "sudo ls", "curl https://x", "bash -c 'wget x'", "npm publish", "docker ps",
    "env FOO=1 gh pr create", "$(echo ssh) host", ""])
def test_push_network_privilege_dialogs_stay_with_the_owner(fc, monkeypatch, command):
    e, a, log, root = fc({"add": ["hang"]})
    _at_dialog(e, a, log, monkeypatch, command)
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]
    assert not [r for r in fe.pending_reviews("tinycalc") if r["kind"] == "permission"]


@needs_tmux
def test_a_second_dialog_in_the_same_attempt_goes_to_the_owner(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "npm test")
    fe.submit_decision(_review("permission")["review_id"], "approve", key="k1", by="gpt")
    e.tick()
    seen["obs"] = Observation(WAITING_PERMISSION, {"command": "npm run lint"})
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]


@needs_tmux
def test_the_same_dialog_after_the_grace_is_a_new_question(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "npm test")
    fe.submit_decision(_review("permission")["review_id"], "approve", key="k1", by="gpt")
    e.tick()
    now = e.clock()
    e.clock = lambda: now + fe.LEAD_ALLOW_GRACE + 1
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.OWNER_DECISION and t["gate_id"]


def test_owner_only_command():
    assert fe.owner_only_command("git push") and fe.owner_only_command("")
    assert fe.owner_only_command("cargo publish --dry-run")
    assert not fe.owner_only_command("npm test")
    assert not fe.owner_only_command("python3 -m pytest tests -q")
    assert not fe.owner_only_command("git status && git diff")


@needs_tmux
def test_an_old_owner_gate_on_leftovers_moves_to_the_tech_lead(fc):
    e, a, log, root = fc(before_spec=_wip, lead=False)
    e.tick()
    gate = _task(e, "add")["gate_id"]
    assert gate
    a.lead_resolves_gates = True
    e.tick()
    t = _task(e, "add")
    assert t["state"] == tc.BLOCKED and not t["gate_id"]
    assert _review("dirty")
    assert e.conn.execute("SELECT state FROM owner_gate WHERE id=?", (gate,)).fetchone()[0] \
        == "resolved"


@needs_tmux
def test_an_old_owner_gate_on_a_tree_clean_by_now_just_sends_the_task(fc):
    e, a, log, root = fc(before_spec=_wip, lead=False)
    e.tick()
    subprocess.run(["git", "-C", root, "checkout", "--", "README.md"], check=True)
    os.remove(os.path.join(root, "stray.txt"))
    a.lead_resolves_gates = True
    e.tick()
    assert h.wait_for(lambda: len(h.agent_log(log)) == 1, 10)
    assert not [r for r in fe.pending_reviews("tinycalc") if r["kind"] == "dirty"]


@needs_tmux
def test_deny_after_the_dialog_went_away_presses_nothing(fc, monkeypatch):
    e, a, log, root = fc({"add": ["hang"]})
    seen, pressed = _at_dialog(e, a, log, monkeypatch, "rm -rf build")
    fe.submit_decision(_review("permission")["review_id"], "reject", key="k1", by="gpt",
                       feedback="no")
    seen["obs"] = Observation(WORKING, {})
    e.tick()
    assert pressed == []
