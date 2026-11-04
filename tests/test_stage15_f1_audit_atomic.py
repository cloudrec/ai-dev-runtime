"""F1: a review's state change and its audit row are one transaction.

Offline: tmp DBs only (conftest isolation). Faults are injected three ways: `_audit`
raising, a real SQLite trigger refusing the audit INSERT, and a process dying between the
INSERT and the COMMIT.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from core.fullcycle import engine as fe
from tests.test_fullcycle_e2e import fc, h  # noqa: F401
from tests.test_fullcycle_review_fixes import _blocked_pending, _stalled_and_answered, _task

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    conn, _ = fe.connect()
    conn.execute("INSERT INTO fc_review (project_id, task_id, attempt, kind, payload, created_ts)"
                 " VALUES ('demo', 'demo:a', 1, 'review', '{}', 0)")
    conn.commit()
    rid = conn.execute("SELECT id FROM fc_review").fetchone()[0]
    yield conn, rid
    conn.close()


def _review(conn, rid):
    return conn.execute("SELECT status, decision, decision_key, decided_by, acked_ts FROM"
                        " fc_review WHERE id=?", (rid,)).fetchone()


def _audit_rows(conn):
    return conn.execute("SELECT action, outcome, decision_key FROM fc_decision_audit ORDER BY"
                        " seq").fetchall()


def _raise_on(outcome):
    real = fe._audit

    def faulty(conn, **kw):
        if kw.get("outcome") == outcome:
            raise RuntimeError("injected audit failure")
        return real(conn, **kw)
    return faulty


def test_audit_failure_leaves_the_review_pending(db, monkeypatch):
    conn, rid = db
    with monkeypatch.context() as m:
        m.setattr(fe, "_audit", _raise_on("accepted"))
        with pytest.raises(RuntimeError, match="injected"):
            fe.submit_decision(rid, "approve", key="k1", by="api:client/r")
    assert _review(conn, rid) == ("pending", None, None, None, None)
    assert _audit_rows(conn) == []
    # nothing was spent by the failed attempt: the same key still decides, exactly once
    assert fe.submit_decision(rid, "approve", key="k1", by="api:client/r")["ok"] is True
    assert fe.verify_decision_audit()["ok"]


def test_real_sqlite_failure_on_the_audit_insert_rolls_the_decision_back(db):
    conn, rid = db
    conn.execute("CREATE TRIGGER audit_full BEFORE INSERT ON fc_decision_audit BEGIN"
                 " SELECT RAISE(ABORT, 'disk full, say'); END")
    conn.commit()
    with pytest.raises(Exception, match="disk full"):
        fe.submit_decision(rid, "approve", key="k1", by="api:client/r")
    assert _review(conn, rid)[0] == "pending"
    conn.execute("DROP TRIGGER audit_full")
    conn.commit()


def test_process_death_between_insert_and_commit_keeps_neither(db, tmp_path):
    conn, rid = db
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {ROOT!r})
        from core.fullcycle import engine as fe
        real = fe._audit
        def dies(conn, **kw):
            real(conn, **kw)            # row written, transaction still open
            os._exit(9)
        fe._audit = dies
        fe.submit_decision({rid}, "approve", key="k1", by="api:client/r")
    """)
    env = {**os.environ, "CONTROL_PLANE_DB": str(tmp_path / "cp.db")}
    assert subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT).returncode == 9
    assert _review(conn, rid)[0] == "pending"
    assert _audit_rows(conn) == []


def test_ack_and_its_audit_row_are_atomic(db, monkeypatch):
    conn, rid = db
    monkeypatch.setattr(fe, "_audit", _raise_on("acked"))
    with pytest.raises(RuntimeError, match="injected"):
        fe.ack_review(rid, by="api:client/r")
    assert _review(conn, rid)[4] is None and _audit_rows(conn) == []


def test_ack_of_an_unknown_review_writes_nothing(db):
    conn, _ = db
    with pytest.raises(fe.EngineError):
        fe.ack_review(999, by="api:client/r")
    assert _audit_rows(conn) == []


def test_validation_errors_leave_nothing_and_release_the_lock(db):
    conn, rid = db
    with pytest.raises(fe.EngineError):
        fe.submit_decision(rid, "not-a-decision", key="k1", by="api:client/r")
    assert _review(conn, rid)[0] == "pending" and _audit_rows(conn) == []
    assert fe.submit_decision(rid, "approve", key="k2", by="api:client/r")["ok"] is True


def test_refused_outcomes_are_still_audited(db):
    conn, rid = db
    assert fe.submit_decision(rid, "approve", key="k1", by="api:client/r")["ok"] is True
    r = fe.submit_decision(rid, "reject", key="k2", by="api:client/r", feedback="no")
    assert r["ok"] is False and r["reason"] == "review_already_decided"
    assert [x[1] for x in _audit_rows(conn)] == ["accepted", "review_already_decided"]
    assert fe.verify_decision_audit()["ok"]


def test_in_tx_audit_outside_a_transaction_is_a_bug_not_a_silent_write(db):
    conn, _ = db
    with pytest.raises(fe.EngineError):
        fe._audit(conn, action="x", review_id=None, actor="t", in_tx=True)


def test_open_caller_work_is_refused_not_committed_on_its_behalf(db):
    conn, rid = db
    conn.execute("UPDATE fc_review SET payload='uncommitted' WHERE id=?", (rid,))
    with pytest.raises(fe.EngineError, match="already open"):
        fe.submit_decision(rid, "approve", key="k1", by="api:client/r", conn=conn)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT payload, status FROM fc_review WHERE id=?", (rid,)
                        ).fetchone() == ("{}", "pending")


def test_a_key_from_before_atomicity_stays_spent_after_reopen(fc, monkeypatch):  # noqa: F811
    """A decision left without its `accepted` row by the old code: the reopen row names the
    key it clears, so a replay is refused even though no `accepted` row ever existed."""
    e, a, log, root, clock = _stalled_and_answered(fc, monkeypatch)[:5]
    conn = e.conn
    rid = conn.execute("SELECT id FROM fc_review WHERE decision_key='answer-1'").fetchone()[0]
    # recreate the pre-fix state: the accepted row is gone (triggers must go first)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND"
                                " tbl_name='fc_decision_audit'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DELETE FROM fc_decision_audit WHERE decision_key='answer-1'")
    conn.commit()
    e._blocked_review(_task(e), {"question": "late question"})
    assert [r["review_id"] for r in _blocked_pending(e)] == [rid]
    replay = fe.submit_decision(rid, "answer", key="answer-1", by="stub-gpt", feedback="old")
    assert replay["ok"] is False and replay["reason"] == "decision_key_already_used"


def test_reopen_and_its_audit_row_are_atomic(fc, monkeypatch):  # noqa: F811
    e, a, log, root, clock = _stalled_and_answered(fc, monkeypatch)[:5]
    before = e.conn.execute("SELECT status, decision_key FROM fc_review WHERE"
                            " decision_key='answer-1'").fetchone()
    assert before == ("decided", "answer-1")
    real = fe._audit

    def faulty(conn, **kw):
        if kw.get("action") == "review_reopened":
            raise RuntimeError("injected audit failure")
        return real(conn, **kw)
    monkeypatch.setattr(fe, "_audit", faulty)
    with pytest.raises(RuntimeError, match="injected"):
        e._blocked_review(_task(e), {"question": "late"})
    assert e.conn.execute("SELECT status, decision_key FROM fc_review WHERE"
                          " decision_key='answer-1'").fetchone() == before


_WORKER = """
import json, sys
sys.path.insert(0, {root!r})
from core.fullcycle import engine as fe
rid, key = int(sys.argv[1]), sys.argv[2]
print(json.dumps(fe.submit_decision(rid, "approve", key=key, by="api:client/r")))
"""


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_processes_decide_once_and_keep_the_chain(db, tmp_path, same_key):
    import json
    conn, rid = db
    env = {**os.environ, "CONTROL_PLANE_DB": str(tmp_path / "cp.db")}
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER.format(root=ROOT), str(rid),
                               "k" if same_key else f"k{i}"], env=env, cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(8)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        results.append(json.loads(out))
    won = [r for r in results if r["ok"] and not r["duplicate"]]
    assert len(won) == 1
    accepted = [x for x in _audit_rows(conn) if x[1] == "accepted"]
    assert len(accepted) == 1
    assert _review(conn, rid)[0] == "decided"
    assert fe.verify_decision_audit()["ok"] is True
    if not same_key:
        assert sum(1 for x in _audit_rows(conn) if x[1] == "review_already_decided") == 7
    else:
        assert all(r["ok"] for r in results)
