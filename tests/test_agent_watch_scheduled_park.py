"""An explicit scheduled park is honoured until its stated time — event 57311.

`main105-backup-repair:0.0` reported the backup repair completed and verified, with a
durable host timer doing the work and "Припаркован до 23 Sep 02:00" on screen. Between
06:02 and 15:41 on 2026-09-22 it was poked "continue with the next safe step" and
published as `work_stopped_incomplete` THIRTEEN times, each reply minting a new digest
and so a new event. The native supervisor exempts an intentional wait only when the Stop
hook's payload carries `background_tasks`/`session_crons`; work parked on a HOST timer
has neither.

These pin the parse (fail-closed in every direction), the class ranking (a real question,
a crash or an owner gate still wins), the durable declaration that stops the poking, and
the wake at the stated time.
"""
import sys
import time

import pytest

sys.path.insert(0, "/root/ai-dev-runtime")

from core import agent_watch as aw  # noqa: E402

NOW = time.mktime(time.strptime("2026-09-22 15:41", "%Y-%m-%d %H:%M"))
DUE = time.mktime(time.strptime("2026-09-23 02:00", "%Y-%m-%d %H:%M"))

# The 57311 pane, ANSI stripped, as `capture-pane` printed it.
PARKED_PANE = """● Бэкап-ремонт завершён и верифицирован: последний успех 22 Sep
  03:06, следующий прогон 23 Sep 02:00. Механизм durable (systemd timer на
  хосте), блокеров нет.
  Припаркован до 23 Sep 02:00 — сбой оповестит OnFailure notifier.
✻ Churned for 52s · done 3:36 PM
❯ continue with the next safe
  step
● Припаркован.
✻ Sautéed for 5s · done 3:41 PM
──────────────────────────────────
❯ 
──────────────────────────────────
  ⧉  backup-incident
"""


# ── parsing ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Припаркован до 23 Sep 02:00 — сбой оповестит OnFailure notifier.",
    "следующий прогон 23 Sep 02:00",
    "Parked until Sep 23 02:00.",
    "parked; next run 2026-09-23 02:00",
    "next scheduled run 2026-09-23T02:00",
])
def test_an_explicit_future_park_time_is_read(text):
    assert aw.parse_park_until(text, now=NOW) == DUE


@pytest.mark.parametrize("text", [
    "Припаркован.",                                  # no time at all
    "Припаркован до утра",                           # no parseable time
    "parked until 02:00",                            # no day: guessing one outlives the park
    "Припаркован до 21 Sep 02:00",                   # already past
    "parked until 31 Feb 02:00",                     # impossible date
    "parked until 2027-12-01 02:00",                 # beyond the horizon
    "последний успех 22 Sep 03:06",                  # a timestamp with no park word
    "работаю; в 23 Sep 02:00 будет прогон",          # time not after a park word
    "",
])
def test_anything_less_than_an_explicit_future_time_parses_to_nothing(text):
    assert aw.parse_park_until(text, now=NOW) is None


def test_the_horizon_is_the_bound_on_how_long_a_park_may_last():
    assert aw.parse_park_until("parked until 2026-10-01 02:00", now=NOW) is not None
    assert aw.PARK_HORIZON_SECS >= 86400


# ── classification ──────────────────────────────────────────────────────────

def test_the_57311_pane_is_a_scheduled_park_not_an_incomplete_stop():
    r = aw.classify(PARKED_PANE, state="idle", prev_cls="working", quiet_secs=301, now=NOW)
    assert r["cls"] == "parked_scheduled"
    assert r["reason"] == "parked_until_2026-09-23T02:00"
    assert r["park_until_ts"] == DUE


def test_a_scheduled_park_publishes_no_event_at_all():
    """`work_stopped_incomplete` was the wake; a class with no `_EVENT_FOR` entry is
    recorded and never announced."""
    assert "parked_scheduled" not in aw._EVENT_FOR


def test_at_the_stated_time_the_park_expires_and_the_pane_wakes():
    r = aw.classify(PARKED_PANE, state="idle", prev_cls="working", quiet_secs=301,
                    now=DUE + 1)
    assert r["cls"] != "parked_scheduled"
    assert aw.parse_park_until(PARKED_PANE, now=DUE + 1) is None


def test_a_real_question_during_a_park_still_wins():
    tail = PARKED_PANE + "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
    r = aw.classify(tail, state="waiting_owner", prev_cls="working", quiet_secs=301, now=NOW)
    assert r["cls"] == "owner_prompt"


def test_a_crash_during_a_park_still_wins():
    tail = PARKED_PANE + "Traceback (most recent call last):\n  MemoryError\n"
    assert aw.classify(tail, state="idle", prev_cls="working", quiet_secs=301,
                       now=NOW)["cls"] == "crashed"


def test_a_park_whose_time_has_passed_classifies_as_before():
    tail = "● Готово. Припаркован до 21 Sep 02:00.\n"
    r = aw.classify(tail, state="idle", prev_cls="working", quiet_secs=400, now=NOW)
    assert r["cls"] == "completed"          # it still SAYS it finished — just not parked


def test_a_park_with_no_stated_time_is_unchanged_behaviour():
    """Only an EXPLICIT future time suppresses. A bare park is the adb62a3 case."""
    r = aw.classify("● Припаркован.\n", state="idle", prev_cls="working",
                    quiet_secs=400, now=NOW)
    assert r["cls"] == "completed"


# ── the durable declaration that stops the poking ───────────────────────────

def _scan_once(tmp_path, monkeypatch, tail, now):
    import sqlite3
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    conn = sqlite3.connect(str(tmp_path / "cp.db"))
    conn.executescript(aw._SCHEMA)
    aw._migrate_state(conn)
    agent = {"target": "main105-backup-repair:0.0", "session": "main105-backup-repair",
             "is_agent": True, "alive": True, "state": "idle", "claude_cwd": "/opt"}
    emitted = []
    r = aw.scan(agents=[agent], read_fn=lambda t: tail, conn=conn, now=now,
                emit_fn=lambda *a, **k: emitted.append(k.get("agent_id")) or {"event_id": 1})
    return conn, r, emitted


def test_a_parsed_park_declares_a_bounded_external_wait(tmp_path, monkeypatch):
    from core import native_supervisor as ns
    # the pane must look at rest for long enough that the old path would have fired
    conn, r, emitted = _scan_once(tmp_path, monkeypatch, PARKED_PANE, NOW)
    _conn2, r2, emitted2 = _scan_once(tmp_path, monkeypatch, PARKED_PANE, NOW + 400)
    assert emitted == [] and emitted2 == []
    assert ns.in_external_wait("main105-backup-repair:0.0", conn=conn, now=NOW) is True
    # bounded: it ends at the stated moment, not later
    assert ns.in_external_wait("main105-backup-repair:0.0", conn=conn, now=DUE + 1) is False


def test_the_declaration_only_suppresses_continuation(tmp_path, monkeypatch):
    """`native_external_wait` is read by the idle sweep and the SLO watch; it is not a
    mute — the supervisor's own decide() still refuses to continue, and nothing about a
    crash, a question or an owner gate is touched."""
    from core import native_supervisor as ns
    conn, _r, _e = _scan_once(tmp_path, monkeypatch, PARKED_PANE, NOW)
    assert ns.decide("agent_turn_stopped", {"_declared_external_wait": True})["action"] == "skip"
    rows = ns.list_external_waits(conn=conn, now=NOW)
    row = next(x for x in rows if x["target"] == "main105-backup-repair:0.0")
    assert row["reason"] == "scheduled_park" and row["by"] == "agent_watch"
    assert row["live"] is True


def test_no_declaration_without_a_parsed_park(tmp_path, monkeypatch):
    from core import native_supervisor as ns
    conn, _r, _e = _scan_once(tmp_path, monkeypatch, "● Working on the next replica.\n", NOW)
    assert ns.in_external_wait("main105-backup-repair:0.0", conn=conn, now=NOW) is False


# ── the whole 57311 chain, end to end ───────────────────────────────────────

def test_every_door_that_poked_57311_now_declines(tmp_path, monkeypatch):
    """One pane, four doors. Before this, the pane published `work_stopped_incomplete`,
    the idle sweep typed at it, the stall doctor saw a stall and any delivered wake
    escalated. All four read the same durable declaration now."""
    from core import closed_loop_wake as clw
    from core import native_supervisor as ns
    from core import stall_doctor as sd

    conn, r, emitted = _scan_once(tmp_path, monkeypatch, PARKED_PANE, NOW)
    target = "main105-backup-repair:0.0"

    # 1. the watcher: recorded, never announced
    assert emitted == []
    assert conn.execute("SELECT cls FROM agent_watch_state WHERE target=?",
                        (target,)).fetchone()[0] == "parked_scheduled"
    # 2. the supervisor's idle sweep
    assert ns.in_external_wait(target, conn=conn, now=NOW) is True
    # 3. the stall doctor
    assert sd.in_declared_external_wait(target, conn=conn, now=NOW) is True
    # 4. the SLO watchdog
    assert clw._armed_external_wait(conn, target, NOW) is True

    # and at the stated time every one of them returns to normal, with nothing to reset
    assert ns.in_external_wait(target, conn=conn, now=DUE + 1) is False
    assert sd.in_declared_external_wait(target, conn=conn, now=DUE + 1) is False
    assert clw._armed_external_wait(conn, target, DUE + 1) is False
    assert aw.classify(PARKED_PANE, state="idle", prev_cls="working", quiet_secs=400,
                       now=DUE + 1)["cls"] != "parked_scheduled"
