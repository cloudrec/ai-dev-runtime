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


# ── combined summary: stale counters do not flag a healthy system ────────────
def test_observability_summary_all_clear_with_only_historical(tmp_path, monkeypatch):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 8 * 86400)])
    monkeypatch.setenv("RUNTIME_JOBS_DB", p)
    _dead_letter(NOW - 7200)
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] == 0 and s["all_clear"] is True and s["status"] == "green"
    assert s["historical_failures_total"] == 2   # 1 job + 1 notification, both historical


def test_observability_summary_red_when_active(tmp_path, monkeypatch):
    p = str(tmp_path / "jobs.db")
    _jobs_db(p, [("failed", NOW - 100)])
    monkeypatch.setenv("RUNTIME_JOBS_DB", p)
    s = diag.observability_summary(now=NOW)
    assert s["active_failures_total"] >= 1 and s["all_clear"] is False and s["status"] == "red"
