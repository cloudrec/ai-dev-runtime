#!/usr/bin/env python3
"""Owner's check before a run under AGENT_RUN_AS: run as root, on the host. Every probe runs
as the agent's uid, the way a real pane or check does. Prints names and yes or no, never a
value. Exit 0 only when everything passes.

    AGENT_RUN_AS=fc-agent tools/agent_uid_preflight.py --deny /opt/sandbox-mcp/.env
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from core import agent_privilege as priv  # noqa: E402
from core import tmux_control  # noqa: E402

DENY = ("/root", "/opt/seo/.env", "/etc/ai-runtime/runtime-clients.json",
        "/var/run/docker.sock")


def _as_agent(t, argv: list) -> int:
    try:
        return subprocess.run(priv.host_argv(t, argv), env=priv.child_env(t), cwd="/",
                              capture_output=True, timeout=20).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 125


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deny", action="append", default=[], help="another path the agent must not reach")
    ap.add_argument("--project", help="project dir the agent must be able to use")
    a = ap.parse_args(argv)
    try:
        t = priv.target()
    except priv.AgentPrivilegeError as e:
        print(json.dumps({"ok": False, "reason": f"agent_run_as:{e}"}))
        return 1
    if not t:
        print(json.dumps({"ok": False, "reason": "AGENT_RUN_AS is not set"}))
        return 1
    paths = {*DENY, *a.deny, tmux_control.socket_path(),
             os.getenv("RUNTIME_CLIENT_TOKENS_FILE") or DENY[2]}
    checks = {f"cannot_reach:{p}": _as_agent(t, ["sh", "-c", 'test ! -r "$1" && test ! -w "$1"',
                                                 "sh", p]) == 0 for p in sorted(paths)}
    checks["uid_is_not_root"] = _as_agent(t, ["sh", "-c", 'test "$(id -u)" != 0']) == 0
    checks["no_extra_groups"] = _as_agent(t, ["sh", "-c", 'test "$(id -G)" = "$(id -g)"']) == 0
    checks["docker_refused"] = _as_agent(t, ["docker", "ps"]) != 0
    checks["sudo_refused"] = _as_agent(t, ["sudo", "-n", "true"]) != 0
    checks["claude_runs"] = _as_agent(t, ["claude", "--version"]) == 0
    if a.project:
        checks["project_usable"] = priv.can_use_dir(t, a.project)
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "run_as": t.name, "checks": checks}, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
