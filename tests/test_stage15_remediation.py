"""Stage 15 pre-audit remediation: offline regression tests (no network, no live DB)."""
import os
import sqlite3
import stat
import subprocess

import pytest

from core import agent_auth_mode as aam


# ── F7: the strip list covers every re-routing / alternate-billing switch ───────
F7_VARS = ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
           "AWS_BEARER_TOKEN_BEDROCK")


def test_f7_strip_list_includes_rerouting_switches():
    for v in F7_VARS + ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                        "ANTHROPIC_CUSTOM_HEADERS"):
        assert v in aam.API_ENV_VARS


def test_f7_spawned_subscription_process_sees_none_of_them(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nenv | grep -E '^(ANTHROPIC_|CLAUDE_CODE_USE_|AWS_BEARER)' || true\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    for v in aam.API_ENV_VARS:
        monkeypatch.setenv(v, "sentinel-secret")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    out = subprocess.run(aam.subscription_argv(), capture_output=True, text=True).stdout
    assert out.strip() == "", out
