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
