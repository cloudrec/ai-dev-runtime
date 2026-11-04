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


def test_ld_an_old_ledger_without_a_chain_gets_its_triggers(wt):  # noqa: F811
    _sql(aam._PROOF_TABLE)
    assert not set(aam._PROOF_TRIGGERS) & _triggers()
    assert _use_general_proof() == ""
    assert set(aam._PROOF_TRIGGERS) <= _triggers()


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
    assert aam.reanchor()["reason"] == "api_chain_needs_owner_review"
    assert aam.reanchor(accept_chain=True)["ok"]
    assert _anchored_rows() == 0
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
def _fail_anchor_write_once(monkeypatch, after=0):
    real = aam._write_anchor
    left = [after]

    def fail(rows, head):
        if left[0]:
            left[0] -= 1
            return real(rows, head)
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
    _fail_anchor_write_once(monkeypatch, after=1)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    assert _anchored_rows() == 0 and _state() is None
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
    ["sh", "-c", "X=claude pytest"], ["xargs", "grep", "claude"], ["sh"], ["sh", "-c"], [],
    ["sh", "-c", "command -v claude"], ["command", "-V", "claude"],
    ["sh", "-c", "cat f > claude"], ["sh", "-c", "pytest 2>&1 > claude; wc -l < claude"]])
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
    ["sh", "-c", "claude 'unbalanced"], ["sh", "-c", "echo `claude -p x`"],
    ["bash", "--rcfile", "f", "-c", "claude"], ["sh", "-c", "exec -a name claude"],
    ["sh", "-c", "builtin claude"], ["bash", "-c", "coproc claude"],
    ["sh", "-c", "> out claude"], ["sh", "-c", "claude 2>&1"],
    ["sh", "-c", "2>/dev/null claude -p x"], ["sh", "-c", "1> out claude"],
    ["sh", "-c", ">| out claude"], ["sh", "-c", "&> out claude"]])
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
    ["../bin/claude-owner-api"], ["./claude-owner-api"],
    ["sh", "-c", "~/../usr/local/bin/claude-owner-api"],
    ["sh", "-c", "$HOME/../usr/local/bin/claude-owner-api"],
    ["sh", "-c", "cd / && usr/local/bin/claude-owner-api"],
    ["x", "claude-owner-api,"], ["x", "a:claude-owner-api:b"],
    ["sh", "-c", "\\claude-owner-api -p x"]])
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


# ── M-A: reanchor cannot seal a chain its anchor does not vouch for ──────────────
ALL_TRIGGERS = (*aam._CHAIN_TRIGGERS, *aam._PROOF_TRIGGERS)


def _three_rows_then_truncate(wt):  # noqa: F811
    proofs = [_sign(wt) for _ in range(3)]
    for p in proofs:
        assert _commit(wt, p) == ""
    _sql(*[f"DROP TRIGGER {t}" for t in ALL_TRIGGERS],
         "DELETE FROM agent_auth_api_chain WHERE seq=3",
         "DELETE FROM owner_proof_used WHERE nonce='{}'".format(proofs[2]["nonce"]))


def test_ma_reanchor_refuses_a_truncated_chain(wt, api_cfg):  # noqa: F811
    _three_rows_then_truncate(wt)
    r = aam.reanchor()
    assert r == {"ok": False, "reason": "api_chain_needs_owner_review", "rows": 2,
                 "anchored_rows_before": 3}
    assert not set(ALL_TRIGGERS) & _triggers()
    assert _state() == "api_ledger_tampered"


def test_ma_a_rewritten_anchor_does_not_vouch(wt, api_cfg):  # noqa: F811
    _three_rows_then_truncate(wt)
    path = aam._anchor_path()
    data = json.load(open(path))
    data["rows"] = 2
    json.dump(data, open(path, "w"))
    assert aam.reanchor()["reason"] == "api_chain_needs_owner_review"


def test_ma_a_deleted_anchor_does_not_vouch(wt, api_cfg):  # noqa: F811
    _three_rows_then_truncate(wt)
    os.unlink(aam._anchor_path())
    assert aam.reanchor()["reason"] == "api_chain_needs_owner_review"


def test_ma_the_owner_can_accept_and_it_is_audited(wt, api_cfg, tmp_path, monkeypatch):  # noqa: F811
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(log))
    _three_rows_then_truncate(wt)
    r = aam.reanchor(accept_chain=True)
    assert r["ok"] and r["rows"] == 2 and r["anchored_rows_before"] == 3
    assert r["anchor_vouched"] is False and r["restored_triggers"] == sorted(ALL_TRIGGERS)
    assert _state() is None
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["action"] == "api_chain_reanchor" and entry["anchored_rows_before"] == 3


def test_ma_the_tool_passes_the_flag(wt, api_cfg, capsys):  # noqa: F811
    import importlib.util
    spec = importlib.util.spec_from_file_location("owner_proof", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "owner_proof.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _three_rows_then_truncate(wt)
    assert mod.main(["reanchor"]) == 1
    assert "api_chain_needs_owner_review" in capsys.readouterr().out
    assert mod.main(["reanchor", "--accept-chain"]) == 0


# ── L-A and the lag read: an anchor is never written or trusted blindly ──────────
def test_la_no_key_writes_no_anchor(wt, api_cfg, monkeypatch):  # noqa: F811
    proof = _sign(wt)
    _fail_anchor_write_once(monkeypatch, after=1)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    before = open(aam._anchor_path()).read()
    with monkeypatch.context() as m:
        m.setattr(ac, "_owner_key", lambda: None)
        assert _commit(wt, None) != ""
    assert open(aam._anchor_path()).read() == before
    assert _state() is None


def test_la_an_unreadable_anchor_does_not_raise_in_the_lag_check(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    with open(aam._anchor_path(), "w") as fh:
        fh.write('{"rows": "x"}')
    c = sqlite3.connect(ac._db_path())
    assert aam._close_anchor_lag(c) == "api_ledger_tampered"
    c.close()


def test_lb_a_long_wrapper_chain_stays_linear():
    import time

    from core.fullcycle import hosts
    start = time.monotonic()
    assert hosts._runs_claude(["env"] * 200_000) is False
    assert hosts._runs_claude(["sh", "-c", "exec " * 200_000]) is False
    assert time.monotonic() - start < 10


# ── M-B: no anchor file under a row is always deletion ───────────────────────────
def _rebuild_without_last_row(proof):
    """The audit's clean attack: tables rebuilt with triggers intact, anchor unlinked."""
    c = sqlite3.connect(ac._db_path())
    chain = c.execute("SELECT sql FROM sqlite_master WHERE name='agent_auth_api_chain'").fetchone()[0]
    triggers = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")]
    rows = c.execute("SELECT * FROM agent_auth_api_chain ORDER BY seq").fetchall()[:-1]
    used = [r for r in c.execute("SELECT * FROM owner_proof_used") if r[0] != proof["nonce"]]
    c.executescript("DROP TABLE agent_auth_api_chain; DROP TABLE owner_proof_used;")
    c.execute(chain)
    c.execute(aam._PROOF_TABLE)
    c.executemany(f"INSERT INTO agent_auth_api_chain VALUES ({','.join('?' * 10)})", rows)
    c.executemany("INSERT INTO owner_proof_used VALUES (?,?,?,?)", used)
    for t in triggers:
        c.execute(t)
    c.commit()
    c.close()
    os.unlink(aam._anchor_path())


def test_mb_cutting_two_rows_to_one_with_the_anchor_gone_is_caught(wt, api_cfg):  # noqa: F811
    first, second = _sign(wt), _sign(wt)
    assert _commit(wt, first) == "" and _commit(wt, second) == ""
    _rebuild_without_last_row(second)
    assert _state() == "api_ledger_tampered"
    assert _commit(wt, second) == "api_ledger_tampered"
    assert aam.reanchor()["reason"] == "api_chain_needs_owner_review"


def test_mb_a_one_row_chain_with_no_anchor_needs_the_owner(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    os.unlink(aam._anchor_path())
    assert _state() == "api_ledger_tampered"
    r = aam.reanchor()
    assert r == {"ok": False, "reason": "api_chain_needs_owner_review", "rows": 1,
                 "anchored_rows_before": 0}
    assert aam.reanchor(accept_chain=True)["ok"] and _state() is None


def test_mb_the_anchor_exists_before_the_first_row(wt, api_cfg):  # noqa: F811
    assert _commit(wt, {"nonce": "ab" * 16}) != ""
    assert _anchored_rows() == 0 and _state() is None


def test_mb_no_first_row_without_its_anchor(wt, api_cfg, monkeypatch):  # noqa: F811
    proof = _sign(wt)
    _fail_anchor_write_once(monkeypatch)
    assert _commit(wt, proof) == "api_anchor_write_failed"
    assert sqlite3.connect(ac._db_path()).execute(
        "SELECT COUNT(*) FROM agent_auth_api_chain").fetchone()[0] == 0
    assert _commit(wt, proof) == ""


@pytest.mark.parametrize("data", [{"rows": 0, "head": "x"}, {"rows": 0, "head": ""}])
def test_mb_a_rows_zero_anchor_is_keyed_too(wt, api_cfg, data):  # noqa: F811
    assert _commit(wt, _sign(wt)) == "" and _commit(wt, _sign(wt)) == ""
    json.dump({**data, "mac": "0" * 64}, open(aam._anchor_path(), "w"))
    assert _state() == "api_ledger_tampered"


# ── L-E: a failed anchor write in reanchor is reported and audited ───────────────
def test_le_reanchor_reports_a_failed_write(wt, api_cfg, tmp_path, monkeypatch):  # noqa: F811
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(log))
    aam._chain(aam._ledger())
    _sql("DROP TRIGGER owner_proof_used_no_delete")

    def fail(rows, head):
        raise OSError("disk full")
    monkeypatch.setattr(aam, "_write_anchor", fail)
    r = aam.reanchor()
    assert r["ok"] is False and r["reason"] == "api_anchor_write_failed"
    assert r["restored_triggers"] == ["owner_proof_used_no_delete"]
    assert json.loads(log.read_text())["reason"] == "api_anchor_write_failed"
