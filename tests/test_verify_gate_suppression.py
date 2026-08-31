"""The live-proof checker must distinguish suppression from the dedup mask."""
from __future__ import annotations

import sqlite3
import sys

sys.path.insert(0, "/root/ai-dev-runtime")
from tools import verify_gate_suppression as vg  # noqa: E402

TTL = vg.GATE_TTL_SECS


def _db(tmp_path, rows):
    p = tmp_path / "cp.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, ts TEXT, ts_epoch REAL, "
              "type TEXT, agent_id TEXT, severity TEXT, owner_action_required INTEGER)")
    c.executemany("INSERT INTO event (id,ts,ts_epoch,type,agent_id,severity,"
                  "owner_action_required) VALUES (?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return str(p)


def test_no_gate_events_is_not_a_claim(tmp_path):
    assert vg.check(_db(tmp_path, []), now=1000.0)["status"] == "no_gate_events"


def test_an_event_inside_its_own_window_proves_nothing(tmp_path):
    """The exact production situation: silence is consistent with fix AND mask."""
    db = _db(tmp_path, [(1, "t", 1000.0, "agent_continuation_exhausted", "a:0", "high", 1)])
    r = vg.check(db, now=1000.0 + TTL / 2)
    assert r["status"] == "masked" and r["exit"] == 2
    assert r["minutes_left"] > 0


def test_a_post_expiry_info_event_confirms(tmp_path):
    db = _db(tmp_path, [
        (1, "t", 1000.0, "agent_continuation_exhausted", "a:0", "high", 1),
        (2, "t", 1000.0 + TTL + 60, "agent_continuation_exhausted", "a:0", "info", 0)])
    r = vg.check(db, now=1000.0 + TTL + 120)
    assert r["status"] == "confirmed" and r["exit"] == 0


def test_a_post_expiry_owner_facing_event_contradicts(tmp_path):
    """A wake after the window means the suppression did NOT hold — must not read green."""
    db = _db(tmp_path, [
        (1, "t", 1000.0, "agent_continuation_exhausted", "a:0", "high", 1),
        (2, "t", 1000.0 + TTL + 60, "agent_continuation_exhausted", "a:0", "high", 1)])
    r = vg.check(db, now=1000.0 + TTL + 120)
    assert r["status"] == "contradicted" and r["exit"] == 3


def test_expiry_is_per_target_not_global(tmp_path):
    """B's fresh event must not count as evidence just because A's window expired."""
    db = _db(tmp_path, [
        (1, "t", 1000.0, "agent_continuation_exhausted", "a:0", "high", 1),
        (2, "t", 1000.0 + TTL - 60, "agent_continuation_exhausted", "b:0", "high", 1)])
    r = vg.check(db, now=1000.0 + TTL + 30)
    assert r["status"] == "masked", "b:0 is still inside its own window"
