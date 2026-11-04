"""Fixes after the Fable audit of 05073f4..e96e72d: M1, L1-L5 and its open notes.

Offline: tmp DBs, fake executables, no network, no live file, no money.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from core import agent_auth_mode as aam
from core import agent_control as ac
from tests.test_agent_auth_mode import api_cfg, launcher, no_secret_reads  # noqa: F401
from tests.test_agent_creation_policy import KEY, mutations, tmux, wt  # noqa: F401
from tests.test_stage15_f3_api_owner_proof import _commit, _sign, _sql, _state


def _triggers():
    c = sqlite3.connect(ac._db_path())
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    finally:
        c.close()


def _use_general_proof(text="x"):
    proof = ac.sign_owner_proof(KEY, "agent_send", "t:0.0", text)
    return ac.verify_owner_proof(proof, "agent_send", "t:0.0", text)


# ── M1: routine proof use never repairs the ledger ───────────────────────────────
def test_m1_a_dropped_proof_trigger_stays_detected_after_proof_use(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    _sql("DROP TRIGGER owner_proof_used_no_delete")
    assert _state() == "api_ledger_tampered"
    assert _use_general_proof() == ""
    assert "owner_proof_used_no_delete" not in _triggers()
    assert _state() == "api_ledger_tampered"
    assert _commit(wt, _sign(wt)) == "api_ledger_tampered"


def test_m1_a_dropped_proof_table_is_not_repaired_by_proof_use(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    _sql("DROP TABLE owner_proof_used")
    assert _use_general_proof() == ""
    assert _state() == "api_ledger_tampered"


def test_m1_a_new_ledger_gets_its_triggers_from_the_first_proof(wt):  # noqa: F811
    assert _use_general_proof() == ""
    assert set(aam._PROOF_TRIGGERS) <= _triggers()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _sql("DELETE FROM owner_proof_used")


def _pre_r6_db():
    """A DB as code before 47264ad left it: chain and spent table, no proof triggers."""
    c = sqlite3.connect(ac._db_path())
    c.close()
    aam._chain(aam._ledger())
    _sql(*[f"DROP TRIGGER {t}" for t in aam._PROOF_TRIGGERS])


def test_m1_upgrade_a_pre_r6_db_with_reanchor(wt, api_cfg):  # noqa: F811
    _pre_r6_db()
    assert _commit(wt, _sign(wt)) == "api_ledger_tampered"
    r = aam.reanchor()
    assert r["ok"] and r["restored_triggers"] == sorted(aam._PROOF_TRIGGERS)
    assert _commit(wt, _sign(wt)) == ""
    assert aam.reanchor()["restored_triggers"] == []


def test_m1_reanchor_refuses_damaged_rows_and_restores_nothing(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    _sql("DROP TRIGGER agent_auth_api_chain_no_update",
         "UPDATE agent_auth_api_chain SET actor='x'")
    assert aam.reanchor() == {"ok": False, "reason": "api_ledger_tampered"}
    assert "agent_auth_api_chain_no_update" not in _triggers()


def test_m1_reanchor_needs_the_owner_key(wt, api_cfg, monkeypatch):  # noqa: F811
    aam._chain(aam._ledger())
    _sql("DROP TRIGGER owner_proof_used_no_delete")
    monkeypatch.setattr(ac, "_owner_key", lambda: None)
    assert aam.reanchor() == {"ok": False, "reason": "owner_proof_unconfigured"}
    assert "owner_proof_used_no_delete" not in _triggers()


def test_reanchor_clears_the_anchor_of_a_chain_reset_to_empty(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    _sql(*[f"DROP TRIGGER {t}" for t in (*aam._CHAIN_TRIGGERS, *aam._PROOF_TRIGGERS)],
         "DELETE FROM agent_auth_api_chain", "DELETE FROM owner_proof_used")
    assert _state() == "api_ledger_tampered"
    assert aam.reanchor()["ok"] and not os.path.exists(aam._anchor_path())
    assert _state() is None


def test_a_missing_owner_key_is_not_reported_as_tampering(wt, api_cfg, monkeypatch):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    monkeypatch.setattr(ac, "_owner_key", lambda: None)
    assert _state() == "owner_proof_unconfigured"


# ── L1: no settings file can stop a launch by accident ───────────────────────────
@pytest.mark.parametrize("content", ["[" * 100_000, "{" + '"a":' * 50_000, "x" * (1 << 21)])
def test_l1_a_hostile_settings_file_counts_as_unreadable(tmp_path, content):
    proj = tmp_path / "p"
    (proj / ".claude").mkdir(parents=True)
    path = proj / ".claude" / "settings.json"
    path.write_text(content)
    found = aam.settings_api_overrides(str(proj), config_dir=str(tmp_path / "none"))
    assert f"{path}:unreadable" in found


def test_l1_a_hostile_settings_file_does_not_block_a_resume(wt, tmux):  # noqa: F811
    d = os.path.join(wt["kz"], ".claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.json"), "w") as fh:
        fh.write("[" * 100_000)
    r = ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt")
    assert r["resumed"] is True


# ── L3: the one-row anchor lag closes at the next attempt ────────────────────────
def _fail_anchor_write_once(monkeypatch):
    real = aam._write_anchor

    def fail(rows, head):
        monkeypatch.setattr(aam, "_write_anchor", real)
        raise OSError("disk full")
    monkeypatch.setattr(aam, "_write_anchor", fail)


def _anchored_rows():
    return json.load(open(aam._anchor_path()))["rows"]


def test_l3_a_refused_attempt_anchors_the_lagging_row(wt, api_cfg, monkeypatch):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    proof = _sign(wt)
    _fail_anchor_write_once(monkeypatch)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    assert _anchored_rows() == 1
    assert _commit(wt, proof) == "owner_proof_replayed"
    assert _anchored_rows() == 2
    _sql(*[f"DROP TRIGGER {t}" for t in (*aam._CHAIN_TRIGGERS, *aam._PROOF_TRIGGERS)],
         "DELETE FROM agent_auth_api_chain WHERE seq=2",
         "DELETE FROM owner_proof_used WHERE nonce='{}'".format(proof["nonce"]))
    assert _commit(wt, proof) == "api_ledger_tampered"


def test_l3_the_first_row_is_anchored_too(wt, api_cfg, monkeypatch):  # noqa: F811
    proof = _sign(wt)
    _fail_anchor_write_once(monkeypatch)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    assert not os.path.exists(aam._anchor_path())
    assert _commit(wt, proof) == "owner_proof_replayed"
    assert _anchored_rows() == 1


def test_l3_a_persistent_write_failure_starts_nothing(wt, api_cfg, monkeypatch):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    proof = _sign(wt)

    def fail(rows, head):
        raise OSError("disk full")
    monkeypatch.setattr(aam, "_write_anchor", fail)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    assert _commit(wt, _sign(wt)) == "api_anchor_write_failed"
    assert sqlite3.connect(ac._db_path()).execute(
        "SELECT COUNT(*) FROM agent_auth_api_chain").fetchone()[0] == 2


# ── L2: only command position counts ─────────────────────────────────────────────
@pytest.mark.parametrize("argv", [
    ["bash", "-c", "pytest -k claude"], ["env", "FOO=claude", "pytest"],
    ["sh", "-c", "git log --grep claude | wc -l"],
    ["timeout", "60", "npm", "test", "--", "--grep", "claude"],
    ["sh", "-c", "echo 'a; claude b'"], ["bash", "run_claude.sh"], ["bash", "claude"],
    ["sudo", "-u", "claude", "pytest"], ["nice", "-n", "5", "pytest", "claude"],
    ["sh", "-c", "X=claude pytest"], ["xargs", "grep", "claude"], ["sh"], ["sh", "-c"], []])
def test_l2_a_word_in_an_argument_is_data(argv):
    from core.fullcycle import hosts
    assert hosts._runs_claude(argv) is False


@pytest.mark.parametrize("argv", [
    ["claude"], ["/usr/local/bin/claude", "-p", "x"], ["env", "X=1", "claude"],
    ["env", "-u", "Y", "claude"], ["timeout", "-k", "5", "60s", "claude"],
    ["nohup", "claude"], ["nice", "-n", "5", "claude"], ["sudo", "-u", "root", "claude"],
    ["xargs", "-n", "1", "claude"], ["setsid", "claude"],
    ["sh", "-c", "claude -p x"], ["bash", "-lc", "cd x && claude -p y"],
    ["bash", "-c", "true; claude"], ["bash", "-c", "true\nclaude"],
    ["sh", "-c", "X=1 claude"], ["sh", "-c", "'claude' -p x"], ["sh", "-c", '"claude"'],
    ["sh", "-c", "(claude)"], ["sh", "-c", "echo $(claude -p x)"],
    ["sh", "-c", "a | claude"], ["sh", "-c", "if true; then claude; fi"],
    ["bash", "-o", "pipefail", "-c", "claude"], ["env", "sh", "-c", "nohup claude"],
    ["sh", "-c", "sh -c 'claude -p x'"], ["sh", "-c", "exec claude"],
    ["sh", "-c", "claude 'unbalanced"]])
def test_l2_claude_in_command_position_is_seen(argv):
    from core.fullcycle import hosts
    assert hosts._runs_claude(argv) is True


def test_l2_a_check_keeps_its_environment(monkeypatch):
    from core.fullcycle import hosts
    monkeypatch.setenv("VIRTUAL_ENV", "/v")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel")
    env = hosts._child_env(["bash", "-c", "pytest -k claude"], {"PYTHONDONTWRITEBYTECODE": "1"})
    assert env["VIRTUAL_ENV"] == "/v" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "ANTHROPIC_API_KEY" not in env


# ── L4: without raw_path a decoded ? stays in the signed path ────────────────────
def test_l4_a_decoded_question_mark_is_signed_with_the_path():
    from types import SimpleNamespace

    from api.v1 import _signed_target
    req = SimpleNamespace(scope={"path": "/jobs/x?evil", "query_string": b"a=1"})
    assert _signed_target(req) == ("/jobs/x?evil", "a=1")
    req = SimpleNamespace(scope={"raw_path": b"/jobs/x%3Fevil?a=1", "query_string": b"a=1"})
    assert _signed_target(req) == ("/jobs/x%3Fevil", "a=1")


# ── L5: the launcher is matched as a whole word ──────────────────────────────────
def _launcher_named(monkeypatch, path):
    monkeypatch.setattr(aam, "load_config", lambda: {"launcher": path})


@pytest.mark.parametrize("argv", [
    ["pytest", "/opt/claude-owner-api-docs/t.py"], ["pytest", "tests/test_claude-owner-api.py"],
    ["python3", "my-claude-owner-api"]])
def test_l5_a_longer_name_is_not_the_launcher(monkeypatch, argv):
    from core.fullcycle import gates
    assert gates._names_api_launcher(argv) is False


def test_l5_a_short_configured_name_spares_project_paths(monkeypatch):
    from core.fullcycle import gates
    _launcher_named(monkeypatch, "/usr/local/bin/api")
    assert gates._names_api_launcher(["pytest", "tests/api"]) is False
    assert gates._names_api_launcher(["pytest", "tests/test_api.py"]) is False
    assert gates._names_api_launcher(["/usr/local/bin/api"]) is True


@pytest.mark.parametrize("argv", [
    ["claude-owner-api"], ["/usr/local/bin/claude-owner-api", "-p", "x"],
    ["sh", "-c", "claude-owner-api -p x"], ["sh", "-c", "true;claude-owner-api"],
    ["sh", "-c", "X=$(claude-owner-api)"], ["env", "A=claude-owner-api"],
    ["../bin/claude-owner-api"], ["./claude-owner-api"]])
def test_l5_the_launcher_is_still_refused(argv):
    from core.fullcycle import gates
    assert gates._names_api_launcher(argv) is True


# ── F7: fullcycle children are covered too ───────────────────────────────────────
@pytest.fixture
def project_with_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-config"))
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True)
    (d / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "sentinel-value"}))
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\necho ran\n")
    claude.chmod(0o755)
    return str(d), str(claude)


def test_f7_a_settings_override_is_reported_not_refused_by_default(
        project_with_helper, caplog, monkeypatch):
    from core.fullcycle.hosts import LocalHost
    monkeypatch.delenv("AGENT_AUTH_SETTINGS_GUARD", raising=False)
    d, claude = project_with_helper
    rc, out, _ = LocalHost().run([claude], cwd=d)
    assert rc == 0 and out.strip() == "ran"
    assert "apiKeyHelper" in caplog.text and "sentinel-value" not in caplog.text


def test_f7_enforce_refuses_the_child(project_with_helper, monkeypatch):
    from core.fullcycle.hosts import LocalHost
    monkeypatch.setenv("AGENT_AUTH_SETTINGS_GUARD", "enforce")
    d, claude = project_with_helper
    rc, out, err = LocalHost().run([claude], cwd=d)
    assert rc == 126 and out == "" and "apiKeyHelper" in err and "sentinel-value" not in err
    assert LocalHost().run(["sh", "-c", "echo fine"], cwd=d)[0] == 0
