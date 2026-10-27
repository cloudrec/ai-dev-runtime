"""Fail-closed agent creation/resume gate: scopes (per worktree key, e.g. country), callers,
one session per worktree, max concurrency, hard deny, owner proof — and the two live
creation paths (agent_resume / API / fabric, session_recovery.recover) never mutate tmux
when refused."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import agent_control as ac  # noqa: E402
from core import agent_creation_policy as acp  # noqa: E402

KEY = b"q" * 64


@pytest.fixture
def wt(tmp_path, monkeypatch):
    """GAIKA-like scope with two country worktrees and a config file on disk."""
    d = {c: tmp_path / "gaika" / c for c in ("ru", "kz", "uz")}
    for p in d.values():
        p.mkdir(parents=True)
    other = tmp_path / "other-project"
    other.mkdir()
    pay = tmp_path / "payorch"
    pay.mkdir()
    cfg = {"hard_deny_dirs": [str(pay)],
           "scopes": {"gaika": {"callers": ["api"], "session_prefix": "gaika-",
                                "max_concurrent": 2,
                                "worktrees": {"ru": str(d["ru"]), "kz": str(d["kz"]),
                                              "uz": str(d["uz"])}}}}
    path = tmp_path / "agent_creation.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(path))
    monkeypatch.delenv("AGENT_CREATION_POLICY", raising=False)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    kf = tmp_path / "owner.key"
    kf.write_bytes(KEY)
    kf.chmod(0o600)
    monkeypatch.setenv("AGENT_OWNER_PROOF_KEY_FILE", str(kf))
    return {**{k: str(v) for k, v in d.items()}, "other": str(other), "pay": str(pay),
            "cfg": path, "tmp": tmp_path}


def auth(wt, d, session, caller="api", live=(), **kw):
    return acp.authorize(d, session, caller=caller, live_cwds=list(live), **kw)


# ── policy decisions ──────────────────────────────────────────────────────────

def test_shipped_config_authorizes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CREATION_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_CREATION_POLICY", raising=False)
    shipped = acp.load_config()
    assert shipped.get("scopes") == {}
    for caller in ("api", "unattributed", "session_recovery", "native_supervisor"):
        assert acp.authorize(str(tmp_path), "agent-x", caller=caller,
                             live_cwds=[])["reason"] == "creation_not_in_any_scope"


@pytest.mark.parametrize("content", ["", "::: not yaml", "[1, 2]", "scopes: 7"])
def test_missing_or_malformed_config_fails_closed(wt, content):
    wt["cfg"].write_text(content)
    assert not auth(wt, wt["ru"], "gaika-ru")["ok"]


def test_scoped_creation_is_allowed(wt):
    assert auth(wt, wt["ru"], "gaika-ru") == {"ok": True, "reason": "scope", "scope": "gaika"}


@pytest.mark.parametrize("caller", ["unattributed", "session_recovery", "native_supervisor",
                                    "agent_fabric", "fabric"])
def test_unlisted_callers_are_refused(wt, caller):
    assert auth(wt, wt["ru"], "gaika-ru", caller=caller)["reason"] == \
        "creation_caller_not_allowed"


@pytest.mark.parametrize("actor", ["api:hmac/owner", "api:bearer/OWNER-APPROVED", "api:x"])
def test_spoofed_api_actor_names_are_just_api(wt, actor):
    """X-Runtime-Actor is self-declared: any api:… actor is the `api` caller class."""
    assert ac.caller_class(actor) == "api"
    wt["cfg"].write_text(yaml.safe_dump({"scopes": {"g": {
        "callers": ["unattributed"], "session_prefix": "gaika-", "max_concurrent": 5,
        "worktrees": {"ru": wt["ru"]}}}}))
    assert auth(wt, wt["ru"], "gaika-ru", caller=ac.caller_class(actor))["reason"] == \
        "creation_caller_not_allowed"


@pytest.mark.parametrize("dir_key,session,reason", [
    ("kz", "gaika-ru", "creation_session_name_mismatch"),        # cross-country
    ("ru", "gaika-kz", "creation_session_name_mismatch"),
    ("ru", "agent-ru", "creation_session_name_mismatch"),
    ("other", "gaika-ru", "creation_not_in_any_scope"),          # cross-project
])
def test_cross_country_and_cross_project_are_refused(wt, dir_key, session, reason):
    assert auth(wt, wt[dir_key], session)["reason"] == reason


def test_subdirectories_and_traversal_resolve_to_the_real_worktree(wt):
    sub = os.path.join(wt["ru"], "src")
    os.makedirs(sub)
    assert auth(wt, sub, "gaika-ru")["reason"] == "creation_not_in_any_scope"
    trav = os.path.join(wt["ru"], "..", "kz")
    assert auth(wt, trav, "gaika-ru")["reason"] == "creation_session_name_mismatch"
    assert auth(wt, trav, "gaika-kz")["ok"]


def test_symlink_into_a_worktree_is_judged_by_its_real_path(wt):
    link = os.path.join(wt["tmp"], "ru-link")
    os.symlink(wt["kz"], link)
    assert auth(wt, link, "gaika-ru")["reason"] == "creation_session_name_mismatch"


def test_one_live_session_per_worktree(wt):
    assert auth(wt, wt["ru"], "gaika-ru", live=[wt["ru"]])["reason"] == \
        "worktree_already_has_live_agent"


def test_max_concurrency_counts_agents_inside_the_scope_only(wt):
    assert auth(wt, wt["uz"], "gaika-uz", live=[wt["ru"], wt["kz"]])["reason"] == \
        "creation_scope_at_max_concurrent"
    assert auth(wt, wt["uz"], "gaika-uz", live=[wt["ru"], wt["other"], "/root"])["ok"]


def test_scope_without_max_concurrent_refuses(wt):
    cfg = yaml.safe_load(wt["cfg"].read_text())
    del cfg["scopes"]["gaika"]["max_concurrent"]
    wt["cfg"].write_text(yaml.safe_dump(cfg))
    assert auth(wt, wt["ru"], "gaika-ru")["reason"] == "creation_scope_missing_max_concurrent"


def test_payment_agent_is_hard_denied_even_inside_a_scope_or_with_policy_off(wt, monkeypatch):
    cfg = yaml.safe_load(wt["cfg"].read_text())
    cfg["scopes"]["gaika"]["worktrees"]["pay"] = wt["pay"]
    wt["cfg"].write_text(yaml.safe_dump(cfg))
    assert auth(wt, wt["pay"], "gaika-pay")["reason"] == "hard_denied_target"
    assert auth(wt, wt["ru"], "payorch-ha-fresh")["reason"] == "hard_denied_target"
    assert auth(wt, wt["ru"], " PAYORCH-HA-FRESH")["reason"] == "hard_denied_target"
    monkeypatch.setenv("AGENT_CREATION_POLICY", "off")
    assert auth(wt, wt["pay"], "x")["reason"] == "hard_denied_target"
    assert auth(wt, wt["other"], "anything")["ok"]                    # escape hatch


def test_owner_proof_authorizes_exactly_one_creation(wt):
    target = os.path.realpath(wt["other"])
    p = ac.sign_owner_proof(KEY, "agent_resume", target, "manual-1")
    assert auth(wt, wt["other"], "manual-1", caller="api:x", owner_proof=p)["ok"]
    assert auth(wt, wt["other"], "manual-1", owner_proof=p)["reason"] == "owner_proof_replayed"
    for bad in (ac.sign_owner_proof(KEY, "agent_resume", target, "manual-2"),   # other session
                ac.sign_owner_proof(KEY, "agent_resume", wt["ru"], "manual-1"), # other dir
                ac.sign_owner_proof(KEY, "agent_send", target, "manual-1"),     # other action
                ac.sign_owner_proof(b"z" * 64, "agent_resume", target, "manual-1")):
        assert not auth(wt, wt["other"], "manual-1", owner_proof=bad)["ok"]


# ── live path 1: agent_resume (API /agents/resume, fabric) ─────────────────────

@pytest.fixture
def tmux(monkeypatch, wt):
    calls, live = [], []
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: calls.append(list(args))
                        or (1 if args[0] == "has-session" else 0, "", ""))
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [
        {"is_agent": True, "alive": True, "claude_cwd": c, "target": f"x{i}:0.0",
         "session": f"x{i}"} for i, c in enumerate(live)]})
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda d: os.path.realpath(d))
    return {"calls": calls, "live": live}


def mutations(calls):
    return [c for c in calls if c and c[0] in ("new-session", "kill-session",
                                               "respawn-pane", "send-keys")]


def test_refused_resume_never_mutates_tmux(wt, tmux):
    r = ac.agent_resume(wt["ru"], session_name="gaika-ru", actor=None)
    assert r["refused"] == "creation_caller_not_allowed" and not r["agent_created"]
    r = ac.agent_resume(wt["other"], session_name="gaika-ru", actor="api:hmac/chatgpt")
    assert r["refused"] == "creation_not_in_any_scope"
    assert mutations(tmux["calls"]) == []


def test_authorized_resume_creates_exactly_the_scoped_session(wt, tmux):
    r = ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt")
    assert r["agent_created"] and r["session"] == "gaika-kz"
    assert mutations(tmux["calls"]) == [["new-session", "-d", "-s", "gaika-kz", "-c",
                                         os.path.realpath(wt["kz"]), "claude"]]


def test_resume_respects_concurrency_with_real_inventory(wt, tmux):
    tmux["live"].extend([wt["ru"], wt["kz"]])
    r = ac.agent_resume(wt["uz"], session_name="gaika-uz", actor="api:x")
    assert r["refused"] == "creation_scope_at_max_concurrent"
    assert mutations(tmux["calls"]) == []


def test_fabric_start_carries_the_caller_and_is_gated(wt, tmux):
    from core import agent_fabric
    r = agent_fabric.start_or_resume(wt["ru"])                 # in-process, no api actor
    assert r.get("refused") == "creation_caller_not_allowed"
    assert mutations(tmux["calls"]) == []


def test_api_routes_pass_actor_and_owner_proof(wt, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("api_v1_creation_test", ROOT / "api" / "v1.py")
    v1 = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "api_v1_creation_test", v1)
    spec.loader.exec_module(v1)
    seen = []
    monkeypatch.setattr(v1, "agent_control", type("A", (), {
        "agent_resume": staticmethod(lambda *a, **k: seen.append(("resume", a, k)) or {}),
        "AgentControlError": ac.AgentControlError}))
    monkeypatch.setattr(v1, "_validate_project_path", lambda p: p)
    import core.agent_fabric as fab
    monkeypatch.setattr(fab, "start_or_resume",
                        lambda *a, **k: seen.append(("fabric", a, k)) or {})
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._auth] = lambda: True
    c = TestClient(app)
    proof = {"ts": 1, "nonce": "ab" * 8, "sig": "0" * 64}
    h = {"X-Runtime-Actor": "owner", "X-Owner-Proof": json.dumps(proof)}
    c.post("/api/v1/agents/resume", json={"project_dir": wt["ru"], "session_name": "gaika-ru"},
           headers=h)
    c.post("/api/v1/fabric/start-or-resume", json={"project_dir": wt["ru"]}, headers=h)
    kinds = {k: kw for k, a, kw in seen}
    for k in ("resume", "fabric"):
        assert kinds[k]["actor"].startswith("api:") and kinds[k]["owner_proof"] == proof


# ── live path 2: session_recovery.recover (commander_autopilot) ────────────────

def test_session_recovery_is_gated_before_any_tmux_call(wt, monkeypatch):
    from core import session_recovery as sr
    monkeypatch.setattr(sr, "control_probe",
                        lambda: {"healthy": True, "reachable": True, "reason": "ok"})
    monkeypatch.setattr(sr, "pane_state", lambda t: {"missing": True, "dead": True})
    monkeypatch.setattr(sr, "_capture", lambda t, n=30: "")
    monkeypatch.setattr(sr, "panes", lambda: [])
    monkeypatch.setattr(sr, "has_authoritative_work",
                        lambda t: {"open": True, "task_id": "t", "reason": "active_task"})
    import core.continuation_governor as cg
    monkeypatch.setattr(cg, "load_config", lambda *a, **k: {})
    reg = {"sessions": {"gaika-ru:0.0": {"target": "gaika-ru:0.0", "session": "gaika-ru",
                                         "cwd": wt["ru"], "conversation_id": "c" * 8,
                                         "enabled": True}},
           "limits": {"max_recoveries_per_target": 3, "window_secs": 60,
                      "backoff_base_secs": 0}}
    calls = []
    run = lambda a: calls.append(list(a)) or (0, "", "")  # noqa: E731
    res = sr.recover("gaika-ru:0.0", registry=reg, run_fn=run, sleep=lambda s: None,
                     explicit=True, live_cwds=[])
    assert res["reason"] == "creation_policy:creation_caller_not_allowed"
    assert calls == []                               # `explicit` is not owner authority
    res = sr.recover("gaika-ru:0.0", registry=reg, run_fn=run, sleep=lambda s: None,
                     live_cwds=[wt["kz"], wt["uz"]], actor="api:x")
    assert res["reason"] == "creation_policy:creation_scope_at_max_concurrent" and calls == []
