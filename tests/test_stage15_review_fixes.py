"""Fixes after the Fable review of e94a32e..2d762c4 (N1-N4) and the audit's open items.

Offline: tmp DBs, fake executables, no network, no live file.
"""
from __future__ import annotations

import pytest

from core.fullcycle import engine as fe


# ── N4: a failing wake bridge leaves nothing open on the shared connection ───────
@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    c, _ = fe.connect()
    c.execute("CREATE TABLE bridge_work (x)")
    c.commit()
    yield c
    c.close()


def _fake_bridge(monkeypatch, conn):
    from core import wake_bridge as wb
    monkeypatch.setattr(wb, "_enabled", lambda: (True, ""))
    monkeypatch.setattr(wb, "is_significant", lambda **k: {"significant": True})
    monkeypatch.setattr(wb, "should_wake", lambda **k: {"decision": "wake"})

    def half_done(d, **k):
        conn.execute("INSERT INTO bridge_work VALUES (1)")
        raise RuntimeError("injected bridge failure")
    monkeypatch.setattr(wb, "record", half_done)


def test_n4_a_bridge_failure_after_dml_does_not_poison_the_next_tx(conn, monkeypatch):
    _fake_bridge(monkeypatch, conn)
    fe._offer_to_wake_bridge(1, "t", "high", False, project_id="p", agent_id="a",
                             correlation_id="c", conn=conn)
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM bridge_work").fetchone()[0] == 0
    with fe._tx(conn):
        fe._audit(conn, action="x", review_id=None, actor="t", in_tx=True)


def test_n4_the_committed_event_survives_the_rollback(conn, monkeypatch):
    from core.control_plane import api as cp
    eid = cp.append_event("t", "fullcycle.test", conn=conn)
    _fake_bridge(monkeypatch, conn)
    fe._offer_to_wake_bridge(eid, "t", "high", False, project_id="p", agent_id="a",
                             correlation_id="c", conn=conn)
    conn.close()
    fresh, _ = fe.connect()
    try:
        assert fresh.execute("SELECT COUNT(*) FROM event WHERE id=?", (eid,)).fetchone()[0] == 1
    finally:
        fresh.close()
