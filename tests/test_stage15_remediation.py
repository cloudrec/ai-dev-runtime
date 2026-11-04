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


# ── F4: recovery fails closed on any shape that cannot be env-wrapped ───────────
@pytest.mark.parametrize("shape", [
    "cd /tmp && claude --resume x", "FOO=1 claude --resume x",
    "/root/.local/bin/claude --resume x", "bash -lc 'claude --resume x'",
    "claude --resume x; env", "claude --resume $(id)", "claude --resume `id`",
    "claude --resume x | tee y", "claude\n--resume x"])
def test_f4_non_plain_shapes_are_refused(shape):
    r = aam.recovery_command("zz", shape, conn=sqlite3.connect(":memory:"))
    assert r == {"ok": False, "reason": "recovery_shape_not_plain_claude"}


def test_f4_plain_claude_is_wrapped_with_the_full_strip():
    r = aam.recovery_command("zz", "claude --resume abc-123", conn=sqlite3.connect(":memory:"))
    assert r["ok"] and r["cmd"].startswith("env ")
    for v in aam.API_ENV_VARS:
        assert f"-u {v}" in r["cmd"]
    assert r["cmd"].endswith("claude --resume abc-123")


# ── F5: HMAC v2 is bound to the request and single-use ──────────────────────────
import asyncio
import hashlib
import hmac
import json
import time
import types

from api import v1

TOKEN = "stage15-test-token"


@pytest.fixture
def hmac_env(monkeypatch):
    monkeypatch.setattr(v1, "_TOKEN", TOKEN)
    monkeypatch.delenv("RUNTIME_HMAC_LEGACY", raising=False)
    v1._seen_sigs.clear()


def _req(method="POST", path="/api/v1/jobs", query="", body=b"{}"):
    async def _body():
        return body
    return types.SimpleNamespace(state=types.SimpleNamespace(), method=method, body=_body,
                                 scope={"raw_path": path.encode(),
                                        "query_string": query.encode()})


def _v2(ts, method="POST", path="/api/v1/jobs", query="", body=b"{}"):
    msg = json.dumps(["v2", ts, method, path, query, hashlib.sha256(body).hexdigest()],
                     separators=(",", ":")).encode()
    return hmac.new(TOKEN.encode(), msg, hashlib.sha256).hexdigest()


def _call(req, ts, sig, ver="2"):
    return asyncio.run(v1._auth(req, authorization=None, x_runtime_timestamp=ts,
                                x_runtime_signature=sig, x_runtime_signature_version=ver))


def test_f5_valid_v2_authenticates_once(hmac_env):
    ts = str(int(time.time()))
    r = _req()
    assert _call(r, ts, _v2(ts)) is True and r.state.auth_method == "hmac"


def test_f5_replay_of_identical_request_is_refused(hmac_env):
    ts = str(int(time.time())); sig = _v2(ts)
    _call(_req(), ts, sig)
    with pytest.raises(v1.HTTPException) as e:
        _call(_req(), ts, sig)
    assert e.value.status_code == 401


@pytest.mark.parametrize("kw", [dict(path="/api/v1/fullcycle/reviews/1/ack"),
                                dict(method="DELETE"), dict(query="x=1"),
                                dict(body=b'{"evil":1}')])
def test_f5_signature_does_not_transfer_to_another_request(hmac_env, kw):
    ts = str(int(time.time()))
    sig = _v2(ts)                                  # signed for POST /api/v1/jobs {}
    with pytest.raises(v1.HTTPException) as e:
        _call(_req(**kw), ts, sig)
    assert e.value.status_code == 401


def test_f5_legacy_signature_cannot_pass_as_v2_and_can_be_disabled(hmac_env, monkeypatch):
    ts = str(int(time.time()))
    legacy = hmac.new(TOKEN.encode(), ts.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(v1.HTTPException):
        _call(_req(), ts, legacy, ver="2")
    assert _call(_req(), ts, legacy, ver=None) is True          # compat default
    monkeypatch.setenv("RUNTIME_HMAC_LEGACY", "0")
    with pytest.raises(v1.HTTPException) as e:
        _call(_req(), ts, legacy, ver=None)
    assert e.value.status_code == 401


def test_r2_v2_fields_cannot_run_into_each_other():
    body = b"{}"
    assert v1.hmac_v2_message("1", "GET", "/a|b", "", body) != \
        v1.hmac_v2_message("1", "GET", "/a", "b|", body)
    assert v1.hmac_v2_message("1", "GET", '/a","b', "", body) != \
        v1.hmac_v2_message("1", "GET", "/a", 'b",""', body)


def test_r2_v2_message_is_the_documented_bytes():
    sha = hashlib.sha256(b"x").hexdigest()
    assert v1.hmac_v2_message("12", "post", "/p", "q=1", b"x") == \
        f'["v2","12","POST","/p","q=1","{sha}"]'.encode()


def test_r2_the_old_pipe_format_is_refused(hmac_env):
    ts = str(int(time.time()))
    msg = "|".join(("v2", ts, "POST", "/api/v1/jobs", "", hashlib.sha256(b"{}").hexdigest()))
    sig = hmac.new(TOKEN.encode(), msg.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(v1.HTTPException):
        _call(_req(), ts, sig)


def test_f5_stale_v2_is_refused(hmac_env):
    ts = str(int(time.time()) - 10_000)
    with pytest.raises(v1.HTTPException):
        _call(_req(), ts, _v2(ts))


def test_f5_replay_cache_is_bounded_and_fails_closed(hmac_env, monkeypatch):
    monkeypatch.setattr(v1, "_SEEN_MAX", 3)
    assert [v1._hmac_first_use(f"s{i}") for i in range(5)] == [True, True, True, False, False]
