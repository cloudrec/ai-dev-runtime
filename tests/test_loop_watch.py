"""The loop watches itself: silent stops become one Telegram line, then a recovery line."""
from __future__ import annotations

import json
import subprocess

import pytest

from core import loop_watch as lw
from core import wake_bridge as wb
from core.fullcycle import engine as fe


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))


def _conn():
    conn, _ = fe.connect()
    wb._conn(conn)                                   # the bridge's tables and migrations
    conn.execute(wb._SUBMIT_SCHEMA)                  # made lazily by the bridge
    return conn


def _review(conn, rid, event_id, created, status="pending"):
    conn.execute("INSERT INTO fc_review (id, project_id, task_id, attempt, kind, status, "
                 "event_id, payload, created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                 (rid, "gaika-ua", f"t{rid}", 1, "blocked", status, event_id, "{}", created))
    conn.commit()


def _wake(conn, event_id, ts, decision="wake", acked=0):
    conn.execute("INSERT INTO wake_audit (ts, at, event_id, decision, reason, acknowledged) "
                 "VALUES (?,?,?,?,?,?)", (ts, "", event_id, decision, "", acked))
    conn.commit()


class Run:
    """systemctl / journalctl stand-in."""
    def __init__(self, active=True, journal=""):
        self.active, self.journal, self.calls = active, journal, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = ""
        if argv[:2] == ["systemctl", "is-active"]:
            out = "active\n" if self.active else "inactive\n"
        elif argv[0] == "journalctl":
            out = self.journal
        return subprocess.CompletedProcess(argv, 0, out, "")


NOW = 100_000.0


def test_a_review_without_any_wake_decision_is_reported_once_then_recovers():
    conn, told = _conn(), []
    _review(conn, 1, 501, NOW - lw.UNWOKEN_AFTER - 5)
    r = lw.check(notify=told.append, conn=conn, now=NOW, run=Run())
    assert "unwoken_reviews" in r["problems"] and len(told) == 1 and "gaika-ua" in told[0]
    lw.check(notify=told.append, conn=conn, now=NOW + 60, run=Run())
    assert len(told) == 1                                    # no repeat inside the window
    lw.check(notify=told.append, conn=conn, now=NOW + lw.REMIND_SECS + 1, run=Run())
    assert len(told) == 2 and "всё ещё" in told[1]
    conn.execute("UPDATE fc_review SET status='decided', decision='answer' WHERE id=1")
    conn.commit()
    lw.check(notify=told.append, conn=conn, now=NOW + lw.REMIND_SECS + 60, run=Run())
    assert told[-1].startswith("✅")


def test_a_woken_or_young_review_is_fine():
    conn, told = _conn(), []
    _review(conn, 1, 501, NOW - lw.UNWOKEN_AFTER - 5)
    _wake(conn, 501, NOW - 100, acked=1)
    _review(conn, 2, 502, NOW - 30)
    assert lw.check(notify=told.append, conn=conn, now=NOW, run=Run())["problems"] == {}
    assert told == []


def test_a_decided_wake_stuck_undelivered_is_reported():
    conn, told = _conn(), []
    _wake(conn, 600, NOW - lw.UNDELIVERED_AFTER - 60)
    r = lw.check(notify=told.append, conn=conn, now=NOW, run=Run())
    assert "undelivered_wakes" in r["problems"]
    _wake(conn, 601, NOW - 10)                              # fresh one alone is fine
    conn.execute("UPDATE wake_audit SET acknowledged=1 WHERE event_id=600")
    conn.commit()
    assert "undelivered_wakes" not in lw.check(notify=told.append, conn=conn, now=NOW,
                                               run=Run())["problems"]


def test_a_stopped_worker_is_restarted_at_most_once_per_gap():
    conn, told, run = _conn(), [], Run(active=False)
    lw.check(notify=told.append, conn=conn, now=NOW, run=run)
    restarts = [c for c in run.calls if c[:2] == ["systemctl", "restart"]]
    assert sorted(c[2] for c in restarts) == sorted(lw.WORKERS)
    assert any("Перезапустил" in t for t in told)
    lw.check(notify=told.append, conn=conn, now=NOW + 60, run=run)
    assert len([c for c in run.calls if c[:2] == ["systemctl", "restart"]]) == len(lw.WORKERS)
    lw.check(notify=told.append, conn=conn, now=NOW + lw.RESTART_GAP + 1, run=run)
    assert len([c for c in run.calls if c[:2] == ["systemctl", "restart"]]) == 2 * len(lw.WORKERS)


def test_an_error_streak_is_reported_with_its_commonest_error():
    lines = [json.dumps({"what": "error", "error": "AgentControlError: tmux list-panes failed"})
             ] * lw.ERROR_STREAK + [json.dumps({"what": "tick"}), "not json"]
    conn, told = _conn(), []
    r = lw.check(notify=told.append, conn=conn, now=NOW, run=Run(journal="\n".join(lines)))
    keys = [k for k in r["problems"] if k.startswith("errors:")]
    assert keys and "tmux list-panes failed" in r["problems"][keys[0]]


def test_a_few_errors_are_not_a_streak():
    lines = [json.dumps({"what": "error", "error": "x"})] * (lw.ERROR_STREAK - 1)
    conn = _conn()
    r = lw.check(notify=lambda m: None, conn=conn, now=NOW, run=Run(journal="\n".join(lines)))
    assert not [k for k in r["problems"] if k.startswith("errors:")]


def test_a_broken_check_and_a_broken_notice_do_not_stop_the_pass():
    conn = _conn()

    def boom(conn, now):
        raise RuntimeError("db gone")

    def bad_notify(m):
        raise OSError("telegram down")
    r = lw.check(notify=bad_notify, conn=conn, now=NOW, checks=(boom,))
    assert "check_failed:boom" in r["problems"] and r["sent"] == []
    told = []
    lw.check(notify=told.append, conn=conn, now=NOW + 10, checks=(boom,))
    assert len(told) == 1    # the failed send is not counted as told: asked again


def test_a_telegram_refusal_counts_as_not_sent():
    conn, calls = _conn(), []

    def refused(m):
        calls.append(m)
        return (False, None, "telegram rejected")
    _review(conn, 1, 501, NOW - lw.UNWOKEN_AFTER - 5)
    lw.check(notify=refused, conn=conn, now=NOW, run=Run())
    lw.check(notify=refused, conn=conn, now=NOW + 60, run=Run())
    assert len(calls) == 2


def test_the_companion_runs_the_watch_with_the_owner_push(monkeypatch):
    import importlib.util
    import os
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    spec = importlib.util.spec_from_file_location("wcomp2", os.path.join(root, "tools",
                                                                        "wake_companion.py"))
    wc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wc)
    from core.control_plane import delivery
    seen = []
    monkeypatch.setattr(delivery, "_send_owner_push", lambda m: seen.append(m) or (True, "t", None))
    monkeypatch.setattr(lw, "check", lambda notify: {"problems": {}, "sent": [notify("x") and "x"]})
    wc.watch_loop()
    assert seen == ["x"]
    assert wc.LOOP_WATCH_ENABLED and wc.LOOP_WATCH_EVERY_TICKS > 0


def test_a_fired_but_unverified_wake_is_not_waiting_for_delivery():
    """Live, 2026-11-09: the first pass reported 12 wakes 71 days old; each had been fired
    (wake_submitted) and is never offered again, exactly as pending_wake treats it."""
    conn, told = _conn(), []
    _wake(conn, 700, NOW - 6_000_000)
    conn.execute("INSERT INTO wake_submitted (event_id, ts, at, source) VALUES (?,?,?,?)",
                 (700, NOW - 6_000_000, "", "companion"))
    conn.commit()
    assert "undelivered_wakes" not in lw.check(notify=told.append, conn=conn, now=NOW,
                                               run=Run())["problems"]


def test_a_wake_the_canary_filter_passes_over_is_not_waiting(monkeypatch):
    """Live after the first fix: wakes for agents outside WAKE_BRIDGE_ONLY_AGENTS are never
    offered by design; counting them kept a false alarm on."""
    conn, told = _conn(), []
    monkeypatch.setenv("WAKE_BRIDGE_ONLY_AGENTS", "fc-*")
    conn.execute("INSERT INTO wake_audit (ts, at, event_id, decision, reason, acknowledged, "
                 "agent_id) VALUES (?,?,?,?,?,?,?)",
                 (NOW - lw.UNDELIVERED_AFTER - 60, "", 800, "wake", "", 0, "other-agent:0.0"))
    conn.commit()
    assert "undelivered_wakes" not in lw.check(notify=told.append, conn=conn, now=NOW,
                                               run=Run())["problems"]
    conn.execute("INSERT INTO wake_audit (ts, at, event_id, decision, reason, acknowledged, "
                 "agent_id) VALUES (?,?,?,?,?,?,?)",
                 (NOW - lw.UNDELIVERED_AFTER - 60, "", 801, "wake", "", 0, "fc-gaika-ua"))
    conn.commit()
    assert "undelivered_wakes" in lw.check(notify=told.append, conn=conn, now=NOW,
                                           run=Run())["problems"]
