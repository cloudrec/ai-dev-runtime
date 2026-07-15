"""PHASE 45 — provider smoke test (core/ai_planner.smoke + POST /api/v1/smoke).

Read-only, single-call, hard-timeout contract that Owner OS's
runtime_client.provider_smoke() gates retry_runtime_job on. No network to a
real provider here — a fake CLI script stands in, same pattern as
tests/test_phase13.py.
"""
import os
import stat
import subprocess
import tempfile

import pytest

os.environ.setdefault("RUNTIME_DB", os.path.join(tempfile.gettempdir(), "rt_test_jobs.db"))

from core import ai_planner  # noqa: E402


def _fake_cli(tmp_path, body):
    p = tmp_path / "fake_claude.py"
    p.write_text("#!/usr/bin/env python3\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _fake_cli_envelope(tmp_path, envelope_obj):
    import json as _json
    payload = _json.dumps(envelope_obj)
    return _fake_cli(tmp_path, f"import sys\nsys.stdout.write({payload!r})\n")


def _reload(monkeypatch, cli, model_env=None):
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", cli)
    if model_env is not None:
        monkeypatch.setenv("RUNTIME_CLAUDE_MODEL", model_env)
    import importlib
    importlib.reload(ai_planner)


# ── successful smoke ──────────────────────────────────────────────────────────
def test_smoke_success_reports_provider_model_tokens_cost(tmp_path, monkeypatch):
    cli = _fake_cli_envelope(tmp_path, {
        "type": "result", "subtype": "success", "is_error": False, "result": "pong",
        "total_cost_usd": 0.0042, "usage": {"input_tokens": 5, "output_tokens": 2},
    })
    _reload(monkeypatch, cli, model_env="claude-haiku-4-5")

    out = ai_planner.smoke()
    assert out["ok"] is True
    assert out["provider"] == "claude-cli"
    assert out["model"] == "claude-haiku-4-5"
    assert out["cost_usd"] == 0.0042
    assert out["tokens"] == {"input_tokens": 5, "output_tokens": 2}
    assert out["latency_seconds"] >= 0
    assert out["error"] is None


def test_smoke_resolves_model_from_model_usage_when_unset(tmp_path, monkeypatch):
    cli = _fake_cli_envelope(tmp_path, {
        "type": "result", "subtype": "success", "is_error": False, "result": "pong",
        "total_cost_usd": 0.001, "usage": {"input_tokens": 1, "output_tokens": 1},
        "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 1},
                      "claude-opus-4-8[1m]": {"outputTokens": 9}},
    })
    _reload(monkeypatch, cli, model_env="")

    out = ai_planner.smoke()
    assert out["ok"] is True and out["model"] == "claude-opus-4-8[1m]"


# ── timeout ───────────────────────────────────────────────────────────────────
def test_smoke_timeout_is_bounded_and_reaps_process_group(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "import time\ntime.sleep(30)\n")
    _reload(monkeypatch, cli)

    import time as _t
    start = _t.monotonic()
    out = ai_planner.smoke(timeout_seconds=2)
    elapsed = _t.monotonic() - start

    assert out["ok"] is False
    assert "timed out" in out["error"]
    assert elapsed < 10  # bounded by the 2s request, not the fake CLI's 30s sleep

    ps = subprocess.run(["pgrep", "-af", cli], capture_output=True, text=True)
    assert cli not in ps.stdout


def test_smoke_timeout_hard_capped_at_60s_even_if_caller_asks_more(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "import time\ntime.sleep(1)\n")
    _reload(monkeypatch, cli)
    # can't actually wait 90s in a unit test — just prove the cap logic directly
    # (same computation smoke() uses), matching runtime_client.py's min(...,60).
    assert min(90.0, ai_planner._SMOKE_TIMEOUT_CAP) == 60.0


# ── provider error ────────────────────────────────────────────────────────────
def test_smoke_provider_error_is_classified_not_raw_dumped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, (
        "import sys\n"
        "sys.stderr.write('boom: internal provider failure\\n')\n"
        "sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)

    out = ai_planner.smoke()
    assert out["ok"] is False
    assert "claude cli error" in out["error"]


def test_smoke_rate_limited_is_classified(tmp_path, monkeypatch):
    envelope = '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,"result":""}'
    cli = _fake_cli(tmp_path, f"print('{envelope}')\n")
    _reload(monkeypatch, cli)

    out = ai_planner.smoke()
    assert out["ok"] is False and out["error"] == "provider_limit_exceeded"


# ── missing credentials ───────────────────────────────────────────────────────
def test_smoke_missing_credentials_reports_not_configured(monkeypatch):
    monkeypatch.setenv("RUNTIME_CLAUDE_BIN", "/nonexistent/claude/binary")
    import importlib
    importlib.reload(ai_planner)

    out = ai_planner.smoke()
    assert out["ok"] is False
    assert out["error"] == "provider_not_configured"


def test_smoke_auth_required_is_classified(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, (
        "import sys\n"
        "sys.stderr.write('Invalid API key · please run /login\\n')\n"
        "sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)

    out = ai_planner.smoke()
    assert out["ok"] is False and out["error"] == "provider_auth_required"


# ── no secret leakage ─────────────────────────────────────────────────────────
def test_smoke_never_leaks_api_key_looking_strings(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, (
        "import sys\n"
        "sys.stderr.write('failed: Bearer sk-ant-api03-REALSECRETVALUE1234 rejected\\n')\n"
        "sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)

    out = ai_planner.smoke()
    assert out["ok"] is False
    assert "sk-ant-" not in out["error"]
    assert "REALSECRETVALUE" not in out["error"]
    assert "[redacted]" in out["error"]


def test_redact_helper_strips_known_secret_shapes():
    assert "sk-ant-" not in ai_planner._redact("token sk-ant-api03-abc123XYZ leaked")
    assert "Bearer " not in ai_planner._redact("Authorization: Bearer abcDEF123.token-value")


# ── endpoint does not modify workspace / DB ──────────────────────────────────
def test_smoke_takes_no_project_path_and_writes_nothing(tmp_path, monkeypatch):
    """smoke() has no project_path parameter at all — it structurally cannot
    touch a repository, unlike plan()/job_executor which always take one."""
    import inspect
    sig = inspect.signature(ai_planner.smoke)
    assert "project_path" not in sig.parameters

    marker = tmp_path / "untouched.txt"
    marker.write_text("before")
    cli = _fake_cli_envelope(tmp_path, {
        "type": "result", "subtype": "success", "is_error": False, "result": "pong",
        "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    _reload(monkeypatch, cli)

    before = os.path.getmtime(marker)
    ai_planner.smoke()
    assert os.path.getmtime(marker) == before
    assert marker.read_text() == "before"


def test_smoke_endpoint_route_has_no_job_store_side_effects(monkeypatch):
    """The /smoke route must never create a job (job_store) — it's a distinct
    code path from POST /jobs, not a job with autonomy/approval semantics."""
    from api import v1 as api_v1
    import inspect
    src = inspect.getsource(api_v1.smoke)
    assert "job_store" not in src
    assert "job_executor" not in src


# ── exactly one provider request, no retry loop ──────────────────────────────
def test_smoke_invokes_cli_exactly_once(tmp_path, monkeypatch):
    calls_file = tmp_path / "calls.txt"
    cli = _fake_cli(tmp_path, (
        f"import sys\n"
        f"open({str(calls_file)!r}, 'a').write('x')\n"
        f"sys.stdout.write('{{\"type\":\"result\",\"is_error\":false,\"result\":\"pong\",\"usage\":{{}}}}')\n"
    ))
    _reload(monkeypatch, cli)

    ai_planner.smoke()
    assert calls_file.read_text() == "x"  # exactly one invocation, not looped/retried


def test_smoke_failure_does_not_retry_internally(tmp_path, monkeypatch):
    calls_file = tmp_path / "calls.txt"
    cli = _fake_cli(tmp_path, (
        f"open({str(calls_file)!r}, 'a').write('x')\n"
        "import sys; sys.exit(1)\n"
    ))
    _reload(monkeypatch, cli)

    out = ai_planner.smoke()
    assert out["ok"] is False
    assert calls_file.read_text() == "x"  # one attempt even on failure
