"""F3: an API-credit launch needs a verified owner proof, and its caps survive tampering.

All mocked: tmux is the fake from test_agent_creation_policy, the launcher is a dummy file
that is never run, the owner key is a test key in tmp_path. No API call, no money.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from core import agent_auth_mode as aam
from core import agent_control as ac
from tests.test_agent_auth_mode import api_cfg, launcher, no_secret_reads  # noqa: F401
from tests.test_agent_creation_policy import KEY, mutations, tmux, wt  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sign(wt, session="gaika-kz", d=None, model="sonnet", risk=None, conv=None, key=KEY,  # noqa: F811
          launcher_path=None, action=aam.API_PROOF_ACTION, **kw):
    lp = launcher_path or str(aam.load_config()["launcher"])
    return ac.sign_owner_proof(key, action, session,
                               aam.api_launch_text(session, d or wt["kz"], model, risk, conv, lp),
                               **kw)


def _start(wt, proof, session="gaika-kz", d=None, actor="api:hmac/chatgpt", **kw):  # noqa: F811
    return ac.agent_resume(d or wt["kz"], session_name=session, actor=actor, auth_mode="api",
                           api_proof=proof, **kw)


def _new_sessions(tmux):  # noqa: F811
    return [c for c in mutations(tmux["calls"]) if c[0] == "new-session"]


def _rows(table):
    c = sqlite3.connect(ac._db_path())
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            return 0                     # no API launch was ever attempted here
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def _sql(*stmts):
    c = sqlite3.connect(ac._db_path())
    try:
        for s in stmts:
            c.execute(s)
        c.commit()
    finally:
        c.close()


def _clear_reservation():
    """The worktree reservation outlives a start briefly; tests of a later start skip it."""
    from core import agent_creation_policy as acp
    with open(acp._state_base() + ".json", "w") as fh:
        fh.write("{}")


# ── unauthorized ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("proof", [
    None, "", "owner", "owner approved this", "api_proof: ok", 1, ["ok"],
    {"owner": True}, {"approved_by": "owner"}])
def test_opt_in_without_a_real_proof_starts_nothing(wt, tmux, api_cfg, proof):  # noqa: F811
    r = _start(wt, proof)
    assert not r["agent_created"]
    assert r["refused"] in ("auth_mode:api_owner_proof_required",
                            "auth_mode:owner_proof_malformed")
    assert mutations(tmux["calls"]) == []


@pytest.mark.parametrize("actor", ["owner", "api:client/owner", "api:hmac/owner"])
def test_an_actor_string_is_not_proof(wt, tmux, api_cfg, actor, monkeypatch):  # noqa: F811
    from core import agent_creation_policy as acp
    monkeypatch.setattr(acp, "authorize", lambda *a, **k: {"ok": True, "reason": "scope",
                                                           "scope": "gaika"})
    r = _start(wt, None, actor=actor)
    assert r["refused"] == "auth_mode:api_owner_proof_required"
    assert _new_sessions(tmux) == []


def test_the_creation_proof_does_not_authorise_spending(wt, tmux, api_cfg):  # noqa: F811
    creation = ac.sign_owner_proof(KEY, "agent_resume", os.path.realpath(wt["kz"]), "gaika-kz")
    r = ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt",
                        auth_mode="api", owner_proof=creation)
    assert r["refused"] == "auth_mode:api_owner_proof_required"
    other = ac.sign_owner_proof(KEY, "agent_resume", os.path.realpath(wt["kz"]), "gaika-kz")
    assert _start(wt, other)["refused"] == "auth_mode:owner_proof_bad_signature"
    assert _new_sessions(tmux) == []


def test_a_proof_signed_with_another_key_is_refused(wt, tmux, api_cfg):  # noqa: F811
    r = _start(wt, _sign(wt, key=b"z" * 64))
    assert r["refused"] == "auth_mode:owner_proof_bad_signature"


def test_no_owner_key_means_no_api_launch(wt, tmux, api_cfg, monkeypatch, tmp_path):  # noqa: F811
    proof = _sign(wt)
    monkeypatch.setenv("AGENT_OWNER_PROOF_KEY_FILE", str(tmp_path / "missing.key"))
    assert _start(wt, proof)["refused"] == "auth_mode:owner_proof_unconfigured"


def test_a_group_readable_owner_key_is_not_a_key(wt, tmux, api_cfg, tmp_path, monkeypatch):  # noqa: F811
    kf = tmp_path / "loose.key"
    kf.write_bytes(KEY)
    kf.chmod(0o640)
    monkeypatch.setenv("AGENT_OWNER_PROOF_KEY_FILE", str(kf))
    assert _start(wt, _sign(wt))["refused"] == "auth_mode:owner_proof_unconfigured"


@pytest.mark.parametrize("field,value", [
    ("model", "opus"), ("risk", "high"), ("conv", "c0ffee-1234")])
def test_a_proof_for_another_launch_shape_is_refused(wt, tmux, api_cfg, field, value):  # noqa: F811
    r = _start(wt, _sign(wt, **{field: value}))
    assert r["refused"] == "auth_mode:owner_proof_bad_signature"
    assert _new_sessions(tmux) == []


def test_a_proof_for_another_session_or_dir_is_refused(wt, tmux, api_cfg):  # noqa: F811
    assert _start(wt, _sign(wt, session="gaika-ru"))["refused"] == \
        "auth_mode:owner_proof_bad_signature"
    assert _start(wt, _sign(wt, d=wt["ru"]))["refused"] == "auth_mode:owner_proof_bad_signature"
    assert _new_sessions(tmux) == []


def test_a_proof_does_not_follow_a_swapped_launcher(wt, tmux, api_cfg, tmp_path):  # noqa: F811
    proof = _sign(wt)
    evil = tmp_path / "evil-launcher"
    evil.write_text("#!/bin/sh\n")
    evil.chmod(0o700)
    api_cfg(launcher=str(evil))
    assert _start(wt, proof)["refused"] == "auth_mode:owner_proof_bad_signature"


def test_an_expired_proof_is_refused(wt, tmux, api_cfg):  # noqa: F811
    proof = _sign(wt, ts=int(time.time()) - ac.OWNER_PROOF_TTL_SECS - 5)
    assert _start(wt, proof)["refused"] == "auth_mode:owner_proof_expired"


def test_enabling_the_yaml_alone_is_not_enough(wt, tmux, api_cfg):  # noqa: F811
    cfg = aam.load_config()
    assert cfg["enabled"] is True and "gaika-kz" in cfg["agents"]
    assert _start(wt, None)["refused"] == "auth_mode:api_owner_proof_required"


# ── the valid path, and replay ────────────────────────────────────────────────────
def test_a_valid_proof_starts_exactly_one_api_session(wt, tmux, api_cfg, no_secret_reads):  # noqa: F811
    r = _start(wt, _sign(wt))
    assert r["agent_created"] and r["auth_mode"] == "api"
    (call,) = _new_sessions(tmux)
    assert "claude-owner-api" in " ".join(call)
    assert _rows("agent_auth_api_chain") == 1 and aam.last_mode("gaika-kz") == "api"


def test_the_opus_shape_works_when_signed_for_and_allowed(wt, tmux, api_cfg):  # noqa: F811
    api_cfg(agents={"gaika-kz": {"project_dir": wt["kz"], "allow_opus_for_high_risk": True}})
    r = _start(wt, _sign(wt, model="opus", risk="high"), model="opus", risk="high")
    assert r["agent_created"] and r["model"] == "opus"


def test_replay_after_the_session_died_is_refused(wt, tmux, api_cfg):  # noqa: F811
    proof = _sign(wt)
    assert _start(wt, proof)["agent_created"]
    _clear_reservation()
    assert _start(wt, proof)["refused"] == "auth_mode:owner_proof_replayed"
    assert len(_new_sessions(tmux)) == 1


def test_replay_is_refused_even_if_the_used_table_is_emptied(wt, tmux, api_cfg):  # noqa: F811
    proof = _sign(wt)
    assert _commit(wt, proof) == ""
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _sql("DELETE FROM owner_proof_used")
    _sql("DROP TRIGGER owner_proof_used_no_delete", "DELETE FROM owner_proof_used")
    assert _commit(wt, proof) == "api_ledger_tampered"
    assert _rows("agent_auth_api_chain") == 1


def _commit(wt, proof, **over):  # noqa: F811
    kw = {"session": "gaika-kz", "project_dir": wt["kz"], "model": "sonnet",
          "launcher": str(aam.load_config()["launcher"]), **over}
    return aam.commit_api_launch(proof=proof, **kw)


@pytest.mark.parametrize("proof,reason", [
    ("owner", "api_owner_proof_required"), (["ok"], "api_owner_proof_required"),
    ({"nonce": "ab" * 16}, "owner_proof_malformed"),
    ({"ts": 1, "nonce": "ab" * 16, "sig": "0" * 64}, "owner_proof_expired")])
def test_commit_refuses_anything_but_a_verified_proof(wt, api_cfg, proof, reason):  # noqa: F811
    assert _commit(wt, proof) == reason
    assert _rows("agent_auth_api_chain") == 0


@pytest.mark.parametrize("over", [{"session": "gaika-ru"}, {"model": "opus"}, {"risk": "high"},
                                  {"conversation_id": "other"}, {"launcher": "/evil"}])
def test_commit_rechecks_the_binding_itself(wt, api_cfg, over):  # noqa: F811
    assert _commit(wt, _sign(wt), **over) == "owner_proof_bad_signature"
    assert _rows("agent_auth_api_chain") == 0


# ── idempotency: a retry changes nothing and spends nothing ───────────────────────
def test_a_retry_while_the_agent_lives_returns_it_and_spends_nothing(wt, tmux, api_cfg,  # noqa: F811
                                                                     monkeypatch):
    proof = _sign(wt)
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: {
        "target": "gaika-kz:0.0", "session": "gaika-kz", "claude_pid": 7, "claude_cwd": d})
    r = _start(wt, proof)
    assert r["resumed"] is False and r["existing_agent"]["session"] == "gaika-kz"
    assert _new_sessions(tmux) == [] and _rows("agent_auth_api_chain") == 0


def test_a_live_session_name_refuses_without_spending_the_proof(wt, tmux, api_cfg,  # noqa: F811
                                                                monkeypatch):
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (0, "", ""))
    monkeypatch.setattr(ac, "_session_liveness", lambda n: {"panes": 1, "dead": 0, "live": 1})
    r = _start(wt, _sign(wt))
    assert r["resumed"] is False and "already exists" in r["reason"]
    assert _rows("agent_auth_api_chain") == 0


def test_a_refused_launch_does_not_spend_the_proof(wt, tmux, api_cfg):  # noqa: F811
    for _ in range(2):
        aam.record_launch(session="old", project_dir="/x", mode="api")
    proof = _sign(wt)
    assert _start(wt, proof)["refused"] == "auth_mode:api_daily_launch_cap_reached"
    _sql("DELETE FROM agent_auth_launch")
    assert _start(wt, proof)["agent_created"]


def test_a_failed_start_still_counts_and_stays_spent(wt, tmux, api_cfg, monkeypatch):  # noqa: F811
    real = ac._tmux
    monkeypatch.setattr(ac, "_tmux", lambda a, stdin=None: (1, "", "no server")
                        if a[0] == "new-session" else real(a))
    proof = _sign(wt)
    with pytest.raises(ac.AgentControlError):
        _start(wt, proof)
    assert _rows("agent_auth_api_chain") == 1
    monkeypatch.setattr(ac, "_tmux", real)
    assert _start(wt, proof)["refused"] == "auth_mode:owner_proof_replayed"


# ── concurrent starts ─────────────────────────────────────────────────────────────
def _race(n, fn):
    barrier, out, errors = threading.Barrier(n), [], []

    def go(i):
        barrier.wait()
        try:
            out.append(fn(i))
        except Exception as e:  # noqa: BLE001 — a crash is a test failure, not a refusal
            errors.append(repr(e))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    return out


def test_concurrent_starts_with_one_proof_create_one_session(wt, tmux, api_cfg):  # noqa: F811
    proof = _sign(wt)
    out = _race(8, lambda i: _start(wt, proof))
    assert sum(1 for r in out if r["agent_created"]) == 1
    assert len(_new_sessions(tmux)) == 1 and _rows("agent_auth_api_chain") == 1


def test_concurrent_starts_cannot_overrun_the_daily_cap(wt, tmux, api_cfg):  # noqa: F811
    api_cfg(limits={"max_live_api_agents": 5, "max_api_launches_per_day": 1},
            agents={"gaika-kz": {"project_dir": wt["kz"]}, "gaika-ru": {"project_dir": wt["ru"]}})
    targets = [("gaika-kz", wt["kz"]), ("gaika-ru", wt["ru"])]
    proofs = [_sign(wt, session=s, d=d) for s, d in targets]
    out = _race(2, lambda i: _start(wt, proofs[i], session=targets[i][0], d=targets[i][1]))
    assert sum(1 for r in out if r["agent_created"]) == 1
    assert len(_new_sessions(tmux)) == 1 and _rows("agent_auth_api_chain") == 1


_SPENDER = """
import json, sys
sys.path.insert(0, {root!r})
from core import agent_auth_mode as aam
print(repr(aam.commit_api_launch(session="gaika-kz", project_dir=sys.argv[2], model="sonnet",
                                 launcher=sys.argv[3], proof=json.loads(sys.argv[1]))))
"""


def test_separate_processes_cannot_both_spend_one_nonce(wt, api_cfg):  # noqa: F811
    env = {**os.environ}
    lp = str(aam.load_config()["launcher"])
    proof = json.dumps(_sign(wt, launcher_path=lp))
    procs = [subprocess.Popen([sys.executable, "-c", _SPENDER.format(root=ROOT), proof, wt["kz"],
                               lp], env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(6)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        results.append(out.strip())
    assert sorted(results) == ["''"] + ["'owner_proof_replayed'"] * 5
    assert _rows("agent_auth_api_chain") == 1


# ── caps survive tampering ────────────────────────────────────────────────────────
def test_deleting_ledger_rows_does_not_reset_the_caps(wt, tmux, api_cfg):  # noqa: F811
    api_cfg(limits={"max_live_api_agents": 5, "max_api_launches_per_day": 1})
    assert _start(wt, _sign(wt))["agent_created"]
    _sql("DELETE FROM agent_auth_launch")
    _clear_reservation()
    assert _start(wt, _sign(wt))["refused"] == "auth_mode:api_daily_launch_cap_reached"
    assert aam._api_usage({"gaika-kz"})["live_api_sessions"] == ["gaika-kz"]


@pytest.mark.parametrize("stmt", ["DELETE FROM agent_auth_api_chain",
                                  "UPDATE agent_auth_api_chain SET ts=0"])
def test_the_chain_refuses_edits(wt, tmux, api_cfg, stmt):  # noqa: F811
    assert _start(wt, _sign(wt))["agent_created"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _sql(stmt)


@pytest.mark.parametrize("attack", [
    ("DROP TRIGGER agent_auth_api_chain_no_delete",),
    ("DROP TRIGGER agent_auth_api_chain_no_update",
     "UPDATE agent_auth_api_chain SET ts=0"),
    ("DROP TABLE agent_auth_api_chain",)])
def test_a_damaged_chain_refuses_every_api_launch(wt, tmux, api_cfg, attack):  # noqa: F811
    api_cfg(limits={"max_live_api_agents": 5, "max_api_launches_per_day": 9})
    assert _start(wt, _sign(wt))["agent_created"]
    _sql(*attack)
    _clear_reservation()
    assert _start(wt, _sign(wt))["refused"] == "auth_mode:api_ledger_tampered"
    assert len(_new_sessions(tmux)) == 1


def test_subscription_launches_never_touch_the_chain_or_need_a_proof(wt, tmux, api_cfg):  # noqa: F811
    r = ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt")
    assert r["agent_created"] and r["auth_mode"] == "subscription"
    c = sqlite3.connect(ac._db_path())
    try:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_auth_api_chain'"
                             ).fetchone()
    finally:
        c.close()


# ── the owner's tools ─────────────────────────────────────────────────────────────
def _load_tool(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"{name}_t",
                                                  os.path.join(ROOT, "tools", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_owner_tool_signs_what_agent_create_accepts(wt, tmux, api_cfg, capsys, tmp_path):  # noqa: F811
    assert _load_tool("owner_proof").main(["sign-api-launch", "--session", "gaika-kz",
                                           "--dir", wt["kz"]]) == 0
    proof_file = tmp_path / "proof.json"
    proof_file.write_text(capsys.readouterr().out)
    assert set(json.loads(proof_file.read_text())) == {"ts", "nonce", "sig"}
    r = ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt",
                        auth_mode="api", api_proof=json.loads(proof_file.read_text()))
    assert r["agent_created"]


def test_agent_create_passes_the_proof_and_reports_a_bad_file(monkeypatch, capsys, tmp_path):
    mod = _load_tool("agent_create")
    seen = {}
    monkeypatch.setattr(ac, "agent_resume", lambda d, **kw: seen.update(kw) or
                        {"agent_created": True})
    good = tmp_path / "p.json"
    good.write_text('{"ts": 1, "nonce": "n", "sig": "s"}')
    assert mod.main(["--dir", "/tmp", "--session", "s", "--auth-mode", "api",
                     "--api-proof-file", str(good)]) == 0
    assert seen["api_proof"] == {"ts": 1, "nonce": "n", "sig": "s"}
    seen.clear()
    good.write_text("not json")
    assert mod.main(["--dir", "/tmp", "--session", "s", "--auth-mode", "api",
                     "--api-proof-file", str(good)]) == 1
    assert seen == {}


# ── R6: the proof ledger and its anchor ───────────────────────────────────────────
DROP_ALL_TRIGGERS = ("DROP TRIGGER agent_auth_api_chain_no_delete",
                     "DROP TRIGGER agent_auth_api_chain_no_update",
                     "DROP TRIGGER owner_proof_used_no_delete",
                     "DROP TRIGGER owner_proof_used_no_update")


def _two_launches(wt):  # noqa: F811
    assert _commit(wt, _sign(wt)) == "" and _commit(wt, _sign(wt)) == ""


def _state():
    c = sqlite3.connect(ac._db_path())
    try:
        return aam._chain_problem(aam._chain(aam._ledger(c)))
    finally:
        c.close()


@pytest.mark.parametrize("stmt", ["DELETE FROM owner_proof_used",
                                  "UPDATE owner_proof_used SET nonce='x'"])
def test_r6_the_used_table_refuses_edits(wt, api_cfg, stmt):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _sql(stmt)


def test_r6_the_audit_attack_is_caught_by_the_anchor(wt, api_cfg):  # noqa: F811
    """DROP TABLE chain + DELETE the spent rows: what the audit showed reset the caps."""
    _two_launches(wt)
    _sql(*DROP_ALL_TRIGGERS, "DROP TABLE agent_auth_api_chain",
         f"DELETE FROM owner_proof_used WHERE action='{aam.API_PROOF_ACTION}'")
    assert _state() == "api_ledger_tampered"
    assert _commit(wt, _sign(wt)) == "api_ledger_tampered"


def test_r6_tail_truncation_is_caught(wt, api_cfg):  # noqa: F811
    _two_launches(wt)
    _sql(*DROP_ALL_TRIGGERS,
         "DELETE FROM agent_auth_api_chain WHERE seq=(SELECT MAX(seq) FROM agent_auth_api_chain)",
         "DELETE FROM owner_proof_used")
    assert _state() == "api_ledger_tampered"


def test_r6_a_repaired_trigger_does_not_hide_the_loss(wt, api_cfg):  # noqa: F811
    _two_launches(wt)
    _sql(*DROP_ALL_TRIGGERS, "DELETE FROM agent_auth_api_chain", "DELETE FROM owner_proof_used")
    c = sqlite3.connect(ac._db_path())
    aam.ensure_proof_table(c)
    c.commit()
    c.close()
    other = ac.sign_owner_proof(KEY, "agent_send", "t:0.0", "x")
    assert ac.verify_owner_proof(other, "agent_send", "t:0.0", "x") == ""
    assert _commit(wt, _sign(wt)) == "api_ledger_tampered"


@pytest.mark.parametrize("damage", [
    lambda d: d.update(rows=1), lambda d: d.update(head="0" * 64),
    lambda d: d.update(mac="0" * 64), lambda d: d.pop("mac")])
def test_r6_a_forged_anchor_refuses(wt, api_cfg, damage):  # noqa: F811
    _two_launches(wt)
    path = aam._anchor_path()
    data = json.load(open(path))
    damage(data)
    json.dump(data, open(path, "w"))
    assert _state() == "api_ledger_tampered"


def test_r6_a_restored_old_anchor_refuses(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    old = open(aam._anchor_path()).read()
    _two_launches(wt)
    open(aam._anchor_path(), "w").write(old)
    assert _state() == "api_ledger_tampered"


def test_r6_a_deleted_anchor_refuses_once_the_chain_has_rows(wt, api_cfg):  # noqa: F811
    _two_launches(wt)
    os.remove(aam._anchor_path())
    assert _state() == "api_ledger_tampered"


def test_r6_a_garbage_anchor_refuses(wt, api_cfg):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    open(aam._anchor_path(), "w").write("not json")
    assert _state() == "api_ledger_tampered"


def test_r6_a_crash_before_the_anchor_write_is_not_tampering(wt, api_cfg, monkeypatch):  # noqa: F811
    assert _commit(wt, _sign(wt)) == ""
    with monkeypatch.context() as m:
        def boom(rows, head):
            raise OSError("disk full")
        m.setattr(aam, "_write_anchor", boom)
        assert _commit(wt, _sign(wt)) == "api_anchor_write_failed"
    assert _rows("agent_auth_api_chain") == 2      # counted, and the caller starts nothing
    assert _state() is None
    assert _commit(wt, _sign(wt)) == ""            # the next launch catches the anchor up
    assert _state() is None and json.load(open(aam._anchor_path()))["rows"] == 3


def test_r6_a_failed_anchor_write_starts_no_session(wt, tmux, api_cfg, monkeypatch):  # noqa: F811
    def boom(rows, head):
        raise OSError("disk full")
    monkeypatch.setattr(aam, "_write_anchor", boom)
    assert _start(wt, _sign(wt))["refused"] == "auth_mode:api_anchor_write_failed"
    assert _new_sessions(tmux) == []


def test_r6_key_rotation_refuses_until_the_owner_reanchors(wt, api_cfg, tmp_path, monkeypatch):  # noqa: F811
    _two_launches(wt)
    kf = tmp_path / "rotated.key"
    kf.write_bytes(b"r" * 64)
    kf.chmod(0o600)
    monkeypatch.setenv("AGENT_OWNER_PROOF_KEY_FILE", str(kf))
    assert _state() == "api_ledger_tampered"
    assert aam.reanchor() == {"ok": True, "rows": 2, "restored_triggers": []}
    assert _state() is None


def test_r6_reanchor_refuses_damaged_rows(wt, api_cfg):  # noqa: F811
    _two_launches(wt)
    _sql(*DROP_ALL_TRIGGERS, "UPDATE agent_auth_api_chain SET ts=0")
    assert aam.reanchor() == {"ok": False, "reason": "api_ledger_tampered"}


def test_r6_concurrent_spends_keep_the_chain_and_anchor_in_step(wt, api_cfg):  # noqa: F811
    lp = str(aam.load_config()["launcher"])
    procs = [subprocess.Popen([sys.executable, "-c", _SPENDER.format(root=ROOT),
                               json.dumps(_sign(wt, launcher_path=lp)), wt["kz"], lp],
                              env={**os.environ}, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(6)]
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0 and out.strip() == "''", err
    assert _rows("agent_auth_api_chain") == 6 and _state() is None
    assert json.load(open(aam._anchor_path()))["rows"] == 6


def test_r6_one_general_proof_is_spent_once_under_race(wt):  # noqa: F811
    proof = ac.sign_owner_proof(KEY, "agent_send", "t:0.0", "x")
    out = _race(8, lambda i: ac.verify_owner_proof(proof, "agent_send", "t:0.0", "x"))
    assert sorted(out) == [""] + ["owner_proof_replayed"] * 7
