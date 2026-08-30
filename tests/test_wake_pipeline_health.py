"""Is the wake pipeline MOVING? (observability for unattended operation)

`health()` answered "did we wake recently and did it land?", which is not the
same question. Event 9870 proved the gap live: it was decided for the gaika-drop
chat and sat undelivered for fifteen minutes while health looked green, because
a DIFFERENT chat had just been delivered to successfully. Nothing said "something
decided is not moving", so the owner found out by noticing silence.

pipeline_health() reports on movement: what is pending and for how long, per
route; whether the deliverer is even attempting claims; and whether deliveries
are failing in a row. It writes nothing and emits no event - the wake path
feeding itself is a failure this system has already had.
"""
from __future__ import annotations

import os

import pytest

from core import wake_bridge


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    from core.control_plane import store
    c = store.connect()
    store.init_db(c)
    wake_bridge._conn(c)
    # wake_send / wake_delivery are created lazily by the writers; the health
    # reader must work before either has ever run, so build them here.
    c.execute(wake_bridge._SEND_SCHEMA)
    c.execute(wake_bridge._DELIVERY_SCHEMA)
    c.execute(wake_bridge._SUBMIT_SCHEMA)
    wake_bridge._migrate_send(c)
    wake_bridge._migrate_delivery(c)
    c.commit()
    return c


NOW = 1_800_000_000.0


def _pending_wake(conn, *, event_id, route, ts, actionable=1):
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                 "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',?,?,?,0)",
                 (ts, "2026-08-27T18:00:00+00:00", event_id, actionable, route, route))
    conn.commit()


def _claim(conn, *, ts, allowed=1, actionable=1, route="owner-os"):
    conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason,actionable,"
                 "route_key) VALUES (?,?,'companion',1,?,'t',?,?)",
                 (ts, "2026-08-27T18:00:00+00:00", allowed, actionable, route))
    conn.commit()


def _delivery(conn, *, delivered, ts_at="2026-08-27T18:00:00+00:00"):
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,"
                 "conversation,route_key) VALUES (?,?,'companion',1,?,'t','c','owner-os')",
                 (NOW, ts_at, delivered))
    conn.commit()


def test_an_empty_quiet_pipeline_is_ok(conn):
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "ok"
    assert h["reasons"] == []
    assert h["pending_count"] == 0


def test_a_wake_pending_past_the_threshold_is_reported_stuck(conn):
    """The 9870 case, isolated."""
    _pending_wake(conn, event_id=9870, route="gaika-drop",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 60)
    _claim(conn, ts=NOW - 10)                      # companion IS alive and trying
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert h["pending_oldest_route"] == "gaika-drop"
    assert any(r.startswith("pending_wake_stuck:gaika-drop") for r in h["reasons"])


def test_a_recent_delivery_to_another_chat_does_not_mask_it(conn):
    """Exactly what made this invisible: a healthy owner-os delivery seconds ago
    while a project chat's wake had been waiting a quarter of an hour."""
    _pending_wake(conn, event_id=9870, route="gaika-drop",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 60)
    _claim(conn, ts=NOW - 5)
    _delivery(conn, delivered=1)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert h["pending_by_route"]["gaika-drop"] > wake_bridge.STUCK_PENDING_SECS


def test_a_silent_companion_is_detected_even_with_work_queued(conn):
    """If the deliverer process dies, deliveries simply stop. The last successful
    delivery keeps looking recent, so only its CLAIM silence reveals it."""
    _pending_wake(conn, event_id=1, route="owner-os", ts=NOW - 30)
    _claim(conn, ts=NOW - wake_bridge.COMPANION_SILENT_SECS - 60)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("companion_silent") for r in h["reasons"])


def test_a_silent_companion_with_nothing_queued_is_not_an_alarm(conn):
    """Quiet is not broken. No pending work means no claim is expected."""
    _claim(conn, ts=NOW - wake_bridge.COMPANION_SILENT_SECS - 600)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "ok"


def test_a_companion_that_never_claimed_is_reported(conn):
    _pending_wake(conn, event_id=1, route="owner-os", ts=NOW - 30)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert "companion_never_claimed" in h["reasons"]


def test_consecutive_delivery_failures_are_surfaced(conn):
    for _ in range(wake_bridge.CONSECUTIVE_FAILURE_LIMIT):
        _delivery(conn, delivered=0)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["consecutive_delivery_failures"] >= wake_bridge.CONSECUTIVE_FAILURE_LIMIT
    assert any(r.startswith("consecutive_delivery_failures") for r in h["reasons"])


def test_a_success_after_failures_clears_the_streak(conn):
    _delivery(conn, delivered=0)
    _delivery(conn, delivered=0)
    _delivery(conn, delivered=1)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["consecutive_delivery_failures"] == 0


def test_backlog_is_reported_per_route_not_averaged(conn):
    _pending_wake(conn, event_id=1, route="mess", ts=NOW - 100)
    _pending_wake(conn, event_id=2, route="treasure", ts=NOW - 900)
    _claim(conn, ts=NOW - 5)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["pending_by_route"]["mess"] == 100
    assert h["pending_by_route"]["treasure"] == 900


def test_an_acknowledged_wake_is_not_pending(conn):
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                 "route_key,acknowledged) VALUES (?,?,?,'wake','t',1,'mess',1)",
                 (NOW - 5000, "2026-08-27T18:00:00+00:00", 42))
    conn.commit()
    assert wake_bridge.pipeline_health(conn=conn, now=NOW)["pending_count"] == 0


def test_the_kill_switch_is_reported_without_being_called_stuck(conn, monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "1")
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "disabled"
    assert "kill_switch_engaged" in h["reasons"]


def test_pipeline_health_writes_nothing(conn):
    """It must be safe to poll continuously."""
    before = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("wake_audit", "wake_send", "wake_delivery", "wake_submitted")]
    for _ in range(3):
        wake_bridge.pipeline_health(conn=conn, now=NOW)
    after = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
             for t in ("wake_audit", "wake_send", "wake_delivery", "wake_submitted")]
    assert before == after


def test_health_embeds_the_pipeline_verdict(conn):
    h = wake_bridge.health(conn=conn, now=NOW)
    assert "pipeline" in h and h["pipeline"]["status"] in ("ok", "stuck", "disabled")


# ── the watcher that says it out loud ──────────────────────────────────────
# An endpoint nobody polls is not detection. The loop logs on TRANSITION so a
# stuck pipeline appears in the service log the owner already reads, while a long
# outage stays one line rather than a stream.

def _run_loop_once(monkeypatch, statuses):
    """Drive pipeline_watch_loop deterministically over a sequence of verdicts."""
    import asyncio
    from core import wake_bridge as wb

    seen, calls = [], {"n": 0}

    def fake_health(*_a, **_k):
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        st = statuses[i]
        return {"status": st, "reasons": [f"r_{st}"], "pending_count": 1,
                "pending_oldest_age_secs": 700, "pending_oldest_route": "mess",
                "last_claim_attempt_age_secs": 5}

    async def fake_sleep(_s):
        if calls["n"] >= len(statuses):
            raise asyncio.CancelledError

    monkeypatch.setattr(wb, "pipeline_health", fake_health)
    try:
        asyncio.run(wb.pipeline_watch_loop(log=lambda lvl, m: seen.append((lvl, m)),
                                           sleep=fake_sleep))
    except asyncio.CancelledError:
        pass
    return seen


def test_it_logs_when_the_pipeline_becomes_stuck(monkeypatch):
    seen = _run_loop_once(monkeypatch, ["stuck"])
    assert seen and seen[0][0] == "warning"
    assert "wake pipeline stuck" in seen[0][1]
    assert "route=mess" in seen[0][1]


def test_a_healthy_pipeline_logs_nothing(monkeypatch):
    assert _run_loop_once(monkeypatch, ["ok", "ok", "ok"]) == []


def test_a_sustained_outage_is_one_line_not_a_stream(monkeypatch):
    seen = _run_loop_once(monkeypatch, ["stuck", "stuck", "stuck", "stuck"])
    assert len(seen) == 1


def test_recovery_is_announced(monkeypatch):
    seen = _run_loop_once(monkeypatch, ["stuck", "stuck", "ok"])
    assert [lvl for lvl, _ in seen] == ["warning", "info"]
    assert "recovered" in seen[1][1]


def test_a_failing_health_call_never_kills_the_loop(monkeypatch):
    import asyncio
    from core import wake_bridge as wb
    seen, calls = [], {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("db locked")

    async def fake_sleep(_s):
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(wb, "pipeline_health", boom)
    try:
        asyncio.run(wb.pipeline_watch_loop(log=lambda lvl, m: seen.append((lvl, m)),
                                           sleep=fake_sleep))
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2                      # kept going after the exception
    assert all("watch error" in m for _l, m in seen)


# ── deployer version skew ──────────────────────────────────────────────────
# The companion is a separate long-running process that imports wake_bridge at
# startup, so restarting the API alone leaves the DELIVERER on old code. That is
# not theoretical: after the routing fix went live, the API decided a wake for
# the gaika-drop chat while the stale companion delivered it to owner-os and
# logged `[route owner-os]`. Same database, two versions of the truth.

def test_a_worker_started_before_the_current_code_is_flagged(conn, monkeypatch):
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 3600)
    monkeypatch.setattr(wake_bridge, "_module_mtime", lambda w=None: NOW - 60)
    skew = wake_bridge.worker_skew(conn=conn, now=NOW)
    assert len(skew) == 1
    assert skew[0]["worker"] == "wake_companion"
    assert skew[0]["code_newer_by_secs"] > 0


def test_a_worker_restarted_after_the_deploy_is_clean(conn, monkeypatch):
    monkeypatch.setattr(wake_bridge, "_module_mtime", lambda w=None: NOW - 600)
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 60)
    assert wake_bridge.worker_skew(conn=conn, now=NOW) == []


def test_skew_makes_the_pipeline_report_stuck(conn, monkeypatch):
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 3600)
    monkeypatch.setattr(wake_bridge, "_module_mtime", lambda w=None: NOW - 60)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("worker_running_stale_code:wake_companion") for r in h["reasons"])


def test_a_heartbeat_from_the_same_process_keeps_the_original_start_time(conn, monkeypatch):
    """A busy stale worker must not be able to clear its own alarm."""
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 3600)
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 5)
    monkeypatch.setattr(wake_bridge, "_module_mtime", lambda w=None: NOW - 60)
    assert len(wake_bridge.worker_skew(conn=conn, now=NOW)) == 1


def test_a_restart_under_a_new_pid_clears_the_alarm(conn, monkeypatch):
    """Restarting is exactly how stale code gets fixed, so the clock restarts.
    An alarm that cannot clear is worse than none - it trains people to ignore
    it. Observed live: the companion was restarted and the skew alarm stayed on
    because the heartbeat path preserved the old start time."""
    monkeypatch.setattr(wake_bridge.os, "getpid", lambda: 1111)
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 3600)
    monkeypatch.setattr(wake_bridge, "_module_mtime", lambda w=None: NOW - 600)
    assert len(wake_bridge.worker_skew(conn=conn, now=NOW)) == 1     # stale

    monkeypatch.setattr(wake_bridge.os, "getpid", lambda: 2222)      # restarted
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 10)
    assert wake_bridge.worker_skew(conn=conn, now=NOW) == []         # cleared


def test_no_registered_worker_reports_no_skew(conn):
    assert wake_bridge.worker_skew(conn=conn, now=NOW) == []


# ── per-worker watched files (event 11073) ─────────────────────────────────
# ai-runtime.service (running agent_orchestrator.run_loop, the source of
# waiting_transitions/agent_waiting_input events) was never restarted across
# three straight agent_control.py fixes on 2026-08-28 - only the SEPARATE
# owner-os-wake-companion.service was, every time. worker_skew() previously
# judged every registered worker against wake_bridge.py's own mtime, so it
# could not have caught this: agent_orchestrator watches a different file set.

def test_a_worker_is_judged_only_against_its_own_watched_files(conn, monkeypatch):
    monkeypatch.setattr(wake_bridge, "_WORKER_WATCHED_FILES", {
        "wake_companion": ("wake_bridge.py",),
        "agent_orchestrator": ("agent_control.py",),
    })
    mtimes = {"wake_bridge.py": NOW - 600, "agent_control.py": NOW - 10}
    monkeypatch.setattr(wake_bridge.os.path, "getmtime",
                         lambda p: mtimes[os.path.basename(p)])
    wake_bridge.register_worker("wake_companion", conn=conn, now=NOW - 300)
    wake_bridge.register_worker("agent_orchestrator", conn=conn, now=NOW - 300)
    skew = wake_bridge.worker_skew(conn=conn, now=NOW)
    assert [w["worker"] for w in skew] == ["agent_orchestrator"]


def test_agent_orchestrator_watched_files_include_agent_control(monkeypatch):
    """The exact shape of event 11073: agent_control.py changed, wake_bridge.py
    did not. If agent_control.py ever falls out of the watched set, this
    mechanism silently stops catching the class of bug it was built for."""
    assert "agent_control.py" in wake_bridge._WORKER_WATCHED_FILES["agent_orchestrator"]
    assert "agent_orchestrator.py" in wake_bridge._WORKER_WATCHED_FILES["agent_orchestrator"]


# ── a cooldown is not a stall ──────────────────────────────────────────────
# Observed live: event 9832 waited out the owner-os 900s floor, counting down
# correctly (475s -> 360s), and would have crossed the 600s "stuck" threshold
# while the system was behaving exactly as designed. An alarm that fires on
# correct behaviour is how a detector teaches people to ignore it.

def test_a_wake_waiting_out_its_own_cooldown_is_not_called_stuck(conn):
    """Modelled on live event 9832: a GENERIC wake on owner-os, past the stuck
    threshold, still inside the 900s generic floor."""
    _pending_wake(conn, event_id=1, route="owner-os", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 120)
    _claim(conn, ts=NOW - 60, actionable=0)      # a send to that chat 60s ago
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "waiting"
    assert h["cooldown_remaining_secs"] > 0
    assert not any(r.startswith("pending_wake_stuck") for r in h["reasons"])
    assert any(r.startswith("waiting_on_cooldown:owner-os") for r in h["reasons"])


def test_once_the_floor_clears_a_wake_that_still_has_not_gone_out_is_stuck(conn):
    _pending_wake(conn, event_id=1, route="owner-os",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 120)
    _claim(conn, ts=NOW - wake_bridge.ACTIONABLE_COOLDOWN_SECS
           - wake_bridge.COOLDOWN_SECS - 10)     # floor long expired
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("pending_wake_stuck:owner-os") for r in h["reasons"])


def test_companion_silence_is_not_alarmed_while_a_cooldown_holds(conn):
    """Nothing to claim yet means nothing to complain about."""
    _pending_wake(conn, event_id=1, route="owner-os", ts=NOW - 60)
    _claim(conn, ts=NOW - 30)
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert not any(r.startswith("companion_silent") for r in h["reasons"])


def test_the_generic_and_actionable_floors_are_not_interchangeable(conn):
    """A claim 90s old clears the 60s actionable floor but not the 900s generic
    one; reading the wrong floor would mislabel a correctly-waiting wake."""
    _pending_wake(conn, event_id=1, route="owner-os", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 120)
    _claim(conn, ts=NOW - 90, actionable=0)
    assert wake_bridge.pipeline_health(conn=conn, now=NOW)["status"] == "waiting"


def test_an_actionable_wake_is_measured_against_the_actionable_floor(conn):
    """The two classes have different floors; using the generic one would either
    over- or under-report a blocked actionable wake."""
    conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                 "project_id,route_key,acknowledged) VALUES (?,?,?,'wake','t',1,?,?,0)",
                 (NOW - wake_bridge.STUCK_PENDING_SECS - 60,
                  "2026-08-27T18:00:00+00:00", 7, "mess", "mess"))
    conn.execute("INSERT INTO wake_send (ts,at,source,event_id,allowed,reason,actionable,"
                 "route_key) VALUES (?,?,'companion',7,1,'t',1,'mess')",
                 (NOW - 5, "2026-08-27T18:00:00+00:00"))
    conn.commit()
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "waiting"
    assert h["cooldown_remaining_secs"] > 0


def test_a_cooldown_on_another_chat_does_not_excuse_a_stuck_wake(conn):
    """Per-chat throughout: a send to owner-os must not make a stalled mess wake
    look like it is merely waiting."""
    _pending_wake(conn, event_id=1, route="mess",
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 120)
    _claim(conn, ts=NOW - 30)                    # that claim was for owner-os
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("pending_wake_stuck:mess") for r in h["reasons"])


def test_a_stuck_chat_is_not_hidden_behind_a_waiting_one(conn):
    """Per route here too: judging only the single oldest pending wake would let
    a genuinely stuck chat hide behind another that is correctly waiting out its
    floor - the same cross-chat blindness as the original defects."""
    # mess: floor cleared long ago, wake far past the threshold -> stuck
    _pending_wake(conn, event_id=1, route="mess", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 300)
    _claim(conn, ts=NOW - wake_bridge.COOLDOWN_SECS - 60, actionable=0, route="mess")
    # owner-os: just sent to, so legitimately waiting, and OLDER
    _pending_wake(conn, event_id=2, route="owner-os", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 600)
    _claim(conn, ts=NOW - 30, actionable=0, route="owner-os")
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert "mess" in h["stuck_routes"]
    assert "owner-os" in h["waiting_routes"]


def test_a_dead_companion_is_caught_while_the_work_is_still_fresh(conn):
    """Silence is about whether there is claimable work, not about how long a
    wake has waited - otherwise a crashed deliverer stays invisible for the
    whole stuck threshold."""
    _pending_wake(conn, event_id=1, route="mess", actionable=0, ts=NOW - 20)
    _claim(conn, ts=NOW - wake_bridge.COMPANION_SILENT_SECS - 60,
           actionable=0, route="treasure")       # last attempt, unrelated chat
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["status"] == "stuck"
    assert any(r.startswith("companion_silent") for r in h["reasons"])


def test_an_event_benched_after_a_failed_delivery_is_not_counted_as_pending(conn):
    """The selector benches an event for RETRY_BACKOFF_SECS after a failed
    delivery and moves to the next in line. Health must model the same
    eligibility, or it describes a queue nobody is trying to drain and can call
    the pipeline stuck while the backoff is doing its job."""
    _pending_wake(conn, event_id=55, route="mess", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 300)
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,"
                 "conversation,route_key) VALUES (?,?,'companion',55,0,'failed','c','mess')",
                 (NOW - 30, "2026-08-27T18:00:00+00:00"))
    conn.commit()
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["pending_count"] == 0
    assert h["benched_after_failure"] == 1
    assert not any(r.startswith("pending_wake_stuck") for r in h["reasons"])


def test_once_the_backoff_expires_the_event_counts_again(conn):
    _pending_wake(conn, event_id=56, route="mess", actionable=0,
                  ts=NOW - wake_bridge.STUCK_PENDING_SECS - 300)
    conn.execute("INSERT INTO wake_delivery (ts,at,source,event_id,delivered,reason,"
                 "conversation,route_key) VALUES (?,?,'companion',56,0,'failed','c','mess')",
                 (NOW - wake_bridge.RETRY_BACKOFF_SECS - 60, "2026-08-27T18:00:00+00:00"))
    conn.commit()
    h = wake_bridge.pipeline_health(conn=conn, now=NOW)
    assert h["pending_count"] == 1
    assert h["benched_after_failure"] == 0


# ── the skew watcher must watch the companion's ACTUAL delivery code ─────────
# _WORKER_WATCHED_FILES listed only wake_bridge.py and wake_routes.py, but the
# companion imports tools/cdp_composer.py for submit_phrase — the composer
# selectors, the latch boundary, page_responsive/recover_wedged_tab and the whole
# post-send verification loop live there — and tools/wake_companion.py is its own
# entrypoint. A fix to either changed how wakes are delivered while raising no
# skew, which is the exact failure this mechanism exists to catch.

def _watched(worker):
    import os as _os
    here = _os.path.dirname(_os.path.abspath(wake_bridge.__file__))
    return {_os.path.normpath(_os.path.join(here, rel))
            for rel in wake_bridge._WORKER_WATCHED_FILES[worker]}


def test_the_companion_watches_its_own_delivery_code():
    watched = _watched("wake_companion")
    assert any(p.endswith("tools/cdp_composer.py") for p in watched), watched
    assert any(p.endswith("tools/wake_companion.py") for p in watched), watched


def test_the_companion_still_watches_the_bridge_modules():
    """Extending the list must not drop what it already covered."""
    watched = _watched("wake_companion")
    for name in ("wake_bridge.py", "wake_routes.py"):
        assert any(p.endswith(name) for p in watched), (name, watched)


def test_every_watched_file_actually_exists():
    """A path typo would silently contribute mtime 0 and weaken the alarm."""
    import os as _os
    missing = {w: sorted(p for p in _watched(w) if not _os.path.exists(p))
               for w in wake_bridge._WORKER_WATCHED_FILES}
    assert not any(missing.values()), missing


def test_a_change_to_the_composer_drives_the_newest_mtime(monkeypatch):
    """The behaviour that matters, asserted deterministically.

    Comparing real mtimes is vacuous in a fresh checkout — every file shares the
    checkout timestamp, so the assertion holds whether or not the composer is
    watched. Instead make ONLY the composer distinctly newer and require
    _module_mtime to return it.
    """
    import os as _os
    real = _os.path.getmtime
    marker = 9_999_999_999.0

    def fake(path):
        return marker if str(path).endswith("cdp_composer.py") else 1.0

    monkeypatch.setattr(wake_bridge.os.path, "getmtime", fake)
    assert wake_bridge._module_mtime("wake_companion") == marker, (
        "cdp_composer.py does not contribute to the companion's newest-mtime")
    # and a worker that started before it now reads as stale
    monkeypatch.setattr(wake_bridge.os.path, "getmtime", real)


def test_the_orchestrator_watch_list_is_unchanged():
    watched = _watched("agent_orchestrator")
    for name in ("agent_control.py", "agent_orchestrator.py", "waiting_transitions.py"):
        assert any(p.endswith(name) for p in watched), (name, watched)
