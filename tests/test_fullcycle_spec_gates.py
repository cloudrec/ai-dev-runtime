"""Spec store and permission gates of core.fullcycle (no tmux needed)."""
from __future__ import annotations

import os

import pytest

from core.fullcycle import gates
from core.fullcycle import spec as specs

SPEC = {"title": "t", "goal": "g", "acceptance_criteria": ["works"],
        "tasks": [{"key": "a", "title": "A", "goal": "do a", "acceptance_criteria": ["a ok"]},
                  {"key": "b", "title": "B", "goal": "do b", "acceptance_criteria": ["b ok"],
                   "depends_on": ["a"]}]}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


def test_spec_versions_are_append_only_and_a_retried_put_does_not_fork(tmp_path):
    specs.create_project("p", name="p", root=str(tmp_path), executor="fixture")
    v1 = specs.put_spec("p", SPEC, by="t")
    assert specs.put_spec("p", SPEC, by="t")["version"] == v1["version"] == 1
    v2 = specs.put_spec("p", {**SPEC, "goal": "g2"}, by="t")
    assert v2["version"] == 2 and v2["sha"] != v1["sha"]
    assert specs.get_spec("p", 1)["body"]["goal"] == "g"


@pytest.mark.parametrize("bad, msg", [
    ({**SPEC, "pushallowed": True}, "unknown spec field"),
    ({**SPEC, "acceptance_criteria": []}, "must not be empty"),
    ({**SPEC, "tasks": [SPEC["tasks"][0], SPEC["tasks"][0]]}, "duplicate task key"),
    ({**SPEC, "tasks": [SPEC["tasks"][1]]}, "depends on unknown"),
    ({**SPEC, "checks": ["python -c 1"]}, "argv lists"),
])
def test_spec_validation_is_fail_closed(bad, msg):
    with pytest.raises(specs.SpecError, match=msg):
        specs.validate_spec(bad)


def test_a_project_id_cannot_be_rebound_to_another_root(tmp_path):
    specs.create_project("p", name="p", root=str(tmp_path), executor="fixture")
    with pytest.raises(specs.SpecError):
        specs.create_project("p", name="p", root="/elsewhere", executor="fixture")


def test_dispatch_is_refused_for_the_live_tree_and_system_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "_payment_denied", lambda root: None)
    for root in ("/root/ai-dev-runtime", "/root/ai-dev-runtime/core", "/etc"):
        if os.path.isdir(root):
            assert gates.check_dispatch(root=root, executor="fixture", contract={}) \
                .startswith("project_root_denied")
    assert gates.check_dispatch(root=str(tmp_path), executor="fixture", contract={}) is None


def test_a_payment_directory_is_refused_and_an_unreadable_policy_fails_closed(tmp_path, monkeypatch):
    from core import agent_creation_policy as acp
    monkeypatch.setattr(acp, "load_config", lambda: {"hard_deny_dirs": [str(tmp_path)]})
    assert gates.check_dispatch(root=str(tmp_path), executor="fixture",
                                contract={}) == "payment_hard_denied"

    def boom():
        raise OSError("unreadable")
    monkeypatch.setattr(acp, "load_config", boom)
    assert gates.check_dispatch(root=str(tmp_path), executor="fixture",
                                contract={}) == "payment_policy_unreadable"


@pytest.mark.parametrize("executor", ["qwen", "codex", "nonexistent"])
def test_untested_executors_are_refused_not_claimed(tmp_path, executor, monkeypatch):
    monkeypatch.setattr(gates, "_payment_denied", lambda root: None)
    r = gates.check_dispatch(root=str(tmp_path), executor=executor, contract={})
    assert r.startswith("executor_unavailable")


@pytest.mark.parametrize("power", ["push_allowed", "deploy_allowed"])
def test_push_and_deploy_stay_owner_only(tmp_path, power, monkeypatch):
    monkeypatch.setattr(gates, "_payment_denied", lambda root: None)
    assert gates.check_dispatch(root=str(tmp_path), executor="fixture",
                                contract={power: True}).endswith("requires_owner")


def test_changes_are_checked_against_scope_and_do_not_touch():
    v = gates.check_changes(["src/a.py", "README.md", "docs/x.md", "../etc/passwd",
                             ".fullcycle/results/a.json", "srcx/b.py"],
                            scope=["src/"], do_not_touch=["README.md"])
    assert v == ["do_not_touch:README.md", "outside_scope:docs/x.md",
                 "outside_workdir:../etc/passwd", "outside_scope:srcx/b.py"]
    assert gates.check_changes(["any/file.py"], scope=[], do_not_touch=[]) == []


def test_a_permission_prompt_always_goes_to_the_owner_even_when_safe(tmp_path):
    safe = gates.check_permission_prompt("ls -la", root=str(tmp_path))
    risky = gates.check_permission_prompt("rm -rf build", root=str(tmp_path))
    assert safe["decision"] == risky["decision"] == "owner"
    assert risky["classified_safe"] is False
