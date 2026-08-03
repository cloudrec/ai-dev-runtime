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


# ── actuation scope integrity (never broadened beyond the canary) ────────────
def _cp_action(target):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO cp_action(idkey,target,verified) VALUES(?,?,1)",
              (f"{target}|k", target))
    c.commit(); c.close()


def test_actuation_scope_green_when_confined_to_canary_and_synthetic():
    _cp_action("cp-canary:0.0")
    _cp_action("canary-synthetic-restart:0.0")   # synthetic test target
    r = diag.actuation_scope_report(allowlist={"cp-canary:0.0"})
    assert r["unexpected_actuated"] == [] and r["status"] == "green"
    assert r["synthetic_test_targets"] == ["canary-synthetic-restart:0.0"]


def test_actuation_scope_breach_when_non_canary_actuated():
    _cp_action("cp-canary:0.0")
    _cp_action("arbitrage2-opus:0.0")            # REAL non-canary agent → breach
    r = diag.actuation_scope_report(allowlist={"cp-canary:0.0"})
    assert r["unexpected_actuated"] == ["arbitrage2-opus:0.0"] and r["status"] == "red"


def test_actuation_scope_reads_allowlist_from_dropin(tmp_path):
    _cp_action("cp-canary:0.0")
    dropin = tmp_path / "canary.conf"
    dropin.write_text("[Service]\nEnvironment=CONTROL_PLANE_CANARY_AGENTS=cp-canary:0.0\n")
    r = diag.actuation_scope_report(dropin_path=str(dropin))
    assert r["canary_allowlist"] == ["cp-canary:0.0"] and r["status"] == "green"


def test_summary_red_on_actuation_scope_breach(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    # empty drop-in path → allowlist empty → any actuated real target is unexpected
    _cp_action("some-real-agent:0.0")
    s = diag.observability_summary(now=NOW)
    assert s["actuation_scope_breach"] is True and s["status"] == "red"


# ── summary red_reasons aggregation ──────────────────────────────────────────
def test_summary_red_reasons_empty_when_all_green(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    s = diag.observability_summary(now=NOW)
    assert s["red_reasons"] == [] and s["status"] == "green" and s["all_clear"] is True


def test_summary_red_reasons_names_the_failing_check(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    _cp_action_fence("cp-canary:0.0", 9); _lease_fence("agent:cp-canary:0.0", 3)  # fence violation
    s = diag.observability_summary(now=NOW)
    assert "consistency_violation" in s["red_reasons"] and s["status"] == "red"


# ── consistency invariants (cursor / notification state / fence) ─────────────
def _cp_action_fence(target, fence):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO cp_action(idkey,target,fence_token,verified) VALUES(?,?,?,1)",
              (f"{target}|{fence}", target, fence))
    c.commit(); c.close()


def _lease_fence(resource, fence):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO resource_lease(resource,holder_controller,fence_token,expires_ts) "
              "VALUES(?,?,?,?)", (resource, "h", fence, NOW + 100))
    c.commit(); c.close()


def test_consistency_all_invariants_hold_green():
    _events(5)
    _cursor("chatgpt", 5, NOW - 10)            # cursor == latest, not ahead
    _notif("dead_letter", NOW - 100)
    _cp_action_fence("cp-canary:0.0", 2)
    _lease_fence("agent:cp-canary:0.0", 3)     # lease fence >= action fence
    r = diag.consistency_report(now=NOW)
    assert r["consistent"] is True and r["status"] == "green"
    assert r["fence_violations"] == [] and r["orphan_actions"] == []


def test_consistency_cursor_ahead_of_log_is_red():
    _events(3)
    _cursor("chatgpt", 99, NOW - 10)           # cursor points past latest event → corrupt
    r = diag.consistency_report(now=NOW)
    assert r["cursors_ahead_of_log"] and r["status"] == "red"


def test_consistency_invalid_notification_state_is_red():
    _events(2)
    _notif("weird_state", NOW - 10)            # unknown state
    r = diag.consistency_report(now=NOW)
    assert "weird_state" in r["invalid_notification_states"] and r["status"] == "red"


def test_consistency_fence_violation_is_red():
    _events(2)
    _cp_action_fence("cp-canary:0.0", 9)       # action fence > current lease fence
    _lease_fence("agent:cp-canary:0.0", 3)
    r = diag.consistency_report(now=NOW)
    assert r["fence_violations"] and r["fence_violations"][0]["target"] == "cp-canary:0.0"
    assert r["status"] == "red"


def test_consistency_orphan_action_is_informational_not_red():
    _events(2)
    _cp_action_fence("orphan:0.0", 1)          # cp_action with NO lease row
    r = diag.consistency_report(now=NOW)
    assert r["orphan_actions"] == ["orphan:0.0"] and r["consistent"] is True and r["status"] == "green"


def test_summary_red_on_consistency_violation(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    _events(2)
    _cp_action_fence("cp-canary:0.0", 9); _lease_fence("agent:cp-canary:0.0", 3)  # fence violation
    s = diag.observability_summary(now=NOW)
    assert s["consistent"] is False and s["status"] == "red"


# ── summary: engine stall makes it RED even with zero active failures ────────
def test_summary_red_when_engine_stalled_no_active_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty_jobs.db"))
    sqlite3.connect(str(tmp_path / "empty_jobs.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("stale:0.0", NOW - 9000, "managed")     # engine looks stalled
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] == 0 and s["engine_alive"] is False
    assert s["all_clear"] is False and s["status"] == "red"


# ── restart consistency: durable in-flight state must survive a restart ───────
def _cp_action_inflight(target, updated_ts, *, submitted=1, verified=0, blocked=0):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO cp_action(idkey,target,fence_token,submitted,verified,"
              "blocked,updated_at) VALUES(?,?,?,?,?,?,?)",
              (f"{target}|{updated_ts}", target, 1, submitted, verified, blocked, _iso(updated_ts)))
    c.commit(); c.close()


def test_restart_clean_state_is_safe():
    _events(3)
    _cursor("chatgpt", 3, NOW - 10)
    r = diag.restart_consistency_report(now=NOW)
    assert r["restart_safe"] is True and r["status"] == "green"
    assert r["orphaned_notifications"] == [] and r["abandoned_inflight_actions"] == []


def test_restart_orphaned_sending_notification_is_red():
    # 'sending' is a valid state but pending_notifications() reclaims only pending/failed,
    # so a restart that crashed mid-deliver leaves it orphaned forever.
    _notif("sending", NOW - 30)
    r = diag.restart_consistency_report(now=NOW)
    assert len(r["orphaned_notifications"]) == 1 and r["orphaned_notifications"][0]["state"] == "sending"
    assert r["restart_safe"] is False and r["status"] == "red"


def test_restart_terminal_notifications_are_safe():
    for st in ("sent", "acked", "dead_letter", "resolved"):
        _notif(st, NOW - 30)
    r = diag.restart_consistency_report(now=NOW)
    assert r["restart_safe"] is True and r["orphaned_notifications"] == []


def test_restart_fresh_reclaimable_notification_is_safe():
    _notif("pending", NOW - 60)      # reclaimable + recent → drain will get it
    _notif("failed", NOW - 60)
    r = diag.restart_consistency_report(now=NOW, stale_secs=900)
    assert r["restart_safe"] is True and r["stale_reclaimable_notifications"] == []


def test_restart_stale_reclaimable_notification_is_red():
    _notif("pending", NOW - 5000)    # reclaimable but not drained in >stale_secs → drain down
    r = diag.restart_consistency_report(now=NOW, stale_secs=900)
    assert len(r["stale_reclaimable_notifications"]) == 1
    assert r["restart_safe"] is False and r["status"] == "red"


def test_restart_abandoned_inflight_action_is_red():
    _cp_action_inflight("cp-canary:0.0", NOW - 5000)   # submitted, never verified/blocked, old
    r = diag.restart_consistency_report(now=NOW, stale_secs=900)
    assert r["abandoned_inflight_actions"] and r["abandoned_inflight_actions"][0]["target"] == "cp-canary:0.0"
    assert r["restart_safe"] is False and r["status"] == "red"


def test_restart_verified_or_blocked_or_fresh_actions_are_safe():
    _cp_action_inflight("a:0.0", NOW - 5000, verified=1)   # completed → not in-flight
    _cp_action_inflight("b:0.0", NOW - 5000, blocked=1)    # blocked → owner-gated, not dangling
    _cp_action_inflight("c:0.0", NOW - 60)                 # in-flight but fresh → still verifying
    r = diag.restart_consistency_report(now=NOW, stale_secs=900)
    assert r["abandoned_inflight_actions"] == [] and r["restart_safe"] is True


def test_restart_cursor_ahead_of_log_is_red():
    _events(3)
    _cursor("chatgpt", 99, NOW - 10)   # cursor past log head → re-deliver/skip after restart
    r = diag.restart_consistency_report(now=NOW)
    assert r["cursors_ahead_of_log"] and r["restart_safe"] is False and r["status"] == "red"


def test_restart_supervisor_heartbeat_fresh_is_alive():
    _loop_markers(sup_ts=NOW - 10)
    r = diag.restart_consistency_report(now=NOW)
    assert r["supervisor_state"] == "alive" and r["restart_safe"] is True


def test_restart_supervisor_heartbeat_stale_is_red():
    _loop_markers(sup_ts=NOW - 5000)   # supervisor didn't resume ticking after restart
    r = diag.restart_consistency_report(now=NOW, supervisor_interval=45)
    assert r["supervisor_state"] == "stalled" and r["restart_safe"] is False and r["status"] == "red"


def test_restart_supervisor_no_marker_is_unknown_not_unsafe():
    r = diag.restart_consistency_report(now=NOW)   # no heartbeat row at all
    assert r["supervisor_state"] == "unknown" and r["restart_safe"] is True


def test_restart_lease_fence_is_monotonic_across_reacquire():
    # simulate a restart re-acquiring the same resource: fence must strictly increase and the
    # pre-restart fence must no longer be current → the restart-no-duplicate guarantee.
    l1 = cp.acquire_lease("agent:cp-canary:0.0", "ctrlA", ttl_secs=100, now=NOW)
    l2 = cp.acquire_lease("agent:cp-canary:0.0", "ctrlB", ttl_secs=100, now=NOW + 200)
    assert l2["fence_token"] == l1["fence_token"] + 1
    assert cp.lease_is_current("agent:cp-canary:0.0", l2["lease_id"], l2["fence_token"]) is True
    assert cp.lease_is_current("agent:cp-canary:0.0", l1["lease_id"], l1["fence_token"]) is False


def test_restart_cursor_and_lease_persist_across_fresh_connection():
    # write via one connection, read via a fresh one (== a process restart on the same DB).
    from core.control_plane import cto
    _events(4)
    cto.set_cursor("chatgpt", 4)
    lease = cp.acquire_lease("agent:cp-canary:0.0", "ctrl", ttl_secs=100, now=NOW)
    # fresh reads
    assert cto.get_cursor("chatgpt") == 4
    held = cp.lease_holder("agent:cp-canary:0.0")
    assert held["lease_id"] == lease["lease_id"] and held["fence_token"] == lease["fence_token"]


def test_summary_includes_restart_safety_and_red_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    _notif("sending", NOW - 30)        # restart-orphaned
    s = diag.observability_summary(now=NOW)
    assert s["restart_safe"] is False and "restart_unsafe" in s["red_reasons"] and s["status"] == "red"


# ── owner-gate SLA escalation (advisory, never flips system to failure) ───────
def test_owner_gate_sla_no_breach_within_window():
    _gate_row("g1", "classify_scope", NOW - 3600)          # 1h old, SLA 24h
    r = diag.owner_gate_report(now=NOW, sla_secs=86400)
    assert r["breached_count"] == 0 and r["escalate"] is False
    assert r["sla_breaches"] == [] and r["status"] == "green"


def test_owner_gate_sla_breach_is_escalation_not_failure():
    _gate_row("g1", "classify_scope", NOW - 100000)        # >24h old → breach
    _gate_row("g2", "unverified_owner_decision", NOW - 200000)  # older breach
    _gate_row("g3", "canary_agent_selection", NOW - 600)   # fresh, no breach
    r = diag.owner_gate_report(now=NOW, sla_secs=86400)
    assert r["breached_count"] == 2 and r["escalate"] is True
    assert [b["id"] for b in r["sla_breaches"]] == ["g2", "g1"]   # oldest breach first
    assert r["status"] == "green"                          # escalation, NOT a system failure
    assert "escalate" in r["note"]


def test_owner_gate_sla_breach_list_is_capped():
    for i in range(25):
        _gate_row(f"gg{i}", "classify_scope", NOW - 100000 - i)
    r = diag.owner_gate_report(now=NOW, sla_secs=86400, breach_limit=20)
    assert r["breached_count"] == 25 and len(r["sla_breaches"]) == 20


def test_summary_gate_sla_breach_is_advisory_not_red(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    _gate_row("g1", "classify_scope", NOW - 200000)        # long-overdue owner gate
    s = diag.observability_summary(now=NOW)
    assert s["owner_gate_sla_breaches"] == 1 and s["owner_gate_escalate"] is True
    assert s["status"] == "green" and s["red_reasons"] == []   # overdue owner action != failure


# ── append-only log growth + retention (read-only, advisory) ─────────────────
def _event_at(ts_epoch, n=1):
    c = _conn()
    for _ in range(n):
        c.execute("INSERT INTO event(ts,ts_epoch,source,type) VALUES('t',?,'x','e')", (ts_epoch,))
    c.commit(); c.close()


def _cp_action_created(target, created_ts):
    c = _conn()
    c.execute("INSERT OR REPLACE INTO cp_action(idkey,target,fence_token,created_at) "
              "VALUES(?,?,1,?)", (f"{target}|{created_ts}", target, _iso(created_ts)))
    c.commit(); c.close()


def _log(r, table):
    return next(l for l in r["logs"] if l["table"] == table)


def test_log_growth_empty_is_green():
    r = diag.log_growth_report(now=NOW)
    assert r["total_rows"] == 0 and r["advise"] is False and r["status"] == "green"
    assert _log(r, "event")["rows"] == 0 and _log(r, "event")["oldest_age_secs"] is None


def test_log_growth_counts_and_age_span():
    _event_at(NOW - 10000)     # oldest
    _event_at(NOW - 100)       # newest
    _event_at(NOW - 5000)
    ev = _log(diag.log_growth_report(now=NOW), "event")
    assert ev["rows"] == 3 and ev["oldest_age_secs"] == 10000 and ev["newest_age_secs"] == 100


def test_log_growth_recent_rate_per_hour():
    _event_at(NOW - 100, n=5)      # 5 within the last hour
    _event_at(NOW - 8000, n=3)     # older than the 1h window
    ev = _log(diag.log_growth_report(now=NOW, rate_window_secs=3600), "event")
    assert ev["rows"] == 8 and ev["recent_rows"] == 5 and ev["rate_per_hr"] == 5.0


def test_log_growth_notification_and_action_use_created_at():
    _notif("sent", NOW - 200)
    _notif("pending", NOW - 100)
    _cp_action_created("cp-canary:0.0", NOW - 300)
    r = diag.log_growth_report(now=NOW, rate_window_secs=3600)
    assert _log(r, "notification")["rows"] == 2 and _log(r, "notification")["recent_rows"] == 2
    assert _log(r, "cp_action")["rows"] == 1 and _log(r, "cp_action")["newest_age_secs"] == 300


def test_log_growth_rows_threshold_advises_not_red():
    _event_at(NOW - 100, n=12)
    r = diag.log_growth_report(now=NOW, advisory_rows=10)   # 12 > 10 → advise
    ev = _log(r, "event")
    assert ev["advise"] is True and "rows>10" in ev["advisory_reasons"]
    assert r["advise"] is True and "event" in r["advise_tables"]
    assert r["status"] == "green"           # retention advisory, NOT a correctness failure


def test_log_growth_rate_threshold_advises():
    _event_at(NOW - 60, n=6)
    r = diag.log_growth_report(now=NOW, rate_window_secs=3600, advisory_rate_per_hr=5)  # 6/hr > 5
    ev = _log(r, "event")
    assert ev["advise"] is True and any("rate>" in x for x in ev["advisory_reasons"])
    assert r["advise"] is True and r["status"] == "green"


def test_summary_log_growth_is_advisory_not_red(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_JOBS_DB", str(tmp_path / "empty.db"))
    sqlite3.connect(str(tmp_path / "empty.db")).execute(
        "CREATE TABLE jobs(id TEXT,status TEXT,created_at TEXT,updated_at TEXT,finished_at TEXT)")
    _agent_row("cp:0.0", NOW - 5, "managed")
    _loop_markers(cw_ts=NOW - 5, orch_ts=NOW - 5, dal_ts=NOW - 5, sup_ts=NOW - 5)
    _event_at(NOW - 100, n=12)
    # force the advisory with a tiny row bound (default 50000 would need too many rows)
    _real = diag.log_growth_report
    monkeypatch.setattr(diag, "log_growth_report",
                        lambda **k: _real(**{**k, "advisory_rows": 10}))
    s = diag.observability_summary(now=NOW)
    assert s["log_retention_advise"] is True and s["log_total_rows"] >= 12
    assert s["status"] == "green" and s["red_reasons"] == []   # growth never flips red
