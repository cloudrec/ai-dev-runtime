"""Delivery attribution — who sent a `deliveries` row (2026-08-04).

The 2026-08-03T22:29–22:37Z rows looked "unattributed" because `deliveries` stored
only WHAT was delivered: idempotency_key, target, action, result, timestamps. The API
knew the authenticated principal and the client address and discarded both before the
write, so attribution needed the access log, the docker network and the caller's source
correlated by hand (reports/ACTUATOR_BLIND_PANE_AND_DELIVERY_ATTRIBUTION_2026-08-04.md).

Attribution is stored in a SIDECAR table, not as columns on `deliveries`: the older
build (which the live service still runs, by owner decision) writes `deliveries` with a
POSITIONAL `INSERT ... VALUES (?,?,?,?,?,?)`, so added columns would break every
delivery for it and for any rollback. These tests pin that both directions keep working,
the recording path, and the API computing the identity. Attribution is OBSERVABILITY
ONLY — no safety gate reads it, `actor` is partly self-declared, and failing to compute
or store it must never break a delivery.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import time

import pytest

from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


def _legacy_db(path):
    """A DB exactly as an older build left it: six columns, no attribution."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE deliveries (
        idempotency_key TEXT PRIMARY KEY, target TEXT, action TEXT,
        result TEXT, created_at TEXT, created_ts REAL)""")
    # NOTE: use a CURRENT timestamp. A hardcoded epoch ages past the idempotency TTL and
    # the row is then legitimately pruned, which made this test fail with the clock rather
    # than with a code change.
    conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)",
                 ("legacy-key", "proj:0.0", "agent_send",
                  json.dumps({"delivered": True}), "2026-08-03T22:29:17+00:00",
                  time.time()))
    conn.commit()
    conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()


def _cols(path):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(deliveries)")]
    finally:
        conn.close()


def _row(path, key):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM deliveries WHERE idempotency_key=?", (key,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ═════════════ 1. migration is additive and reversible ══════════════════════
def test_migration_adds_the_sidecar_and_leaves_deliveries_untouched(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    before = _cols(db)
    ac._db().close()
    assert _cols(db) == before                       # deliveries schema UNCHANGED
    assert "delivery_attribution" in _tables(db)


def test_old_positional_insert_still_works_after_migration(tmp_path, monkeypatch):
    """THE rollback pin: the running build writes deliveries with a positional
    6-value INSERT. Adding columns would make it fail with 'table deliveries has 8
    columns but 6 values were supplied'. It must keep working after the migration."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._db().close()                                  # migrate
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)",
                 ("old-build-key", "proj:0.0", "agent_send", json.dumps({"delivered": True}),
                  "2026-08-04T00:00:00+00:00", 1785800000.0))
    conn.commit()
    conn.close()
    assert _row(db, "old-build-key")["target"] == "proj:0.0"


def test_legacy_rows_survive_the_migration(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._db().close()
    row = _row(db, "legacy-key")
    assert row["target"] == "proj:0.0" and json.loads(row["result"])["delivered"] is True
    assert ac.delivery_attribution("legacy-key") is None       # unknown, not invented


def test_legacy_row_is_still_idempotency_visible(tmp_path, monkeypatch):
    """The dedupe path must keep seeing pre-migration rows — otherwise migrating would
    silently re-deliver every message whose key predates it."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    assert ac._seen_delivery("legacy-key") == {"delivered": True}


def test_migration_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    for _ in range(3):
        ac._db().close()
    assert _tables(db).count("delivery_attribution") == 1
    assert _cols(db) == ["idempotency_key", "target", "action", "result",
                         "created_at", "created_ts"]


# ═════════════ 2. the recording path stores who and from where ══════════════
def test_record_delivery_persists_actor_and_source(tmp_path, monkeypatch):
    db = tmp_path / "ac.db"
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._record_delivery("k1", "proj:0.0", "agent_send", {"delivered": True},
                        actor="api:hmac/chatgpt-mcp", source="172.20.0.2:59342")
    att = ac.delivery_attribution("k1")
    assert att["actor"] == "api:hmac/chatgpt-mcp"
    assert att["source"] == "172.20.0.2:59342"
    assert _row(db, "k1")["target"] == "proj:0.0"        # the delivery row is intact


def test_the_investigated_row_shape_is_now_answerable(tmp_path, monkeypatch):
    """The 2026-08-03 question — 'who sent owneros-cancel-wrong-deploy-selection?' —
    is a single lookup instead of a three-system correlation."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    ac._record_delivery("owneros-cancel-wrong-deploy-selection-20260804-0130",
                        "owneros-direct-fix:0.0", "agent_send", {"delivered": True},
                        actor="api:hmac/chatgpt-mcp", source="172.20.0.2:59342 ua=python-httpx")
    att = ac.delivery_attribution("owneros-cancel-wrong-deploy-selection-20260804-0130")
    assert att["actor"] == "api:hmac/chatgpt-mcp" and att["source"].startswith("172.20.0.2")


def test_attribution_failure_never_fails_the_delivery(tmp_path, monkeypatch):
    """Observability must not be able to break the owner's command channel."""
    db = tmp_path / "ac.db"
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._db().close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE delivery_attribution")
    conn.execute("CREATE TABLE delivery_attribution (wrong_shape TEXT NOT NULL)")
    conn.commit()
    conn.close()
    real_db = ac._db

    def _no_migrate():
        c = sqlite3.connect(db, timeout=10)
        return c
    monkeypatch.setattr(ac, "_db", _no_migrate)
    ac._record_delivery("k-fail", "proj:0.0", "agent_send", {"delivered": True},
                        actor="api:hmac", source="1.2.3.4")
    monkeypatch.setattr(ac, "_db", real_db)
    assert _row(db, "k-fail")["target"] == "proj:0.0"    # delivery recorded anyway


def test_internal_caller_without_a_principal_records_nothing(tmp_path, monkeypatch):
    """An in-process caller has no external principal — record no attribution at all,
    never a fabricated identity."""
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    ac._record_delivery("k2", "proj:0.0", "agent_send", {"delivered": True})
    assert ac.delivery_attribution("k2") is None


def test_record_delivery_survives_a_future_column(tmp_path, monkeypatch):
    """New code writes deliveries with NAMED columns, so a future column cannot break
    the write the way a positional INSERT would."""
    db = tmp_path / "ac.db"
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._db().close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE deliveries ADD COLUMN future_col TEXT")
    conn.commit()
    conn.close()
    ac._record_delivery("k3", "proj:0.0", "agent_send", {"delivered": True},
                        actor="api:bearer", source="10.0.0.1:1")
    assert ac.delivery_attribution("k3")["actor"] == "api:bearer"


def test_attribution_is_pruned_on_the_same_retention_as_deliveries(tmp_path, monkeypatch):
    db = tmp_path / "ac.db"
    monkeypatch.setenv("AGENT_CONTROL_DB", str(db))
    ac._record_delivery("old", "proj:0.0", "agent_send", {"delivered": True},
                        actor="api:hmac", source="1.2.3.4")
    conn = sqlite3.connect(db)
    stale = 0.0
    conn.execute("UPDATE deliveries SET created_ts=?", (stale,))
    conn.execute("UPDATE delivery_attribution SET recorded_ts=?", (stale,))
    conn.commit()
    conn.close()
    ac._seen_delivery("anything")                      # triggers the TTL sweep
    assert ac.delivery_attribution("old") is None


def test_agent_send_threads_attribution_to_the_record(monkeypatch):
    captured = {}
    real_deliver = ac._deliver           # keep the real signature to bind against
    monkeypatch.setattr(ac, "validate_target", lambda t: None)
    monkeypatch.setattr(ac, "_deliver",
                        lambda *a, **k: captured.update(args=a, kw=k) or {"ok": True})

    def _delivered():
        """Resolve the recorded call to _deliver by PARAMETER NAME.

        This used to assert `captured["kw"] == {...}` — an exact match on the
        keyword dict, which silently pinned HOW the caller passes its arguments
        rather than WHAT it threads through. Commit 2356691 (`a queued message is
        not a delivered one`) switched agent_send to an all-keyword call while
        still passing actor/source correctly, and the test failed on the call
        style alone. Binding to the signature checks the contract the test is
        named for, and works for positional and keyword callers alike.
        """
        bound = inspect.signature(real_deliver).bind(*captured["args"], **captured["kw"])
        bound.apply_defaults()
        return bound.arguments

    ac.agent_send("proj:0.0", "hello", "key-1", actor="api:hmac/x", source="1.2.3.4:5")
    sent = _delivered()
    assert (sent["actor"], sent["source"]) == ("api:hmac/x", "1.2.3.4:5")
    # the payload itself must still arrive intact alongside the attribution
    assert (sent["target"], sent["text"], sent["action"], sent["idempotency_key"]) == \
           ("proj:0.0", "hello", "agent_send", "key-1")

    ac.agent_answer("proj:0.0", "yes", "key-2", actor="api:bearer", source="1.2.3.4:6")
    answered = _delivered()
    assert (answered["actor"], answered["source"]) == ("api:bearer", "1.2.3.4:6")
    assert (answered["target"], answered["text"], answered["action"],
            answered["idempotency_key"]) == ("proj:0.0", "yes", "agent_answer", "key-2")


# ═════════════ 3. the API computes the identity ═════════════════════════════
class _FakeClient:
    host = "172.20.0.2"
    port = 59342


class _FakeReq:
    def __init__(self, method="hmac", ua="python-httpx/0.27", client=_FakeClient()):
        class _S:
            pass
        self.state = _S()
        if method is not None:
            self.state.auth_method = method
        self.client = client
        self.headers = {"user-agent": ua} if ua else {}


def test_caller_identity_reports_method_address_and_declared_name():
    from api import v1
    actor, source = v1.caller_identity(_FakeReq(), "chatgpt-mcp")
    assert actor == "api:hmac/chatgpt-mcp"
    assert source.startswith("172.20.0.2:59342") and "ua=python-httpx/0.27" in source


def test_caller_identity_without_a_declared_name():
    from api import v1
    actor, _ = v1.caller_identity(_FakeReq(method="bearer"), None)
    assert actor == "api:bearer"


def test_declared_actor_is_sanitised_and_bounded():
    """Self-declared and therefore untrusted: it must not inject newlines, quotes or
    unbounded text into the audit record."""
    from api import v1
    actor, _ = v1.caller_identity(_FakeReq(), "evil\n'; DROP TABLE deliveries; --" + "x" * 200)
    assert "\n" not in actor and "'" not in actor and ";" not in actor
    assert len(actor) <= 120
    assert actor.startswith("api:hmac/")


def test_caller_identity_never_raises_on_a_broken_request():
    """Attribution is observability: it must degrade, never break a delivery."""
    from api import v1
    actor, source = v1.caller_identity(None, None)
    assert actor == "api:unknown" and source == "unknown"

    class _Boom:
        state = property(lambda self: (_ for _ in ()).throw(RuntimeError("no state")))

        @property
        def client(self):
            raise RuntimeError("no client")
        headers = {}

    actor, source = v1.caller_identity(_Boom(), None)
    assert actor == "api:unknown" and source == "unknown"


def test_auth_records_the_method_it_accepted():
    """`caller_identity` reads what `_auth` proved; the method must actually be set."""
    import asyncio
    import hashlib
    import hmac as _hmac
    import time
    from api import v1

    req = _FakeReq(method=None)
    token = "t0ken"
    old = v1._TOKEN
    v1._TOKEN = token
    try:
        ts = str(int(time.time()))
        sig = _hmac.new(token.encode(), ts.encode(), hashlib.sha256).hexdigest()
        assert asyncio.run(v1._auth(req, None, ts, sig)) is True
        assert req.state.auth_method == "hmac"

        req2 = _FakeReq(method=None)
        assert asyncio.run(v1._auth(req2, f"Bearer {token}", None, None)) is True
        assert req2.state.auth_method == "bearer"
    finally:
        v1._TOKEN = old
