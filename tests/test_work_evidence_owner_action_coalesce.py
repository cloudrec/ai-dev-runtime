"""One piece of work must wake the owner once, however many times it is saved.

Live 2026-08-06: `/opt/mess/reports/MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` raised
`work_partial_completion` three times in sixteen minutes (22:47, 22:58, 23:03). The
fingerprint was working exactly as designed — the file really was rewritten three times —
but a new set of BYTES is not a new thing to wake someone for. The decision the owner would
have to act on ("this work is partial") never changed.

The rule these tests pin down: evidence is never lost, delivery is.

* every material rewrite still reaches the CTO inbox as its own event;
* at most one owner-action delivery / wake per MEANING — project + report + semantic
  reason — per 30 minutes, and a meaning the owner has not been told inside that window
  is never suppressed;
* a state the owner has not just heard — partial → blocked, blocked → done — is delivered
  at once rather than waiting out someone else's window, as are a changed task
  correlation, a moved stage pointer and a rise in severity;
* a report that FLAPS between two states it has already delivered is not an endless
  doorbell: each state owns its own window;
* the window is durable, so a restart cannot turn a redeploy into a fresh round of wakes;
* informational events and commit evidence are never suppressed at all.
"""
from __future__ import annotations

import pytest

from core import work_evidence as we
from core.control_plane import store

PARTIAL = """# MESS — auto-update programme

## Part 1 — responsive navigation — **DONE**
Shipped.

## Part 2 — auto-update — **AUDIT COMPLETE / IMPLEMENTATION NOT STARTED**
The updater was audited. The implementation was NOT STARTED.
"""

# Same meaning, different bytes — this is what a re-save actually looks like.
PARTIAL_REWRITTEN = PARTIAL + "\n<!-- saved again at 22:58, wording tightened -->\n"
PARTIAL_REWRITTEN_2 = PARTIAL + "\n<!-- saved again at 23:03, typo fixed -->\n"

# A genuine change of meaning: the half-done part is now blocked, not merely unstarted.
BLOCKED = """# MESS — auto-update programme

## Part 1 — responsive navigation — **DONE**
Shipped.

## Part 2 — auto-update — **BLOCKED**
BLOCKED ON an owner credential. Work cannot continue.
"""

# Same meaning as BLOCKED, different bytes — the other half of a flap.
BLOCKED_REWRITTEN = BLOCKED + "\n<!-- saved again, note added -->\n"

PLAIN = """# MESS — weekly inventory

Raw counts only, nothing concluded.
"""

MIN = 60.0


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    store.init_db().close()
    yield


class _Emitter:
    """Captures what would reach the CTO inbox, and — critically — what would WAKE.

    `cto.emit` consults the wake bridge and the night-shift signal on
    `severity in PUSH_SEVERITIES or owner_action_required`, independently of `push`.
    So a wake is counted the same way the real emitter decides one.
    """

    def __init__(self):
        self.events = []

    def __call__(self, source, type, **kw):
        self.events.append({"source": source, "type": type, **kw})
        return {"event_id": len(self.events), "pushed": self._wakes(self.events[-1])}

    @staticmethod
    def _wakes(e) -> bool:
        return bool(e.get("severity") in ("high", "critical") or
                    e.get("owner_action_required"))

    def wakes(self) -> list:
        return [e for e in self.events if self._wakes(e)]

    def of_type(self, t) -> list:
        return [e for e in self.events if e["type"] == t]


def _write(tmp_path, text, name="MESS_AUTO_UPDATE.md", project="mess"):
    root = tmp_path / project
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / name).write_text(text)
    return root


def _projects(root, target="mess-qa-automation:0.0", project="mess"):
    return {target: {"cwd": str(root), "project": project}}


def _scan(projects, emitter, *, now, state="working", pointer="stage_09"):
    """A scan with the project already known, so cold-start backfill is not in play."""
    return we.scan(projects, emit_fn=emitter, state_fn=lambda t: state,
                   pointer_fn=lambda t: pointer, now=now)


@pytest.fixture()
def known(tmp_path):
    """Make the project 'already seen' so the first real report is news, not backfill."""
    root = _write(tmp_path, PLAIN, name="SEED.md")
    em = _Emitter()
    _scan(_projects(root), em, now=1000.0)
    return root


# ── the live incident ────────────────────────────────────────────────────────
def test_three_rewrites_in_sixteen_minutes_wake_the_owner_once(known, tmp_path):
    """THE regression. Three saves, three pieces of evidence, one interruption."""
    em = _Emitter()
    t0 = 10_000.0
    for offset, text in ((0 * MIN, PARTIAL),
                         (11 * MIN, PARTIAL_REWRITTEN),
                         (16 * MIN, PARTIAL_REWRITTEN_2)):
        _write(tmp_path, text)
        _scan(_projects(known), em, now=t0 + offset)

    partials = em.of_type(we.EVENT_PARTIAL)
    assert len(partials) == 3, "a material rewrite must still be recorded as evidence"
    assert len(em.wakes()) == 1, [
        (e["type"], e.get("severity"), e.get("owner_action_required")) for e in em.events]

    # The one that got through is the first; the others are inbox-only but complete.
    assert partials[0]["owner_action_required"] is True
    for later in partials[1:]:
        assert later["owner_action_required"] is False
        assert later["push"] is False
        assert later["payload"]["coalesced"] is True
        assert later["payload"]["original_severity"] == "high"
        assert later["payload"]["markers"]["partial"] is True, "evidence must survive intact"
    assert partials[2]["payload"]["suppressed_count"] == 2


def test_every_rewrite_is_still_its_own_inbox_event(known, tmp_path):
    """Suppression is of DELIVERY, never of history."""
    em = _Emitter()
    for i, text in enumerate((PARTIAL, PARTIAL_REWRITTEN, PARTIAL_REWRITTEN_2)):
        _write(tmp_path, text)
        _scan(_projects(known), em, now=10_000.0 + i * 5 * MIN)
    keys = [e["dedup_key"] for e in em.of_type(we.EVENT_PARTIAL)]
    assert len(set(keys)) == 3, "each material change needs its own audit identity"


def test_repeats_advance_suppressed_count_and_last_seen(known, tmp_path):
    """A coalesced repeat is counted and dated — silence must still be observable."""
    em = _Emitter()
    seen = []
    for i, text in enumerate((PARTIAL, PARTIAL_REWRITTEN, PARTIAL_REWRITTEN_2)):
        _write(tmp_path, text)
        _scan(_projects(known), em, now=10_000.0 + i * 2 * MIN)
        conn = store.connect()
        try:
            seen.append(conn.execute(
                "SELECT suppressed_count,last_seen_at,last_push_at FROM work_evidence_push "
                "WHERE evidence_key=?",
                ("report:mess:reports/MESS_AUTO_UPDATE.md",)).fetchone())
        finally:
            conn.close()

    assert [r[0] for r in seen] == [0, 1, 2], "each repeat must increment suppressed_count"
    assert seen[0][1] < seen[1][1] < seen[2][1], "last_seen must move on every repeat"
    assert seen[0][2] == seen[1][2] == seen[2][2], (
        "last_push_at must NOT move on a suppressed repeat, or the window would never end")


# ── the flap: meanings the owner already heard ───────────────────────────────
def test_a_flapping_report_does_not_re_wake_for_meanings_already_delivered(known, tmp_path):
    """v8 regression. The window is per MEANING, so a toggle is not an infinite doorbell.

    Keyed on the report alone, `partial → blocked → partial → blocked` each read as "the
    classification changed" and delivered — four interruptions in fifteen minutes for two
    decisions the owner had already been told. Each meaning now owns its own 30 minutes.
    """
    em = _Emitter()
    for off, text in ((0 * MIN, PARTIAL), (5 * MIN, BLOCKED),
                      (10 * MIN, PARTIAL_REWRITTEN), (15 * MIN, BLOCKED_REWRITTEN)):
        _write(tmp_path, text)
        _scan(_projects(known), em, now=10_000.0 + off)

    assert len(em.events) == 4, "every save is still its own piece of evidence"
    assert len(em.wakes()) == 2, [
        (e.get("severity"), e["payload"].get("owner_action_delivery_reason"),
         e["payload"].get("coalesced_reason")) for e in em.events]
    assert [e["payload"]["owner_action_delivery_reason"] for e in em.wakes()] == \
        ["first_time", "classification_changed"]
    for repeat in em.events[2:]:
        assert repeat["payload"]["coalesced_reason"] == "coalesced_same_meaning"


def test_the_stopped_incomplete_finding_coalesces_too(known, tmp_path):
    """`work_stopped_incomplete` is the second owner-action path and gates identically.

    An agent that goes idle on unfinished work raises it alongside the partial event, so an
    ungated stopped-finding would have doubled every one of the live interruptions.
    """
    em = _Emitter()
    for i, text in enumerate((PARTIAL, PARTIAL_REWRITTEN, PARTIAL_REWRITTEN_2)):
        _write(tmp_path, text)
        _scan(_projects(known), em, now=10_000.0 + i * 4 * MIN, state="idle")

    stopped = em.of_type(we.EVENT_STOPPED)
    assert len(stopped) == 3, "each save is still evidence that work stopped half-done"
    assert [e["owner_action_required"] for e in stopped] == [True, False, False]
    assert stopped[-1]["payload"]["suppressed_count"] == 2
    assert len(em.wakes()) == 2, "one partial + one stopped, not six"


def test_each_distinct_meaning_keeps_its_own_window(known, tmp_path):
    """Two meanings, two windows — neither one's cooldown silences the other."""
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, BLOCKED)
    _scan(_projects(known), em, now=10_000.0 + 1 * MIN)
    assert len(em.wakes()) == 2

    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT meaning_key,last_push_at FROM work_evidence_push WHERE evidence_key=?",
            ("report:mess:reports/MESS_AUTO_UPDATE.md",)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2, f"one row per meaning; got {rows}"
    assert all(r[1] for r in rows), "both meanings really were delivered"


# ── reopening delivery ───────────────────────────────────────────────────────
def test_a_repeat_after_the_cooldown_is_news_again(known, tmp_path):
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 31 * MIN)

    assert len(em.wakes()) == 2, "31 minutes is past the 30-minute window"
    assert em.of_type(we.EVENT_PARTIAL)[1]["payload"]["owner_action_delivery_reason"] == \
        "cooldown_expired"


def test_a_change_of_classification_reopens_delivery_immediately(known, tmp_path):
    """partial → blocked is a different decision, not a re-save of the same one."""
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, BLOCKED)
    _scan(_projects(known), em, now=10_000.0 + 2 * MIN)

    assert len(em.wakes()) == 2, "a change of meaning must not wait out the cooldown"
    assert em.wakes()[1]["payload"]["owner_action_delivery_reason"] == \
        "classification_changed"


def test_partial_then_done_then_partial_inside_the_window_is_not_a_second_wake(
        known, tmp_path):
    """A relapse the owner has ALREADY been told about does not interrupt him twice.

    The intermediate `done` never woke anyone — a completed report is informational, at
    severity `info`. So the last thing the owner was told about this report is "partial",
    six minutes ago, and the relapse restates it. All three events are in the inbox.
    """
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, "# MESS — auto-update\n\nAll parts **DONE**. Shipped and verified.\n")
    _scan(_projects(known), em, now=10_000.0 + 3 * MIN)
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 6 * MIN)

    assert len(em.wakes()) == 1, "the owner already knows this report is partial"
    assert len(em.events) == 3, "every state the report passed through is still evidence"
    assert em.of_type(we.EVENT_PARTIAL)[-1]["payload"]["coalesced"] is True


def test_a_relapse_after_the_window_is_heard_again(known, tmp_path):
    """Same sequence, past the cooldown: silence is bounded at 30 minutes, not permanent."""
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, "# MESS — auto-update\n\nAll parts **DONE**. Shipped and verified.\n")
    _scan(_projects(known), em, now=10_000.0 + 3 * MIN)
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 31 * MIN)

    assert len(em.wakes()) == 2
    assert em.wakes()[1]["payload"]["owner_action_delivery_reason"] == "cooldown_expired"


def test_a_new_stage_pointer_reopens_delivery(known, tmp_path):
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0, pointer="stage_09")
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 2 * MIN, pointer="stage_10")

    assert len(em.wakes()) == 2
    assert em.wakes()[1]["payload"]["owner_action_delivery_reason"] == "stage_pointer_moved"


def test_a_new_task_correlation_reopens_delivery(known, tmp_path, monkeypatch):
    em = _Emitter()
    tasks = iter([{"id": "task-1"}, {"id": "task-2"}])
    monkeypatch.setattr(we, "_open_task", lambda *a, **k: next(tasks))
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 2 * MIN)

    assert len(em.wakes()) == 2
    assert em.wakes()[1]["payload"]["owner_action_delivery_reason"] == \
        "task_correlation_changed"


# ── durability ───────────────────────────────────────────────────────────────
def test_a_restart_between_rewrites_does_not_reset_the_window(known, tmp_path):
    """The cooldown lives in SQLite, so nothing in memory can be lost with the process."""
    em1 = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em1, now=10_000.0)
    assert len(em1.wakes()) == 1

    # Simulate the restart: drop every module-level object and re-import from disk, the
    # way a fresh interpreter would. Only the database survives — as in production.
    import importlib
    importlib.reload(we)

    em2 = _Emitter()
    _write(tmp_path, PARTIAL_REWRITTEN)
    we.scan(_projects(known), emit_fn=em2, state_fn=lambda t: "working",
            pointer_fn=lambda t: "stage_09", now=10_000.0 + 5 * MIN)

    assert em2.of_type(we.EVENT_PARTIAL), "the rewrite is still evidence after a restart"
    assert len(em2.wakes()) == 0, "a restart must not re-open a live cooldown"


def test_the_coalesce_state_is_a_real_durable_row(known, tmp_path):
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT project,kind,last_push_at,suppressed_count,meaning_key,last_seen_at "
            "FROM work_evidence_push WHERE evidence_key=?",
            ("report:mess:reports/MESS_AUTO_UPDATE.md",)).fetchone()
    finally:
        conn.close()
    assert row is not None, "no durable row -> the window would die with the process"
    assert row[0] == "mess" and row[2], "a delivery must stamp last_push_at"
    assert row[5], "a delivery is also an observation"
    # The window belongs to the report AND what it says, not to the report alone.
    assert row[4].startswith("report:mess:reports/MESS_AUTO_UPDATE.md|"), row[4]
    assert we.EVENT_PARTIAL in row[4], row[4]


def test_a_live_v8_cooldown_survives_the_upgrade_to_v9(tmp_path, monkeypatch):
    """The deploy that ships v9 must not itself become a round of owner wakes.

    A v8 row is keyed by the report alone. Dropping those rows on upgrade would make every
    live cooldown read as `first_time` on the next 5-minute tick — the exact interruption
    this table exists to prevent, caused by the fix for it. They are re-keyed instead.
    """
    db = tmp_path / "v8.db"
    monkeypatch.setenv("CONTROL_PLANE_DB", str(db))
    store.init_db().close()

    # Rewind an existing row to its v8 shape: no evidence_key, key = the report alone.
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO work_evidence_push(meaning_key,project,target,ref,kind,class_sig,"
            "task_id,stage_pointer,severity,last_push_at,last_event_id,suppressed_count,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("report:mess:reports/MESS_AUTO_UPDATE.md", "mess", "mess-qa-automation:0.0",
             "reports/MESS_AUTO_UPDATE.md", we.EVENT_PARTIAL,
             f"{we.EVENT_PARTIAL}|not_started,partial|", "", "stage_09", "high",
             "2026-08-07T00:00:00+00:00", 41, 0, "2026-08-07T00:00:00+00:00"))
        conn.execute("UPDATE work_evidence_push SET evidence_key=NULL, last_seen_at=NULL")
        conn.execute("UPDATE schema_meta SET version=8 WHERE id=1")
        conn.commit()
    finally:
        conn.close()

    store.init_db().close()

    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT meaning_key,evidence_key,last_push_at,last_seen_at,last_event_id "
            "FROM work_evidence_push").fetchone()
    finally:
        conn.close()
    assert store.schema_version() == 9
    assert row[0] == (f"report:mess:reports/MESS_AUTO_UPDATE.md|"
                      f"{we.EVENT_PARTIAL}|not_started,partial|"), row[0]
    assert row[1] == "report:mess:reports/MESS_AUTO_UPDATE.md"
    assert row[2] == "2026-08-07T00:00:00+00:00", "the live cooldown must survive intact"
    assert row[3] == "2026-08-07T00:00:00+00:00", "last_seen backfills from updated_at"
    assert row[4] == 41, "the event it coalesces with is not forgotten"


# ── no over-reach ────────────────────────────────────────────────────────────
def test_different_reports_do_not_coalesce_with_each_other(known, tmp_path):
    em = _Emitter()
    _write(tmp_path, PARTIAL, name="REPORT_A.md")
    _write(tmp_path, PARTIAL, name="REPORT_B.md")
    _scan(_projects(known), em, now=10_000.0)

    refs = {e["payload"]["report"] for e in em.wakes()}
    assert refs == {"reports/REPORT_A.md", "reports/REPORT_B.md"}, (
        f"each report owns its own window; got {refs}")


def test_different_projects_do_not_coalesce_with_each_other(tmp_path):
    root_a = _write(tmp_path, PLAIN, name="SEED.md", project="mess")
    root_b = _write(tmp_path, PLAIN, name="SEED.md", project="arbitrage2")
    projects = {**_projects(root_a, "mess-qa-automation:0.0", "mess"),
                **_projects(root_b, "arbitrage2-opus:0.0", "arbitrage2")}
    em0 = _Emitter()
    _scan(projects, em0, now=1000.0)

    em = _Emitter()
    _write(tmp_path, PARTIAL, project="mess")
    _write(tmp_path, PARTIAL, project="arbitrage2")
    _scan(projects, em, now=10_000.0)

    projects_woken = {e["project_id"] for e in em.wakes()}
    assert projects_woken == {"mess", "arbitrage2"}, (
        "one project's cooldown must never silence another")


def test_informational_report_events_are_never_suppressed(known, tmp_path):
    """Requirement: only repeated OWNER-ACTION delivery is coalesced."""
    em = _Emitter()
    for i, extra in enumerate(("", "one", "two", "three")):
        _write(tmp_path, PLAIN + f"\n<!-- {extra} -->\n", name="INVENTORY.md")
        _scan(_projects(known), em, now=10_000.0 + i * MIN)
    published = em.of_type(we.EVENT_REPORT)
    assert len(published) == 4
    assert all(e["payload"].get("coalesced") is False for e in published)
    assert all(e.get("owner_action_required") is False for e in published)


def test_a_second_distinct_report_is_never_silently_dropped(known, tmp_path):
    """Bounding owner-action per SWEEP would lose the second report for good.

    `_seen()` skips an unchanged fingerprint on the next scan, so a finding deferred once is
    never reconsidered — it would simply never be delivered. Each distinct report therefore
    gets its own window, while the sweep still sends a single outbox push.
    """
    em = _Emitter()
    _write(tmp_path, PARTIAL, name="REPORT_A.md")
    _write(tmp_path, PARTIAL, name="REPORT_B.md")
    _scan(_projects(known), em, now=10_000.0)

    assert len(em.wakes()) == 2, "both distinct decisions must be heard"
    pushed = [e for e in em.of_type(we.EVENT_PARTIAL) if e["push"] is not False]
    assert len(pushed) == 1, "one sweep still sends at most one outbox push"

    # And neither of them re-wakes on the next sweep.
    _scan(_projects(known), em, now=10_000.0 + 60.0)
    assert len(em.wakes()) == 2
