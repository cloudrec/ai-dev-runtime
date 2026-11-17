"""The agent-control database path, validated in one place.

Unset means the live default; a blank or relative AGENT_CONTROL_DB is a
misconfiguration and is refused rather than letting sqlite create a file named after
the value in whatever directory the process happens to run in (2026-11-17: a file
literally named '  ' appeared in a worktree during a test run).
"""
from __future__ import annotations

import os

DEFAULT_CONTROL_DB = "/root/ai-dev-runtime/agent_control.db"


def control_db_path() -> str:
    raw = os.environ.get("AGENT_CONTROL_DB")
    if raw is None:
        return DEFAULT_CONTROL_DB
    path = raw.strip()
    if not path or not os.path.isabs(path) or path != raw:
        raise ValueError(f"AGENT_CONTROL_DB must be an absolute path, got {raw!r}")
    return path
