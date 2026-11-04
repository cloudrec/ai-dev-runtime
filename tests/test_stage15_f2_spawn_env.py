"""F2: no `claude` subscription spawn inherits API credentials or routing overrides.

A fake executable records the environment it really received. Offline: nothing is sent
anywhere and no secret is read; every credential here is a sentinel.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from core import agent_auth_mode as aam

ROOT = Path(__file__).resolve().parent.parent
SENTINEL = "sentinel-must-not-leak"
# named and unnamed: the allowlist has to stop a credential nobody listed yet
LEAK_VARS = (*aam.API_ENV_VARS, "ANTHROPIC_FUTURE_CREDENTIAL", "CLAUDE_CODE_USE_SOMETHING_NEW",
             "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "NODE_OPTIONS")
ENVELOPE = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
            "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A fake `claude`: dumps its environment and argv, prints what the caller expects."""
    seen = tmp_path / "seen.jsonl"
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
with open({str(seen)!r}, "a") as fh:
    fh.write(json.dumps({{"env": dict(os.environ), "argv": sys.argv[1:]}}) + "\\n")
if sys.argv[1:2] == ["-p"]:
    sys.stdin.read()
    print(json.dumps({ENVELOPE!r}))
elif sys.argv[1:3] == ["agents", "--json"]:
    print(json.dumps([{{"sessionId": "abc", "pid": 7, "cwd": "/tmp", "name": "n",
                        "kind": "interactive", "status": "idle"}}]))
elif sys.argv[1:2] == ["--version"]:
    print("9.9.9 (Claude Code)")
else:
    print("agents --json")
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    for v in LEAK_VARS:
        monkeypatch.setenv(v, SENTINEL)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-login")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")

    def calls():
        return [json.loads(x) for x in seen.read_text().splitlines()]
    return str(script), calls


def _assert_clean(call):
    env = call["env"]
    assert not [k for k, v in env.items() if v == SENTINEL]
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    # the login and the host's network path survive: these are what a session needs
    assert env["CLAUDE_CONFIG_DIR"].endswith("cfg")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-login"
    assert env["HOME"] and env["PATH"] and env["LC_ALL"] and env["HTTPS_PROXY"]


@pytest.fixture
def planner(fake_claude, monkeypatch):
    from core import ai_planner
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", fake_claude[0])
    importlib.reload(ai_planner)
    yield ai_planner, fake_claude[1]
    monkeypatch.undo()
    importlib.reload(ai_planner)


def test_planner_smoke_is_clean_and_still_works(planner):
    mod, calls = planner
    r = mod.smoke()
    assert r["ok"] is True, r
    (call,) = calls()
    _assert_clean(call)
    assert call["argv"][:1] == ["-p"] and "--strict-mcp-config" in call["argv"]


def test_planner_plan_spawn_is_clean(planner, tmp_path):
    mod, calls = planner
    with pytest.raises(Exception):      # the fake's text is no plan; the spawn is the point
        mod.plan("g", "i", str(tmp_path), [])
    _assert_clean(calls()[0])


def test_native_sessions_listing_is_clean_and_still_parses(fake_claude, monkeypatch):
    from core import native_sessions as ns
    monkeypatch.setattr(ns, "_BIN", fake_claude[0])
    rows = ns._list_raw()
    assert rows and rows[0]["sessionId"] == "abc"
    _assert_clean(fake_claude[1]()[0])


def test_native_agents_probes_are_clean_and_still_work(fake_claude, monkeypatch):
    from core import native_agents as na
    monkeypatch.setattr(na, "CLAUDE_BIN", fake_claude[0])
    assert na.claude_version() == (9, 9, 9)
    assert na.list_sessions(inventory=lambda: {"agents": []})["source"] == na.SOURCE_NATIVE
    calls = fake_claude[1]()
    assert len(calls) == 2
    for call in calls:
        _assert_clean(call)


def test_the_env_helper_is_an_allowlist_and_the_denylist_always_wins():
    env = aam.subscription_env({"PATH": "/bin", "LC_ALL": "C", "XDG_CONFIG_HOME": "/x",
                                "ANTHROPIC_API_KEY": "k", "SOMETHING_ELSE": "y"})
    assert env == {"PATH": "/bin", "LC_ALL": "C", "XDG_CONFIG_HOME": "/x"}
    assert not (aam.SUBSCRIPTION_ENV_KEEP & set(aam.API_ENV_VARS))


# ── guard: a new `claude` spawn without the helper fails here ───────────────────
SPAWN_FUNCS = {"run", "Popen", "check_output", "check_call", "call", "create_subprocess_exec"}
CLAUDE_REF = re.compile(r"(?<![A-Z])_CLAUDE\b|\bCLAUDE_BIN\b|which\(\s*[\"']claude[\"']")


def _is_claude_literal(node) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        node = node.elts[0]
    return isinstance(node, ast.Constant) and isinstance(node.value, str) \
        and os.path.basename(node.value.split()[0] if node.value.split() else "") == "claude"


def _unsafe_spawns(src: str) -> list:
    """Line numbers of process spawns that could run `claude` without subscription_env. In a
    module holding a claude binary path, every spawn counts: argv is often built elsewhere."""
    binary_module = bool(CLAUDE_REF.search(src))
    bad = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in SPAWN_FUNCS or not isinstance(node.func.value, (ast.Name,
                                                                                 ast.Attribute)):
            continue
        base = ast.unparse(node.func.value)
        if base not in ("subprocess", "asyncio", "sp", "_sp"):
            continue
        if not (binary_module or (node.args and _is_claude_literal(node.args[0]))):
            continue
        env = next((k.value for k in node.keywords if k.arg == "env"), None)
        if env is None or "subscription_env" not in ast.unparse(env):
            bad.append(node.lineno)
    return bad


def test_every_claude_spawn_site_uses_the_helper():
    bad, guarded = [], []
    for d in ("core", "api", "tools", "hooks"):
        for f in (ROOT / d).rglob("*.py"):
            rel = str(f.relative_to(ROOT))
            src = f.read_text(errors="replace")
            bad += [f"{rel}:{n}" for n in _unsafe_spawns(src)]
            if "env=subscription_env()" in src:
                guarded.append(rel)
    assert bad == []
    assert sorted(guarded) == ["core/ai_planner.py", "core/native_agents.py",
                               "core/native_sessions.py"]


@pytest.mark.parametrize("src,expected", [
    ('import subprocess\nsubprocess.run(["claude", "-p"])\n', [2]),
    ('import subprocess\nsubprocess.Popen(["/root/.local/bin/claude"], env=None)\n', [2]),
    ('import subprocess, os\n_CLAUDE = "x"\nsubprocess.Popen([_CLAUDE], env=dict(os.environ))\n',
     [3]),
    ('import subprocess\nsubprocess.run(["claude"], env=subscription_env())\n', []),
    ('import subprocess\nsubprocess.run(["git", "status"])\n', []),
])
def test_the_guard_itself_catches_an_unsafe_spawn(src, expected):
    assert _unsafe_spawns(src) == expected
