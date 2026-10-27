#!/usr/bin/env python3
"""Local controller entry for creating ONE scoped agent session (host shell only).

  venv/bin/python tools/agent_create.py --dir /opt/gaika-worktrees/ru --session gaika-ru

Runs agent_control.agent_resume in-process as caller class `owneros_controller`. The API
can never produce that class (every API caller is `api`, whatever X-Runtime-Actor says), so
a scope listing only `owneros_controller` lets the local controller create agents while
ChatGPT/MCP cannot. All gates still apply: scope, exact worktree, session name, one per
worktree, max_concurrent, max_live_agents, payment-agent hard deny. Prints one JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CALLER = "owneros_controller"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--conversation")
    a = ap.parse_args(argv)
    from core import agent_control as ac
    try:
        r = ac.agent_resume(a.dir, conversation_id=a.conversation, session_name=a.session,
                            actor=CALLER)
    except ac.AgentControlError as e:
        r = {"agent_created": False, "error": str(e)[:300]}
    print(json.dumps({k: r.get(k) for k in ("agent_created", "session", "target", "refused",
                                            "scope", "reason", "error") if k in r}))
    return 0 if r.get("agent_created") else 1


if __name__ == "__main__":
    sys.exit(main())
