"""Read-only observability diagnostics: HISTORICAL vs ACTIVE failure classification.

A stale failed-job count or old dead-letter burst must NOT flag a healthy system; an
active (recent) failure must. Regression tests for both metrics + the combined summary.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.control_plane import diagnostics as diag
from core.control_plane import api as cp


NOW = 1_722_000_000.0   # fixed reference epoch


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))   # isolate commander store
    yield


# ── notification dead-letter classification ──────────────────────────────────
def _dead_letter(created_ts):
    conn = cp.connect() if False else None
    from core.control_plane.store import connect, init_db
    c = connect(); init_db(c)
    c.execute("INSERT INTO notification(channel,dedup_key,state,created_at) "
              "VALUES('owner_push',?, 'dead_letter', ?)", (f"k{created_ts}", _iso(created_ts)))
    c.commit(); c.close()


def test_notification_historical_dead_letters_are_green():
    _dead_letter(NOW - 7200)   # 2h old
    _dead_letter(NOW - 9000)
    r = diag.notification_failure_report(now=NOW, active_window_secs=3600)
    assert r["total"] == 2 and r["active"] == 0 and r["historical"] == 2
    assert r["status"] == "green" and r["classification"] == "historical"


def test_notification_recent_dead_letter_is_active_red():
    _dead_letter(NOW - 100)    # within 1h → active
    _dead_letter(NOW - 8000)   # historical
    r = diag.notification_failure_report(now=NOW, active_window_secs=3600)
    assert r["total"] == 2 and r["active"] == 1 and r["status"] == "red"
    assert r["classification"] == "active"


def test_notification_none_is_clean():
    r = diag.notification_failure_report(now=NOW)
    assert r["total"] == 0 and r["status"] == "green" and r["classification"] == "clean"


# ── notification history: current STATE vs cumulative HISTORY ────────────────
def _notif(state, created_ts, attempts=0):
    from core.control_plane.store import connect, init_db
    c = connect(); init_db(c)
    c.execute("INSERT INTO notification(channel,dedup_key,state,attempts,created_at) "
              "VALUES('owner_push',?,?,?,?)", (f"k{state}{created_ts}{attempts}", state, attempts, _iso(created_ts)))
    c.commit(); c.close()


def _event(etype):
    from core.control_plane.store import connect, init_db
    from core.control_plane.api import append_event
    append_event("test", etype)


def test_notification_history_separates_state_from_history():
    _notif("dead_letter", NOW - 7200, attempts=5)   # historical terminal
    _notif("dead_letter", NOW - 8000, attempts=5)
    _notif("sent", NOW - 100, attempts=0)
    _event("notification_dead_letter"); _event("notification_dead_letter")
    _event("notifications_red")
    r = diag.notification_history_report(now=NOW, active_window_secs=3600)
    assert r["current_dead_letter"] == 2 and r["active_dead_letter"] == 0
    assert r["historical_dead_letter"] == 2 and r["status"] == "green"
    assert r["cumulative_failure_attempts"] == 10          # 2 x 5 retries (monotonic history)
    assert r["dead_letter_events_logged"] == 2 and r["notifications_red_events"] == 1
    assert r["current_state"]["sent"] == 1


def test_notification_history_active_when_recent_dead_letter():
    _notif("dead_letter", NOW - 100, attempts=5)     # within window
    r = diag.notification_history_report(now=NOW, active_window_secs=3600)
    assert r["active_dead_letter"] == 1 and r["status"] == "red"


# ── runtime job failure classification ───────────────────────────────────────
def _jobs_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE jobs(id TEXT, status TEXT, created_at TEXT, updated_at TEXT, finished_at TEXT)")
    for i, (status, fin) in enumerate(rows):
        c.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (str(i), status, _iso(fin), _iso(fin), _iso(fin)))
    c.commit(); c.close()


def test_runtime_jobs_historical_failures_are_green(tmp_path):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 8 * 86400), ("failed", NOW - 10 * 86400),
                 ("completed", NOW - 100)])
    r = diag.runtime_job_failure_report(now=NOW, active_window_secs=86400, jobs_db=p)
    assert r["total"] == 2 and r["active"] == 0 and r["status"] == "green"
    assert r["classification"] == "historical" and r["newest_age_secs"] >= 8 * 86400


def test_runtime_jobs_recent_failure_is_active_red(tmp_path):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 3600), ("failed", NOW - 9 * 86400)])   # one recent
    r = diag.runtime_job_failure_report(now=NOW, active_window_secs=86400, jobs_db=p)
    assert r["active"] == 1 and r["status"] == "red" and r["classification"] == "active"


def test_runtime_failed_is_monotonic_series_reconciles_stale_snapshot(tmp_path):
    # a stale external "15" reconciles to the current authoritative total (monotonic terminal).
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 8 * 86400) for _ in range(19)])
    r = diag.runtime_job_failure_report(now=NOW, jobs_db=p)
    assert r["total"] == 19 and r["monotonic_terminal"] is True
    assert "total=19" in r["reconcile_note"] and "active=0" in r["reconcile_note"]


# ── combined summary: stale counters do not flag a healthy system ────────────
def test_observability_summary_all_clear_with_only_historical(tmp_path, monkeypatch):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 8 * 86400)])
    monkeypatch.setenv("RUNTIME_JOBS_DB", p)
    _dead_letter(NOW - 7200)
    _agent_row("a:0.0", NOW - 10, "managed")     # engine alive (recent observation)
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] == 0 and s["all_clear"] is True and s["status"] == "green"
    assert s["historical_failures_total"] == 2   # 1 job + 1 notification, both historical


def test_observability_summary_red_when_active(tmp_path, monkeypatch):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 100)])
    monkeypatch.setenv("RUNTIME_JOBS_DB", p)
    _agent_row("a:0.0", NOW - 10, "managed")     # fresh → engine alive
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] >= 1 and s["all_clear"] is False and s["status"] == "red"


# ── registry freshness / engine liveness (via updated_at, not evidence) ──────
def _conn():
    from core.control_plane.store import connect, init_db
    c = connect(); init_db(c); return c


def _agent_row(target, updated_ts, lifecycle, duplicate_of=None):
    c = _conn()
    c.execute("INSERT INTO agent(id,target,actual_state,lifecycle_state,duplicate_of,updated_at) "
              "VALUES(?,?,?,?,?,?)", (target, target, "unknown", lifecycle, duplicate_of, _iso(updated_ts)))
    c.commit(); c.close()


def _gate_row(gid, kind, opened_ts, agent_id="x:0.0"):
    c = _conn()
    c.execute("INSERT INTO owner_gate(id,kind,agent_id,state,opened_at) VALUES(?,?,?,'open',?)",
              (gid, kind, agent_id, _iso(opened_ts)))
    c.commit(); c.close()


def _lease_row(resource, expires_ts, holder="ctrl"):
    c = _conn()
    c.execute("INSERT INTO resource_lease(resource,holder_controller,fence_token,expires_ts) "
              "VALUES(?,?,1,?)", (resource, holder, expires_ts))
    c.commit(); c.close()


def test_registry_engine_alive_when_recently_observed():
    _agent_row("arb:0.0", NOW - 10, "managed")           # observed 10s ago
    _agent_row("obs:0.0", NOW - 40, "observe_only")
    _agent_row("gone:0.0", NOW - 9000, "dead")           # old
    r = diag.registry_health_report(now=NOW, fresh_within_secs=120)
    assert r["agents_total"] == 3 and r["observed_fresh"] == 2
    assert r["engine_alive"] is True and r["status"] == "green"
    assert r["by_lifecycle"]["managed"] == 1 and r["dead"] == 1


def test_registry_engine_stalled_when_all_observations_old():
    _agent_row("arb:0.0", NOW - 5000, "managed")
    _agent_row("obs:0.0", NOW - 6000, "observe_only")
    r = diag.registry_health_report(now=NOW, fresh_within_secs=120)
    assert r["observed_fresh"] == 0 and r["engine_alive"] is False and r["status"] == "red"
    assert "stalled" in r["note"]


def test_registry_counts_duplicates():
    _agent_row("p:0.0", NOW - 10, "managed")
    _agent_row("p-dup:0.0", NOW - 10, "managed", duplicate_of="p:0.0")
    r = diag.registry_health_report(now=NOW)
    assert r["duplicates_flagged"] == 1


# ── owner-gate aging ─────────────────────────────────────────────────────────
def test_owner_gate_aging_and_kinds():
    _gate_row("g1", "classify_scope", NOW - 3600)
    _gate_row("g2", "classify_scope", NOW - 7200)          # oldest
    _gate_row("g3", "unverified_owner_decision", NOW - 600)
    r = diag.owner_gate_report(now=NOW)
    assert r["open_total"] == 3 and r["by_kind"]["classify_scope"] == 2
    assert r["oldest_gate_id"] == "g2" and r["oldest_age_secs"] == 7200
    assert r["status"] == "green"          # backlog, not a failure


# ── leases live vs expired ───────────────────────────────────────────────────
def test_lease_live_vs_expired():
    _lease_row("agent:a:0.0", NOW + 100)      # live
    _lease_row("agent:b:0.0", NOW - 100)      # expired
    r = diag.lease_report(now=NOW)
    assert r["total"] == 2 and r["live"] == 1 and r["expired_stale"] == 1
    assert r["live_holders"][0]["resource"] == "agent:a:0.0"


# ── CTO cursor lag / staleness ───────────────────────────────────────────────
def _events(n):
    c = _conn()
    for _ in range(n):
        c.execute("INSERT INTO event(ts,ts_epoch,source,type) VALUES('t',0,'x','e')")
    c.commit(); c.close()


def _cursor(consumer, last_id, updated_ts):
    c = _conn()
    c.execute("INSERT INTO cto_cursor(consumer,last_event_id,updated_at) VALUES(?,?,?)",
              (consumer, last_id, _iso(updated_ts)))
    c.commit(); c.close()


def test_cto_cursor_no_consumers_is_informational_green():
    _events(5)
    r = diag.cto_cursor_report(now=NOW)
    assert r["consumer_count"] == 0 and r["status"] == "green" and "no CTO consumer" in r["note"]


def test_cto_cursor_current_consumer_green():
    _events(5)   # latest id = 5
    _cursor("chatgpt", 5, NOW - 10)
    r = diag.cto_cursor_report(now=NOW)
    assert r["consumers"][0]["lag"] == 0 and r["stale_consumers"] == 0 and r["status"] == "green"


def test_cto_cursor_stale_consumer_is_red():
    _events(10)                         # latest id = 10
    _cursor("chatgpt", 3, NOW - 8000)   # 7 unread, cursor not advanced in >1h → stale
    r = diag.cto_cursor_report(now=NOW, stale_after_secs=3600)
    assert r["consumers"][0]["lag"] == 7 and r["stale_consumers"] == 1 and r["status"] == "red"


def test_cto_cursor_lagging_but_recent_is_not_stale():
    _events(10)
    _cursor("chatgpt", 3, NOW - 60)     # behind but advanced recently → not stale
    r = diag.cto_cursor_report(now=NOW, stale_after_secs=3600)
    assert r["consumers"][0]["lag"] == 7 and r["stale_consumers"] == 0 and r["status"] == "green"


# ── commander same-chat delivery drain health ────────────────────────────────
def _commander_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE commander_events(id INTEGER PRIMARY KEY, ts TEXT, acknowledged INTEGER)")
    for i, (ack, ts) in enumerate(rows):
        c.execute("INSERT INTO commander_events VALUES(?,?,?)", (i + 1, _iso(ts), ack))
    c.commit(); c.close()


def test_commander_drain_alive_when_no_backlog(tmp_path):
    p = str(tmp_path / "ac.db")
    _commander_db(p, [(1, NOW - 100), (1, NOW - 50)])   # all acked, recent
    r = diag.commander_delivery_report(now=NOW, ac_db=p)
    assert r["unacked"] == 0 and r["drain_alive"] is True and r["status"] == "green"


def test_commander_drain_stalled_when_unacked_backlog_and_no_recent_ack(tmp_path):
    p = str(tmp_path / "ac.db")
    _commander_db(p, [(1, NOW - 9000), (0, NOW - 7000), (0, NOW - 6000)])  # unacked, old, no recent ack
    r = diag.commander_delivery_report(now=NOW, stall_after_secs=1800, ac_db=p)
    assert r["unacked"] == 2 and r["drain_alive"] is False and r["status"] == "red"
    assert "STALLED" in r["note"]


def test_commander_recent_ack_means_not_stalled_despite_unacked(tmp_path):
    p = str(tmp_path / "ac.db")
    _commander_db(p, [(1, NOW - 60), (0, NOW - 30)])   # unacked but a very recent ack → draining
    r = diag.commander_delivery_report(now=NOW, stall_after_secs=1800, ac_db=p)
    assert r["unacked"] == 1 and r["drain_alive"] is True and r["status"] == "green"


# ── loop liveness (heartbeat stall detection) ────────────────────────────────
import os as _os


def _loop_markers(cw_ts=None, orch_ts=None, dal_ts=None, sup_ts=None):
    ac = _os.environ["AGENT_CONTROL_DB"]
    c = sqlite3.connect(ac)
    c.execute("CREATE TABLE IF NOT EXISTS cw_health(id INTEGER PRIMARY KEY, last_run_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS agent_orchestrator(agent_key TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS direct_agent_lifecycle(target TEXT, updated_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS supervisor_heartbeat(id INTEGER PRIMARY KEY, last_run_at TEXT, ticks INTEGER)")
    if cw_ts is not None:
        c.execute("INSERT OR REPLACE INTO cw_health(id,last_run_at) VALUES(1,?)", (_iso(cw_ts),))
    if orch_ts is not None:
        c.execute("INSERT INTO agent_orchestrator(agent_key,updated_at) VALUES('a',?)", (_iso(orch_ts),))
    if dal_ts is not None:
        c.execute("INSERT INTO direct_agent_lifecycle(target,updated_at) VALUES('a',?)", (_iso(dal_ts),))
    if sup_ts is not None:
        c.execute("INSERT OR REPLACE INTO supervisor_heartbeat(id,last_run_at,ticks) VALUES(1,?,1)", (_iso(sup_ts),))
    c.commit(); c.close()


def test_loop_liveness_all_alive():
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 10, dal_ts=NOW - 10, sup_ts=NOW - 8)
    _agent_row("cp:0.0", NOW - 5, "managed")   # control-plane engine marker
    r = diag.loop_liveness_report(now=NOW)
    by = {l["loop"]: l["state"] for l in r["loops"]}
    assert by["continuation_watchdog"] == "alive" and by["orchestrator"] == "alive"
    assert by["control_plane_engine"] == "alive" and by["supervisor"] == "alive"
    assert r["stalled_loops"] == 0 and r["status"] == "green"


def test_loop_liveness_detects_stalled_supervisor():
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 10, dal_ts=NOW - 10, sup_ts=NOW - 5000)
    _agent_row("cp:0.0", NOW - 5, "managed")
    r = diag.loop_liveness_report(now=NOW)
    by = {l["loop"]: l["state"] for l in r["loops"]}
    assert by["supervisor"] == "stalled" and r["stalled_loops"] == 1 and r["status"] == "red"


def test_supervisor_heartbeat_writes_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "hb.db"))
    from core import agent_supervisor as sup
    sup.heartbeat(); sup.heartbeat()
    c = sqlite3.connect(str(tmp_path / "hb.db"))
    row = c.execute("SELECT last_run_at,ticks FROM supervisor_heartbeat WHERE id=1").fetchone()
    c.close()
    assert row is not None and row[1] == 2 and row[0]      # timestamp + monotonic tick count


def test_loop_liveness_detects_stalled_watchdog():
    _loop_markers(cw_ts=NOW - 5000, orch_ts=NOW - 10, dal_ts=NOW - 10)   # watchdog old
    _agent_row("cp:0.0", NOW - 5, "managed")
    r = diag.loop_liveness_report(now=NOW, stall_multiplier=3.0)
    by = {l["loop"]: l["state"] for l in r["loops"]}
    assert by["continuation_watchdog"] == "stalled" and r["stalled_loops"] == 1 and r["status"] == "red"


def test_loop_liveness_missing_marker_is_unknown_not_red():
    # no marker tables/rows → unknown, not a failure
    r = diag.loop_liveness_report(now=NOW)
    states = {l["state"] for l in r["loops"]}
    assert states <= {"unknown"} and r["stalled_loops"] == 0 and r["status"] == "green"


def test_summary_red_when_a_loop_is_stalled(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")            # engine alive
    _loop_markers(cw_ts=NOW - 5000, orch_ts=NOW - 10)   # watchdog stalled
    s = diag.observability_summary(now=NOW)
    assert s["stalled_loops"] == 1 and s["all_clear"] is False and s["status"] == "red"


# ── summary: engine stall makes it RED even with zero active failures ────────
def test_summary_red_when_engine_stalled_no_active_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty_jobs.db"))
    sqlite3.connect(str(tmp_path / "empty_jobs.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("stale:0.0", NOW - 9000, "managed")     # engine looks stalled
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] == 0 and s["engine_alive"] is False
    assert s["all_clear"] is False and s["status"] == "red"
