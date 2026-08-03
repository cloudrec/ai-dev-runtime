"""Control Plane V2 — zero-manual-registration discovery + CTO inbox/cursor.

Acceptance A/B/E/F: a manually-created agent is discovered + classified + surfaced
with NO config edit; known project → managed, unknown → observe_only + one decision;
duplicates flagged without conflicting commands; CTO consumer reads exact deltas by
cursor with no loss/duplication across a restart.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import discovery as disc
from core.control_plane import cto


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


CONFIG = {
    "allowed_roots": ["/opt", "/root/ai-dev-runtime"],
    "sessions": {
        "arbitrage2-opus": {"mode": "auto", "project": "arbitrage2", "root": "/opt/arbitrage2"},
        "polyinput": {"mode": "hold", "project": "polyinput", "root": "/opt/polyinput"},
    },
}


def _agent(target, cwd, alive=True, pid=1000, session=None):
    return {"target": target, "session": session or target.split(":")[0], "is_agent": True,
            "alive": alive, "claude_cwd": cwd, "pid": pid, "command": "claude"}


def _inv(*agents):
    return {"agents": list(agents)}


def _conv(mapping):
    return lambda cwd: mapping.get(cwd)


# ── classify_scope (pure) ────────────────────────────────────────────────────
def test_classify_managed_observe_unknown():
    assert disc.classify_scope("/opt/arbitrage2", "arbitrage2-opus", CONFIG)["lifecycle"] == disc.MANAGED
    assert disc.classify_scope("/opt/polyinput", "polyinput", CONFIG)["lifecycle"] == disc.OBSERVE_ONLY
    # unknown cwd under an allowed root → observe_only + owner decision, inferred project
    u = disc.classify_scope("/opt/brand-new-thing", "randomsess", CONFIG)
    assert u["lifecycle"] == disc.OBSERVE_ONLY and u["owner_action_required"] is True
    assert u["project_id"] == "brand-new-thing"
    # outside any allowed root → blocked unknown
    b = disc.classify_scope("/home/somewhere", "x", CONFIG)
    assert b["lifecycle"] == disc.BLOCKED_UNKNOWN and b["owner_action_required"] is True


# ── A: manual new agent discovered with NO config edit ───────────────────────
def test_manual_new_agent_discovered_and_managed_without_config_edit():
    inv = _inv(_agent("arbitrage2-opus:0.0", "/opt/arbitrage2"))
    res = disc.discover(inv, config=CONFIG, conversation_fn=_conv({"/opt/arbitrage2": "convA"}))
    assert res["discovered"] == 1 and res["managed"] == 1
    a = cp.get_agent("arbitrage2-opus:0.0")
    assert a["lifecycle_state"] == disc.MANAGED and a["project_id"] == "arbitrage2"
    assert a["first_seen_at"] and a["conversation_id"] == "convA"
    # a new-agent CTO event exists
    brief = cto.cto_brief_since("cto")
    assert any(e["type"] == "new_agent_discovered" for e in brief["events"])


# ── B: unknown project → observe_only + exactly one decision ─────────────────
def test_unknown_project_agent_observe_only_and_one_decision():
    inv = _inv(_agent("mystery:0.0", "/opt/mystery-proj"))
    disc.discover(inv, config=CONFIG, conversation_fn=_conv({"/opt/mystery-proj": "cX"}))
    a = cp.get_agent("mystery:0.0")
    assert a["lifecycle_state"] == disc.OBSERVE_ONLY
    gates = cp.get_open_gates()
    assert len(gates) == 1 and gates[0]["agent_id"] == "mystery:0.0"
    # discovering again does NOT open a second decision (deduped by new-agent event)
    disc.discover(inv, config=CONFIG, conversation_fn=_conv({"/opt/mystery-proj": "cX"}))
    assert len(cp.get_open_gates()) == 1


# ── F: two agents same project → duplicate detected, no conflicting command ──
def test_duplicate_agents_same_cwd_flagged():
    inv = _inv(_agent("arbitrage2-opus:0.0", "/opt/arbitrage2", pid=1),
               _agent("arb-dupe:0.0", "/opt/arbitrage2", pid=2))
    res = disc.discover(inv, config=CONFIG,
                        conversation_fn=_conv({"/opt/arbitrage2": "convA"}))
    assert res["duplicates"] == 1
    reg = {r["target"]: r for r in cp.get_registry()}
    # the non-primary is flagged duplicate_of the primary (lexicographically first)
    assert reg["arb-dupe:0.0"]["duplicate_of"] == "arbitrage2-opus:0.0"
    brief = cto.cto_brief_since("cto")
    assert any(e["type"] == "duplicate_agent_detected" for e in brief["events"])


# ── dead + recovery preserve conversation_id, no duplicate ───────────────────
def test_dead_then_recovered_same_conversation_no_duplicate():
    convmap = {"/opt/arbitrage2": "convR"}
    disc.discover(_inv(_agent("arb:0.0", "/opt/arbitrage2")), config=CONFIG,
                  conversation_fn=_conv(convmap))
    # gone → dead
    disc.discover(_inv(), config=CONFIG, conversation_fn=_conv(convmap))
    assert cp.get_agent("arb:0.0")["lifecycle_state"] == disc.DEAD
    # returns (same target, same conversation) → recovered, still ONE registry row
    r = disc.discover(_inv(_agent("arb:0.0", "/opt/arbitrage2")), config=CONFIG,
                      conversation_fn=_conv(convmap))
    assert r["recovered"] == 1
    assert cp.get_agent("arb:0.0")["lifecycle_state"] == disc.RECOVERED
    assert len([x for x in cp.get_registry() if x["conversation_id"] == "convR"]) == 1


def test_session_rename_reconciles_without_duplicate():
    convmap_old = {"/opt/arbitrage2": "convS"}
    disc.discover(_inv(_agent("old-name:0.0", "/opt/arbitrage2")), config=CONFIG,
                  conversation_fn=_conv(convmap_old))
    # renamed session, SAME conversation + cwd, old target gone from inventory
    disc.discover(_inv(_agent("new-name:0.0", "/opt/arbitrage2")), config=CONFIG,
                  conversation_fn=_conv({"/opt/arbitrage2": "convS"}))
    reg = {r["target"]: r for r in cp.get_registry()}
    assert reg["old-name:0.0"]["lifecycle_state"] == disc.DEAD      # retired
    assert reg["new-name:0.0"]["conversation_id"] == "convS"        # same conversation
    # exactly one LIVE (non-dead) row for this conversation
    live_rows = [r for r in reg.values()
                 if r["conversation_id"] == "convS" and r["lifecycle_state"] != disc.DEAD]
    assert len(live_rows) == 1


# ── E: CTO cursor — exact deltas, ack, restart no loss/duplication ───────────
def test_cto_cursor_deltas_ack_and_restart_no_loss_or_dup(tmp_path, monkeypatch):
    cto.emit("t", "e1", severity="info", payload={"n": 1})
    cto.emit("t", "e2", severity="high", payload={"n": 2})
    b1 = cto.cto_brief_since("chatgpt")
    assert b1["count"] == 2 and [e["payload"]["n"] for e in b1["events"]] == [1, 2]
    # ack through the batch
    cto.ack_through("chatgpt", b1["next_cursor"])
    # new event arrives; a fresh read returns ONLY the delta
    cto.emit("t", "e3", severity="info", payload={"n": 3})
    b2 = cto.cto_brief_since("chatgpt")
    assert b2["count"] == 1 and b2["events"][0]["payload"]["n"] == 3
    # simulate a service restart: cursor persists in the DB → same delta, no loss/dup
    cursor_before = cto.get_cursor("chatgpt")
    b3 = cto.cto_brief_since("chatgpt")
    assert b3["count"] == 1 and b3["events"][0]["event_id"] == b2["events"][0]["event_id"]
    assert cto.get_cursor("chatgpt") == cursor_before        # unchanged until ack


def test_high_severity_and_owner_action_events_enqueue_owner_push():
    r = cto.emit("ctrl", "real_blocker", severity="critical", owner_action_required=True,
                 dedup_key="blk:1")
    assert r["pushed"] is True
    assert [n["dedup_key"] for n in cp.pending_notifications()] == ["blk:1"]
