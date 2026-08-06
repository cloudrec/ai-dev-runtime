"""One piece of work must wake the owner once, however many times it is saved.

Live 2026-08-06: `/opt/mess/reports/MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` raised
`work_partial_completion` three times in sixteen minutes (22:47, 22:58, 23:03). The
fingerprint was working exactly as designed — the file really was rewritten three times —
but a new set of BYTES is not a new thing to wake someone for. The decision the owner would
have to act on ("this work is partial") never changed.

The rule these tests pin down: evidence is never lost, delivery is.

* every material rewrite still reaches the CTO inbox as its own event;
* at most one owner-action delivery / wake per meaning per 30 minutes;
* anything that changes the MEANING — classification, task correlation, stage pointer,
  a rise in severity — re-opens delivery at once rather than waiting out the window;
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


def test_partial_then_done_then_partial_is_heard_again(known, tmp_path):
    """A relapse must be audible even though the signature matches the first partial.

    This only works because the informational `done` event updates the stored meaning.
    """
    em = _Emitter()
    _write(tmp_path, PARTIAL)
    _scan(_projects(known), em, now=10_000.0)
    _write(tmp_path, "# MESS — auto-update\n\nAll parts **DONE**. Shipped and verified.\n")
    _scan(_projects(known), em, now=10_000.0 + 3 * MIN)
    _write(tmp_path, PARTIAL_REWRITTEN)
    _scan(_projects(known), em, now=10_000.0 + 6 * MIN)

    assert len(em.wakes()) == 2, "going partial again after done is a new decision"
    assert em.wakes()[1]["payload"]["owner_action_delivery_reason"] == \
        "classification_changed"


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
            "SELECT project,kind,last_push_at,suppressed_count FROM work_evidence_push "
            "WHERE meaning_key=?", ("report:mess:reports/MESS_AUTO_UPDATE.md",)).fetchone()
    finally:
        conn.close()
    assert row is not None, "no durable row -> the window would die with the process"
    assert row[0] == "mess" and row[2], "a delivery must stamp last_push_at"


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
