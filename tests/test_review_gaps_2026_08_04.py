"""Gaps found by the local independent review of the attribution + watcher work.

Two real holes survived the M1/M2/attribution rounds, both because a guard trusted a
value it was handed instead of reading the pane or the ledger:

  1. `context_budget.phase` tested for a dialog with `state == "waiting_owner"` ONLY.
     A pane visibly showing a dialog while the caller-supplied state said `idle` was a
     SAFE rotation boundary — and `/clear` on such a pane ANSWERS the dialog. The same
     function called an empty tail safe, so rotation, the most destructive action in the
     system, could fire on a pane `capture-pane` had failed to read.
  2. A duplicate delivery recorded nothing but "not delivered", so a SECOND caller
     replaying another caller's idempotency key left no trace whatsoever — the exact
     blind spot the attribution work was meant to remove.

All tests here FAIL on pre-fix `5647b6d`.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from core import agent_control as ac
from core import context_budget as cb


EN_DIALOG = "Do you want to proceed?\n❯ 1. Yes\n  2. No"
RU_DIALOG = "Точно удалить все данные?\nПродолжить? (да/нет)"
UNSEEN_DIALOG = "Allow this tool to run?\n> approve / deny"      # the M1 shape
CLEAN_TAIL = "❯ ready\nrepo clean"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


# ═══════ 1. rotation must read the pane, not just the state it was handed ════
@pytest.mark.parametrize("tail", [EN_DIALOG, RU_DIALOG, UNSEEN_DIALOG])
def test_rotation_phase_refuses_a_visible_dialog_even_when_state_says_idle(tail):
    """Pre-fix: safe_boundary True → /clear would be pasted onto the dialog."""
    ph = cb.phase("idle", tail, "")
    assert ph["safe_boundary"] is False
    assert "permission_dialog_open" in ph["reasons"], ph


@pytest.mark.parametrize("tail", ["", "   ", "\n\n"])
def test_rotation_phase_refuses_an_unobservable_pane(tail):
    """Pre-fix: safe_boundary True — rotation on a pane nobody could read."""
    ph = cb.phase("idle", tail, "")
    assert ph["safe_boundary"] is False
    assert "unobservable_pane" in ph["reasons"], ph


def test_rotation_phase_still_allows_a_readable_clean_pane():
    """Anti-overcorrection: the whole point of rotation must still work."""
    ph = cb.phase("idle", CLEAN_TAIL, "")
    assert ph["safe_boundary"] is True and ph["reasons"] == []


def test_rotation_phase_keeps_its_existing_refusals():
    assert "state_working" in cb.phase("working", CLEAN_TAIL, "")["reasons"]
    assert "pending_input_line" in cb.phase("idle", CLEAN_TAIL, "queued text")["reasons"]
    assert "permission_dialog_open" in cb.phase("waiting_owner", CLEAN_TAIL, "")["reasons"]


def test_rotate_refuses_and_records_when_the_pane_shows_a_dialog(monkeypatch, tmp_path):
    """End to end: nothing reaches the pane and the refusal is durable."""
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")

    class Ctrl:
        def __init__(self):
            self.delivered = []

        def snapshot(self, target, cwd):
            return {"tail": RU_DIALOG, "pending": "", "conv_mtime": "m0",
                    "state": "idle", "activity": ""}

        def send(self, target, text, idem):
            self.delivered.append(text)
            return {"submitted": True}

        def enter(self, target):
            return 0

        def robust_submit(self, target, text):
            self.delivered.append(text)
            return True

    ctrl = Ctrl()
    out = cb.rotate("cp-canary:0.0", {"root": str(tmp_path)}, state="idle",
                    tail=RU_DIALOG, pending="",
                    measurement={"size_bytes": 5000, "over_hard": True,
                                 "conversation_id": "conv-OLD", "path": ""},
                    ctrl=ctrl, sleep=lambda _: None,
                    conv_meta_fn=lambda *a, **k: {"id": "conv-OLD", "size_bytes": 5000})
    assert out["rotated"] is False and out["reason"] == "unsafe_phase"
    assert "permission_dialog_open" in out["blocking"]
    assert ctrl.delivered == [], "/clear must never reach a pane showing a dialog"
    conn = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    n = conn.execute("SELECT count(*) FROM context_rotation WHERE status="
                     "'refused_unsafe_phase'").fetchone()[0]
    conn.close()
    assert n == 1


# ═══════ 2. a duplicate replayed by another caller must leave a trace ════════
def _delivery_row(key, target="proj:0.0"):
    ac._record_delivery(key, target, "agent_send", {"delivered": True},
                        actor="api:hmac/chatgpt-mcp", source="172.20.0.2:59342")


def test_duplicate_replay_by_a_different_actor_is_audited(tmp_path, monkeypatch):
    """Pre-fix: the replay left NO record of who replayed it."""
    audited = []
    monkeypatch.setattr(ac, "audit", lambda *a, **k: audited.append((a, k)))
    _delivery_row("dup-key")
    ac._note_duplicate_attribution("dup-key", "proj:0.0", "api:bearer/other-client",
                                   "10.9.9.9:1234")
    conflicts = [k for a, k in audited if k.get("replay_actor")]
    assert conflicts, "a replay by a different caller must be audited"
    assert conflicts[0]["original_actor"] == "api:hmac/chatgpt-mcp"
    assert conflicts[0]["replay_actor"] == "api:bearer/other-client"


def test_duplicate_replay_never_overwrites_the_original_attribution():
    """First writer wins: the original caller is the one that reached the pane."""
    _delivery_row("dup-key2")
    ac._note_duplicate_attribution("dup-key2", "proj:0.0", "api:bearer/impostor", "10.9.9.9:1")
    att = ac.delivery_attribution("dup-key2")
    assert att["actor"] == "api:hmac/chatgpt-mcp" and att["source"] == "172.20.0.2:59342"


def test_duplicate_of_an_unattributed_delivery_records_the_replayer():
    """A pre-migration / internal row has no attribution — the replayer is then the
    only identity available, so record it rather than losing it."""
    ac._record_delivery("legacy-dup", "proj:0.0", "agent_send", {"delivered": True})
    assert ac.delivery_attribution("legacy-dup") is None
    ac._note_duplicate_attribution("legacy-dup", "proj:0.0", "api:hmac/late", "1.2.3.4:9")
    assert ac.delivery_attribution("legacy-dup")["actor"] == "api:hmac/late"


def test_same_actor_replaying_its_own_key_is_not_flagged(monkeypatch):
    """A retry by the same caller is normal idempotency, not a conflict."""
    audited = []
    monkeypatch.setattr(ac, "audit", lambda *a, **k: audited.append((a, k)))
    _delivery_row("dup-key3")
    ac._note_duplicate_attribution("dup-key3", "proj:0.0", "api:hmac/chatgpt-mcp",
                                   "172.20.0.2:59342")
    assert not [k for a, k in audited if k.get("replay_actor")]


def test_duplicate_attribution_never_raises(monkeypatch):
    """Observability must not be able to break the duplicate path."""
    monkeypatch.setattr(ac, "delivery_attribution",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("db gone")))
    ac._note_duplicate_attribution("k", "proj:0.0", "api:hmac", "1.2.3.4")   # no raise


def test_deliver_duplicate_path_attributes_without_touching_the_pane(monkeypatch):
    """The full `_deliver` duplicate branch: no tmux call, prior result returned,
    replay attributed."""
    monkeypatch.setattr(ac, "_check_message", lambda *_: None)
    monkeypatch.setattr(ac, "_pane_is_live_agent", lambda t: {"target": t})
    monkeypatch.setattr(ac, "_tmux", lambda *a, **k: pytest.fail("pane must not be touched"))
    _delivery_row("dup-e2e")
    out = ac._deliver("proj:0.0", "hello", "agent_send", "dup-e2e",
                      actor="api:bearer/other", source="10.0.0.9:2")
    assert out["duplicate"] is True and out["delivered"] is False
    assert ac.delivery_attribution("dup-e2e")["actor"] == "api:hmac/chatgpt-mcp"
