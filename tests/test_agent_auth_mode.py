"""Per-agent auth mode (core/agent_auth_mode.py): subscription by default, API credits only
by explicit per-agent opt-in, fail-closed guardrails, no automatic switching.

All mocked: tmux is the fake from test_agent_creation_policy, the launcher is a dummy
root-owned file in tmp_path that is never executed, and no secret is ever read. No API
call is made and no money is spent.
"""
from __future__ import annotations

import builtins
import json
import os

import pytest
import yaml

from core import agent_auth_mode as aam
from core import agent_control as ac
from tests.test_agent_creation_policy import KEY, mutations, tmux, wt  # noqa: F401

SECRETS_DIR = "/root/.config/owner-os/secrets"


@pytest.fixture
def launcher(tmp_path):
    p = tmp_path / "claude-owner-api"
    p.write_text("#!/bin/sh\nexit 99\n")          # never run by these tests
    os.chmod(p, 0o700)
    return str(p)


@pytest.fixture
def api_cfg(tmp_path, monkeypatch, wt, launcher):
    def write(**over):
        cfg = {"enabled": True, "launcher": launcher,
               "limits": {"max_live_api_agents": 1, "max_api_launches_per_day": 2},
               "agents": {"gaika-kz": {"project_dir": wt["kz"],
                                       "allow_opus_for_high_risk": False}}}
        for k, v in over.items():
            cfg[k] = v
        path = tmp_path / "agent_auth_modes.yaml"
        path.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv("AGENT_AUTH_MODES_CONFIG", str(path))
        return cfg
    write()
    return write


@pytest.fixture
def no_secret_reads(monkeypatch):
    """Any attempt to open a file under the secrets dir fails the test."""
    real_open = builtins.open

    def guarded(file, *a, **k):
        if str(file).startswith(SECRETS_DIR):
            raise AssertionError(f"secret file opened: {file}")
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded)


def _create(wt, **kw):
    return ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt", **kw)


def _new_session(tmux):
    (call,) = [c for c in mutations(tmux["calls"]) if c[0] == "new-session"]
    return call[call.index("-c") + 2:]


# ── default: subscription, with no inheritable API credential ────────────────────
def test_shipped_config_enables_nothing():
    cfg = aam.load_config(aam._CONFIG)
    assert cfg["enabled"] is False and cfg["agents"] == {}


def test_default_launch_is_subscription_with_api_vars_stripped(wt, tmux, no_secret_reads):
    r = _create(wt)
    assert r["agent_created"] and r["auth_mode"] == "subscription"
    argv = _new_session(tmux)
    assert argv[0] == "env" and argv[-1] == "claude"
    for v in aam.API_ENV_VARS:
        assert ["-u", v] == argv[argv.index(v) - 1: argv.index(v) + 1]
    assert not any("claude-owner-api" in a for a in argv)
    assert aam.last_mode("gaika-kz") == "subscription"


def test_subscription_keeps_working_even_when_api_config_is_broken(wt, tmux, tmp_path,
                                                                    monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("{not: [valid")
    monkeypatch.setenv("AGENT_AUTH_MODES_CONFIG", str(bad))
    assert _create(wt)["auth_mode"] == "subscription"


# ── API: explicit opt-in only, never a fallback ───────────────────────────────────
@pytest.mark.parametrize("over,reason", [
    ({"enabled": False}, "api_mode_disabled"),
    ({"agents": {}}, "agent_not_opted_in"),
    ({"limits": {}}, "api_limits_not_configured"),
    ({"limits": {"max_live_api_agents": 0, "max_api_launches_per_day": 5}},
     "api_limits_not_configured"),
    ({"launcher": "/nonexistent/claude-owner-api"}, "launcher_missing")])
def test_api_refusals_start_nothing_at_all(wt, tmux, api_cfg, over, reason, no_secret_reads):
    api_cfg(**over)
    r = _create(wt, auth_mode="api")
    assert r["refused"] == f"auth_mode:{reason}" and not r["agent_created"]
    assert mutations(tmux["calls"]) == []           # no subscription "fallback" either


def test_malformed_config_refuses_api(wt, tmux, tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n")
    monkeypatch.setenv("AGENT_AUTH_MODES_CONFIG", str(bad))
    assert _create(wt, auth_mode="api")["refused"] == "auth_mode:config_not_a_mapping"


def test_an_opted_in_agent_starts_on_the_launcher_with_sonnet(wt, tmux, api_cfg, launcher,
                                                              no_secret_reads):
    r = _create(wt, auth_mode="api")
    assert r["agent_created"] and r["auth_mode"] == "api" and r["model"] == "sonnet"
    assert _new_session(tmux) == [launcher, "--model", "sonnet"]
    assert aam.last_mode("gaika-kz") == "api"
    audit = [json.loads(x) for x in open(os.environ["AGENT_CONTROL_AUDIT"])]
    done = [x for x in audit if x.get("resumed") is True][-1]
    assert done["auth_mode"] == "api" and done["model"] == "sonnet"
    assert "ANTHROPIC" not in json.dumps(audit)       # nothing credential-shaped logged


def test_opt_in_is_bound_to_the_exact_session_and_project(wt, tmux, api_cfg):
    api_cfg(agents={"gaika-kz": {"project_dir": wt["ru"]}})
    assert _create(wt, auth_mode="api")["refused"] == "auth_mode:project_dir_mismatch"
    api_cfg(agents={"gaika-uz": {"project_dir": wt["kz"]}})
    assert _create(wt, auth_mode="api")["refused"] == "auth_mode:agent_not_opted_in"


@pytest.mark.parametrize("model,risk,allow,result", [
    ("opus", None, True, "auth_mode:opus_requires_high_risk_and_entry_permission"),
    ("opus", "high", False, "auth_mode:opus_requires_high_risk_and_entry_permission"),
    ("opus", "high", True, "opus"),
    ("haiku", None, True, "auth_mode:model_not_allowed"),
    ("sonnet; rm -rf /", None, True, "auth_mode:model_invalid"),
    ("claude-opus-4", "high", True, "auth_mode:model_not_allowed")])
def test_model_policy(wt, tmux, api_cfg, model, risk, allow, result):
    api_cfg(agents={"gaika-kz": {"project_dir": wt["kz"], "allow_opus_for_high_risk": allow}})
    r = _create(wt, auth_mode="api", model=model, risk=risk)
    assert (r.get("model") if r.get("agent_created") else r["refused"]) == result


# ── spend guardrails ─────────────────────────────────────────────────────────────
def test_live_api_agent_cap(wt, tmux, api_cfg, monkeypatch):
    aam.record_launch(session="gaika-uz", project_dir=wt["uz"], mode="api")
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [
        {"is_agent": True, "alive": True, "session": "gaika-uz", "target": "gaika-uz:0.0",
         "claude_cwd": wt["uz"]}]})
    r = _create(wt, auth_mode="api")
    assert r["refused"] == "auth_mode:api_live_agent_cap_reached"
    assert mutations(tmux["calls"]) == []


def test_a_dead_api_session_no_longer_counts_as_live(wt, tmux, api_cfg):
    aam.record_launch(session="gaika-uz", project_dir=wt["uz"], mode="api")
    assert _create(wt, auth_mode="api")["auth_mode"] == "api"   # gaika-uz is not alive


def test_daily_launch_cap(wt, tmux, api_cfg):
    import time
    for _ in range(2):
        aam.record_launch(session="old", project_dir="/x", mode="api", now=time.time() - 3600)
    assert _create(wt, auth_mode="api")["refused"] == "auth_mode:api_daily_launch_cap_reached"
    # launches older than 24 h no longer count
    assert aam._api_usage(set(), now=time.time() + 86400)["launches_24h"] == 0


def test_unknown_live_population_refuses_api(wt, tmux, api_cfg, monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] > 1:                 # (creation policy's own read may pass first)
            raise RuntimeError("tmux down")
        return {"agents": []}
    monkeypatch.setattr(ac, "agent_list", boom)
    r = _create(wt, auth_mode="api")
    assert r["refused"] == "auth_mode:live_population_unknown"


@pytest.mark.parametrize("mode,reason", [(0o720, "launcher_group_or_other_writable"),
                                         (0o600, "launcher_not_executable")])
def test_an_unsafe_launcher_file_refuses(wt, tmux, api_cfg, launcher, mode, reason):
    os.chmod(launcher, mode)
    assert _create(wt, auth_mode="api")["refused"] == f"auth_mode:{reason}"


def test_a_launcher_not_owned_by_root_refuses(wt, tmux, api_cfg, launcher):
    if os.getuid() != 0:
        pytest.skip("needs root to chown")
    os.chown(launcher, 65534, 65534)
    assert _create(wt, auth_mode="api")["refused"] == "auth_mode:launcher_not_root_owned"


def test_unknown_mode_is_refused(wt, tmux):
    assert _create(wt, auth_mode="free")["refused"] == "auth_mode:unknown_auth_mode:free"


# ── recovery never starts API mode ─────────────────────────────────────────────────
def test_recovery_command_strips_api_vars_for_claude(wt):
    r = aam.recovery_command("gaika-kz", "claude --resume abc")
    assert r["ok"] and r["cmd"].startswith("env -u ANTHROPIC_API_KEY")
    assert r["cmd"].endswith("claude --resume abc")


@pytest.mark.parametrize("cmd", ["/usr/local/bin/claude-owner-api --resume x",
                                 "claude-owner-api", "ANTHROPIC_API_KEY=x claude"])
def test_recovery_refuses_any_api_shape(wt, cmd):
    assert aam.recovery_command("gaika-kz", cmd) == {
        "ok": False, "reason": "recovery_never_starts_api_mode"}


def test_recovery_refuses_a_session_last_started_on_api(wt):
    aam.record_launch(session="gaika-kz", project_dir=wt["kz"], mode="api")
    assert aam.recovery_command("gaika-kz", "claude --resume abc")["reason"] == \
        "api_session_needs_explicit_relaunch"
    aam.record_launch(session="gaika-kz", project_dir=wt["kz"], mode="subscription")
    assert aam.recovery_command("gaika-kz", "claude --resume abc")["ok"]


def test_session_recovery_uses_the_guard(monkeypatch):
    import inspect
    from core import session_recovery as sr
    src = inspect.getsource(sr)
    i = src.index("cmd = shape.format(")
    assert "recovery_command(session, cmd)" in src[i:i + 600]


# ── the local CLI passes the explicit choice through ─────────────────────────────────
def test_agent_create_cli_passes_auth_mode(monkeypatch, capsys):
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    spec = importlib.util.spec_from_file_location("agent_create_t",
                                                  os.path.join(root, "tools", "agent_create.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    def fake(d, **kw):
        seen.update(kw)
        return {"agent_created": True, "session": "s", "auth_mode": kw.get("auth_mode"),
                "model": kw.get("model")}
    monkeypatch.setattr(ac, "agent_resume", fake)
    assert mod.main(["--dir", "/tmp", "--session", "s", "--auth-mode", "api", "--model",
                     "opus", "--risk", "high"]) == 0
    assert (seen["auth_mode"], seen["model"], seen["risk"]) == ("api", "opus", "high")
    assert mod.main(["--dir", "/tmp", "--session", "s"]) == 0
    assert seen["auth_mode"] is None                  # default: subscription


def test_a_refused_api_request_does_not_even_clean_a_zombie_session(wt, monkeypatch):
    """The auth decision precedes EVERY tmux mutation, incl. the dead-session cleanup."""
    calls = []
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: calls.append(list(args))
                        or (0, "", ""))                       # the session name exists
    monkeypatch.setattr(ac, "_session_liveness", lambda name: {"panes": 1, "dead": 1,
                                                               "live": 0})
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": []})
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda d: os.path.realpath(d))
    r = _create(wt, auth_mode="api")                          # API is disabled by default
    assert r["refused"] == "auth_mode:api_mode_disabled"
    assert mutations(calls) == [] and not any(c[0] == "has-session" for c in calls)
    # the same zombie IS cleaned for an authorized (subscription) start
    r = _create(wt)
    assert r["agent_created"] and ["kill-session", "-t", "gaika-kz"] in calls
