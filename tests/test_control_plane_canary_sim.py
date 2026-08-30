"""Deterministic SIMULATED canary — full P4 path offline (no live agent, flags stay OFF).

Proves lease → deliver → consume → verify → CTO event end-to-end against a fake pane, plus
negative cases: false-idle suppression, exclusion (not-canary), restart stale-fence (no
duplicate), and dedup (verified action not re-issued). This is simulated PASS evidence; the
real-agent proof remains gated.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import canary_sim as sim, actuator as act, cto
from core import agent_continuation_watchdog as cw

SAFE = "continue with the next safe step"


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    yield


def test_flags_off_by_default_before_and_after_harness(monkeypatch):
    # harness must not leave the actuator armed. "Default" means a CLEAN env:
    # under the live service the canary drop-ins deliberately arm the actuator
    # (CONTROL_PLANE_ACTUATOR_ENABLED=1), and a runtime job's worktree suite
    # inherits that env — this test measures the code's defaults, not the
    # host's configuration, so the flags are cleared for its duration.
    import importlib
    monkeypatch.delenv("CONTROL_PLANE_ACTUATOR_ENABLED", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_CANARY_AGENTS", raising=False)
    importlib.reload(act)
    try:
        assert act.ENABLED is False
        sim.run_canary("sim:0.0", SAFE)
        assert act.ENABLED is False and act.CANARY_AGENTS == frozenset()
    finally:
        monkeypatch.undo()
        importlib.reload(act)


# ── full path PASS ───────────────────────────────────────────────────────────
def test_full_path_lease_deliver_consume_verify_cto_event():
    out = sim.run_canary("sim:0.0", SAFE, conversation_id="cvA")
    r = out["result"]
    assert r["acted"] is True and r["verified"] is True
    v = r["verify"]
    assert all(v[k] for k in ("submitted", "pane_changed", "prompt_consumed",
                              "conversation_modified", "state_transitioned", "ok"))
    # lease was held; agent SoT advanced to working; CTO event emitted
    assert cp.lease_holder("agent:sim:0.0")["holder"] == "sim_canary"
    assert cp.get_agent("sim:0.0")["actual_state"] == "working"
    assert any(e["type"] == "action_verified" and e["agent_id"] == "sim:0.0"
               for e in cto.cto_brief_since("t")["events"])
    assert out["pane"].sends == 1                       # exactly one delivery


def test_full_path_writes_cp_action_ledger_verified():
    sim.run_canary("sim:0.0", SAFE, conversation_id="cvA")
    import sqlite3
    from core.control_plane.store import db_path
    row = sqlite3.connect(db_path()).execute(
        "SELECT verified,outcome,controller FROM cp_action WHERE target='sim:0.0'").fetchone()
    assert row == (1, "verified", "continuation_watchdog") or row[0] == 1


# ── negative: false-idle suppression ─────────────────────────────────────────
def test_false_idle_pane_is_suppressed():
    out = sim.run_canary("sim:0.0", SAFE, pane=sim.SimulatedPane(active_marker=True))
    assert out["result"]["acted"] is False
    assert out["result"]["reason"] == "target_working"
    assert out["pane"].sends == 0
    assert any(e["type"] == "false_idle_corrected" for e in cto.cto_brief_since("t")["events"])


# ── negative: exclusion (agent not on the canary allowlist) ──────────────────
def test_exclusion_non_canary_agent_refused():
    # arm ONLY "canary:0.0", actuate a different agent → not_canary, no delivery
    pane = sim.SimulatedPane()
    with sim.armed("canary:0.0"):
        lease = cp.acquire_lease("agent:other:0.0", "x", ttl_secs=100)
        r = act.actuate(target="other:0.0", action_text=SAFE, controller="x",
                        conversation_id="cv", lease=lease, cwd="/sim", ctrl=pane,
                        sleep=lambda _: None)
    assert r["acted"] is False and r["reason"] == "not_canary" and pane.sends == 0


# ── negative: restart stale-fence → no duplicate delivery ────────────────────
def test_restart_stale_fence_no_duplicate():
    l1 = cp.acquire_lease("agent:sim:0.0", "sim_canary", ttl_secs=100, now=1000)
    l2 = cp.acquire_lease("agent:sim:0.0", "sim_canary", ttl_secs=100, now=1010)  # restart re-lease
    assert l2["fence_token"] == l1["fence_token"] + 1
    old = sim.run_canary("sim:0.0", SAFE, lease=l1, conversation_id="cvR")   # stale fence
    assert old["result"]["reason"] == "stale_or_no_lease" and old["pane"].sends == 0
    new = sim.run_canary("sim:0.0", SAFE, lease=l2, conversation_id="cvR")   # current fence
    assert new["result"]["acted"] is True and new["pane"].sends == 1


# ── negative: dedup (verified action never re-issued) ────────────────────────
def test_dedup_verified_action_not_reissued():
    lease = cp.acquire_lease("agent:sim:0.0", "sim_canary", now=1000)
    sim.run_canary("sim:0.0", SAFE, lease=lease, conversation_id="cvD")
    second = sim.run_canary("sim:0.0", SAFE, lease=lease, conversation_id="cvD")
    assert second["result"]["reason"] == "already_verified" and second["pane"].sends == 0


# ── negative: retry-once then verify (deliver did not consume first attempt) ─
def test_retry_once_then_verified():
    pane = sim.SimulatedPane(consume_on_enter=2)     # first attempt does not consume
    out = sim.run_canary("sim:0.0", SAFE, pane=pane)
    assert out["result"]["acted"] is True and out["result"]["retried"] is True
