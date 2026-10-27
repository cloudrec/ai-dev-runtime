"""Adversarial review of agent creation: races, symlink retargeting, controller-only scopes
(3 GAIKA country fixture), API spoofing, deny boundaries, send-policy compatibility, and the
invariant that only the two gated sites create panes."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import re
import sys
import threading
import time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import agent_control as ac  # noqa: E402
from core import agent_creation_policy as acp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "agent_creation_gaika_3_countries.yaml"


@pytest.fixture
def gaika(tmp_path, monkeypatch):
    root = tmp_path / "opt"
    for c in ("ru", "kz", "uz"):
        (root / "gaika-worktrees" / c).mkdir(parents=True)
    cfg = tmp_path / "agent_creation.yaml"
    cfg.write_text(FIXTURE.read_text().replace("{root}", str(root)))
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(cfg))
    monkeypatch.setenv("AGENT_CREATION_STATE", str(tmp_path / "creation"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("AGENT_CREATION_POLICY", raising=False)
    wt = {c: str(root / "gaika-worktrees" / c) for c in ("ru", "kz", "uz")}
    return {"root": root, "cfg": cfg, "wt": wt, "tmp": tmp_path}


@pytest.fixture
def fake_tmux(monkeypatch):
    """New sessions exist for has-session, but their Claude process never shows up in the
    inventory during the test — the window a race would exploit."""
    lock = threading.Lock()
    state = {"sessions": set(), "created": [], "live_cwds": []}

    def tmux(args, stdin=None):
        with lock:
            if args[0] == "has-session":
                return (0 if args[2] in state["sessions"] else 1), "", ""
            if args[0] == "new-session":
                time.sleep(0.02)                     # widen the window
                state["sessions"].add(args[3])
                state["created"].append((args[3], args[5]))
            return 0, "", ""
    monkeypatch.setattr(ac, "_tmux", tmux)
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [
        {"is_agent": True, "alive": True, "claude_cwd": c} for c in state["live_cwds"]]})
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda d: os.path.realpath(d))
    monkeypatch.setattr(ac, "_session_liveness", lambda n: {"panes": 1, "live": 1, "dead": 0})
    return state


def ctl(d, s):
    return ac.agent_resume(d, session_name=s, actor="owneros_controller")


# ── races ─────────────────────────────────────────────────────────────────────

def test_simultaneous_launches_into_one_worktree_create_exactly_one(gaika, fake_tmux):
    out = []
    ts = [threading.Thread(target=lambda: out.append(ctl(gaika["wt"]["ru"], "gaika-ru")))
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(fake_tmux["created"]) == 1
    assert sum(1 for r in out if r.get("agent_created")) == 1


def test_simultaneous_launches_across_countries_respect_the_cap(gaika, fake_tmux):
    out = []
    ts = [threading.Thread(target=lambda c=c: out.append(ctl(gaika["wt"][c], f"gaika-{c}")))
          for c in ("ru", "kz", "uz") for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(fake_tmux["created"]) == 2                       # max_concurrent: 2
    assert len({s for s, _ in fake_tmux["created"]}) == 2


def test_reservation_bridges_the_invisible_window_then_expires(gaika, fake_tmux, monkeypatch):
    assert ctl(gaika["wt"]["ru"], "gaika-ru")["agent_created"]
    fake_tmux["sessions"].clear()                 # even if the session name vanished
    assert ctl(gaika["wt"]["ru"], "gaika-ru")["refused"] == \
        "worktree_reserved_by_recent_creation"
    monkeypatch.setattr(acp, "RESERVATION_TTL_SECS", 0)
    assert ctl(gaika["wt"]["ru"], "gaika-ru")["agent_created"]


def test_reservation_blocks_owner_proof_and_policy_off_paths_too(gaika, fake_tmux,
                                                                 monkeypatch):
    assert ctl(gaika["wt"]["kz"], "gaika-kz")["agent_created"]
    monkeypatch.setenv("AGENT_CREATION_POLICY", "off")
    r = ac.agent_resume(gaika["wt"]["kz"], session_name="other-name", actor=None)
    assert r["refused"] == "worktree_reserved_by_recent_creation"


# ── 3-country fixture: controller only ─────────────────────────────────────────

def test_three_country_fixture_controller_flow(gaika, fake_tmux):
    assert ctl(gaika["wt"]["ru"], "gaika-ru")["agent_created"]
    assert ctl(gaika["wt"]["kz"], "gaika-kz")["agent_created"]
    assert ctl(gaika["wt"]["uz"], "gaika-uz")["refused"] == "creation_scope_at_max_concurrent"
    assert ctl(gaika["wt"]["uz"], "gaika-ru")["refused"] == "creation_session_name_mismatch"
    assert ctl(gaika["wt"]["kz"], "gaika-kz")["refused"] in (
        "worktree_reserved_by_recent_creation", "worktree_already_has_live_agent")
    assert [s for s, _ in fake_tmux["created"]] == ["gaika-ru", "gaika-kz"]


@pytest.mark.parametrize("actor", ["api:hmac/owneros_controller", "api:bearer", None,
                                   "native_supervisor", "session_recovery"])
def test_nobody_but_the_local_controller_creates(gaika, fake_tmux, actor):
    r = ac.agent_resume(gaika["wt"]["ru"], session_name="gaika-ru", actor=actor)
    assert r["refused"] == "creation_caller_not_allowed" and fake_tmux["created"] == []


def test_controller_cli_uses_the_local_caller_class(gaika, fake_tmux, capsys):
    spec = importlib.util.spec_from_file_location("agent_create", ROOT / "tools" /
                                                  "agent_create.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["--dir", gaika["wt"]["uz"], "--session", "gaika-uz"]) == 0
    assert json.loads(capsys.readouterr().out)["session"] == "gaika-uz"
    assert m.main(["--dir", gaika["wt"]["uz"], "--session", "gaika-uz"]) == 1


def test_host_wide_cap_counts_every_live_claude(gaika, fake_tmux):
    fake_tmux["live_cwds"].extend(f"/opt/other-{i}" for i in range(12))
    assert ctl(gaika["wt"]["ru"], "gaika-ru")["refused"] == "creation_host_at_max_live_agents"


# ── symlink / rename attacks ───────────────────────────────────────────────────

def test_retargeting_a_country_path_to_another_country_is_refused(gaika, fake_tmux):
    kz = pathlib.Path(gaika["wt"]["kz"])
    kz.rmdir()
    kz.symlink_to(gaika["wt"]["ru"])                    # kz now points at ru
    for d, s in ((gaika["wt"]["ru"], "gaika-ru"), (gaika["wt"]["kz"], "gaika-kz"),
                 (gaika["wt"]["kz"], "gaika-ru")):
        assert ctl(d, s)["refused"] == "creation_scope_ambiguous"
    assert fake_tmux["created"] == []


def test_a_configured_worktree_that_is_a_symlink_is_refused(gaika, fake_tmux):
    real = gaika["tmp"] / "elsewhere"
    real.mkdir()
    uz = pathlib.Path(gaika["wt"]["uz"])
    uz.rmdir()
    uz.symlink_to(real)
    assert ctl(str(real), "gaika-uz")["refused"] == "creation_scope_worktree_is_symlink"


def test_renaming_a_worktree_away_takes_it_out_of_scope(gaika, fake_tmux):
    ru = pathlib.Path(gaika["wt"]["ru"])
    moved = ru.with_name("ru-old")
    ru.rename(moved)
    assert ctl(str(moved), "gaika-ru")["refused"] == "creation_not_in_any_scope"


# ── deny boundaries ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("d,denied", [("/opt/payment-orchestrator", True),
                                      ("/opt/payment-orchestrator/sub", True),
                                      ("/opt/payment-orchestrator-evil", False),
                                      ("/opt/payment", False)])
def test_hard_deny_dir_boundaries(d, denied):
    assert acp.hard_denied(d, "x", {"hard_deny_dirs": ["/opt/payment-orchestrator"]}) is denied


def test_payment_agent_name_is_denied_for_the_local_controller_too(gaika, fake_tmux):
    assert ctl(gaika["wt"]["ru"], "payorch-ha-fresh")["refused"] == "hard_denied_target"


# ── compatibility with the live send policy ────────────────────────────────────

def test_live_send_allowlist_still_blocks_messages_to_created_country_agents(monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "enforce")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "owner-os-supervisor-clean@api")
    for caller in ("api:bearer/chatgpt", "owneros_controller", "native_supervisor", None):
        assert ac.automation_refusal("gaika-ru", "gaika-ru:0.0", caller) == \
            "automation_target_not_allowlisted"
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS",
                       "owner-os-supervisor-clean@api,gaika-ru@owneros_controller")
    assert ac.automation_refusal("gaika-ru", "gaika-ru:0.0", "owneros_controller") == ""
    assert ac.automation_refusal("gaika-ru", "gaika-ru:0.0", "api:x") == \
        "automation_target_not_allowlisted"


# ── invariant: only the two gated sites create panes ──────────────────────────

def test_only_gated_sites_create_panes_or_spawn_claude():
    create = re.compile(r"""["'](new-session|respawn-pane|new-window|split-window)["']""")
    spawn = re.compile(r"(Popen|create_subprocess_exec)\(")
    allowed_create = {"core/agent_control.py", "core/session_recovery.py",
                      "core/tmux_launch.py"}          # tmux_launch only wraps argv
    bad = []
    for d in ("core", "api", "tools", "hooks"):
        for f in (ROOT / d).rglob("*.py"):
            rel = str(f.relative_to(ROOT))
            src = f.read_text(errors="replace")
            if create.search(src) and rel not in allowed_create:
                bad.append(rel)
            if spawn.search(src) and re.search(r"\bclaude\b", src) and rel not in (
                    "core/ai_planner.py", "core/native_sessions.py", "tools/owner_proof.py"):
                if re.search(r"(Popen|create_subprocess_exec)\([^)]*claude", src, re.S):
                    bad.append(rel)
    assert bad == []
    src = (ROOT / "core" / "agent_control.py").read_text()
    i = src.index('argv = ["new-session"')
    assert src.rfind("with acp.creation_guard():", 0, i) > src.rfind("def agent_resume", 0, i)
    sr = (ROOT / "core" / "session_recovery.py").read_text()
    j = sr.index('run(["new-session"')
    assert sr.rfind("with acp.creation_guard():", 0, j) > sr.rfind("def _recover_inner", 0, j)
