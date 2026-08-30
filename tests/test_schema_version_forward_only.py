"""The recorded schema version must never regress below the schema that exists.

Live 2026-08-06: `control_plane.db` reported `schema_version = 4` while every v5/v6/v7
object was present. Nothing was wrong with the schema. `init_db()` wrote the importing
process' constant whenever it differed from the stored value — in BOTH directions — so a
daemon that had imported an older build (the wake companion, started before the bumps,
polling every 20s) kept rewriting the number backwards, while the restarted runtime wrote
it forward again. The value flipped 7 → 4 → 7 every 20-30 seconds.

That matters beyond cosmetics: a version number is what a future migration reads to decide
what to apply. A number that lies downward invites a migration to re-run against a newer
database.
"""
from __future__ import annotations

import sqlite3

import pytest

from core.control_plane import store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def _version(conn) -> int:
    return conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone()[0]


def test_a_fresh_db_records_the_current_version():
    conn = store.init_db()
    assert _version(conn) == store.SCHEMA_VERSION
    conn.close()


def test_an_older_process_constant_cannot_downgrade_the_record(monkeypatch):
    """THE regression: a stale in-memory constant must not rewrite the number backwards."""
    conn = store.init_db()
    newest = store.SCHEMA_VERSION
    assert _version(conn) == newest
    conn.close()

    monkeypatch.setattr(store, "SCHEMA_VERSION", 4)      # a process from before the bumps
    conn = store.init_db()
    try:
        assert _version(conn) == newest, "init_db downgraded the recorded schema version"
    finally:
        conn.close()


def test_an_older_build_writing_directly_is_ignored_by_the_database():
    """Belt and braces: the guard lives in the DB too, because a long-lived process running
    the OLD code cannot be fixed by editing this module."""
    conn = store.init_db()
    current = _version(conn)
    conn.execute("UPDATE schema_meta SET version=?, updated_at=? WHERE id=1",
                 (current - 3, store.now_iso()))
    conn.commit()
    assert _version(conn) == current, "the database accepted a downgrade write"
    conn.close()


def test_an_upgrade_is_still_applied(monkeypatch):
    conn = store.init_db()
    conn.execute("UPDATE schema_meta SET version=1 WHERE id=1")   # simulate an old DB
    conn.commit()
    conn.close()
    conn = store.init_db()
    try:
        assert _version(conn) == store.SCHEMA_VERSION
    finally:
        conn.close()


def test_init_db_is_idempotent_and_leaves_the_schema_complete():
    for _ in range(3):
        conn = store.init_db()
        conn.close()
    conn = store.connect()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"schema_meta", "event", "channel", "notification", "owner_decision",
                    "cp_action", "policy_decision", "policy_override", "policy_claim",
                    "work_evidence"}
        assert required <= tables, sorted(required - tables)
        channel_cols = {r[1] for r in conn.execute("PRAGMA table_info(channel)")}
        assert {"state", "proof_epoch", "last_proof"} <= channel_cols
        assert _version(conn) == store.SCHEMA_VERSION
    finally:
        conn.close()


def test_the_reported_version_matches_the_objects_that_exist():
    """A version number is only useful if it describes the database it is stored in."""
    assert store.schema_version() == store.SCHEMA_VERSION
    conn = store.connect()
    try:
        assert conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='work_evidence'"
        ).fetchone()[0] == 1                      # the v7 object the number claims
    finally:
        conn.close()


def test_two_processes_with_different_constants_settle_on_the_newer(monkeypatch):
    """Simulates the live flap: alternating writers must converge, not oscillate."""
    conn = store.init_db()
    conn.close()
    newest = store.SCHEMA_VERSION
    for constant in (4, newest, 4, 5, newest, 4):
        monkeypatch.setattr(store, "SCHEMA_VERSION", constant)
        c = store.init_db()
        c.close()
    c = sqlite3.connect(store.db_path())
    try:
        assert _version(c) == newest
    finally:
        c.close()
