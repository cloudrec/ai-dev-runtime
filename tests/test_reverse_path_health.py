"""The ChatGPT -> Owner OS control path must not fail silently.

On 2026-09-13 the `seo` containers had been down four days. Owner OS was healthy and
said so; every wake was delivered (15 of 15) and every one was useless, because the
supervisor's `agent_status` / `agent_read` / `agent_send` calls could not reach this
host. Fourteen wake watches closed `pane_awaiting_owner`. The owner discovered it by
hitting a 502 in the chat.

Nothing in this repo could have caught that: `/health` answered in 9ms throughout, and
a self-check cannot see a dependency it never touches. These tests pin the four states
that matter, and in particular that unknown is never reported as healthy.
"""
from __future__ import annotations

import sqlite3

import pytest

from core.control_plane import reverse_path as rp


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    yield


def _ok():
    return True, "http_200 in 21ms"


def _down():
    return False, "unreachable:URLError"


def _five_oh_two():
    return False, "upstream_error:502"


# ── healthy ─────────────────────────────────────────────────────────────────
def test_healthy_path_reports_ok_with_evidence():
    st = rp.check(probe=_ok, now=1000.0)
    assert st["state"] == "ok" and st["healthy"] is True
    assert st["last_ok_at"], "a healthy verdict must carry when it was proven"
    assert "reach" in st["reason"]


# ── broken ──────────────────────────────────────────────────────────────────
def test_broken_path_is_not_healthy_and_names_the_dependency():
    st = rp.check(probe=_down, now=1000.0)
    assert st["state"] == "broken" and st["healthy"] is False
    assert "seo" in st["depends_on"], "the alert must say WHAT is down"
    assert "unreachable" in st["last_error"]


def test_a_502_is_broken_not_ok():
    """The exact symptom the owner saw: nginx alive, upstream dead."""
    st = rp.check(probe=_five_oh_two, now=1000.0)
    assert st["state"] == "broken"
    assert "502" in st["last_error"]


def test_broken_reports_how_long_it_has_been_down():
    rp.check(probe=_ok, now=1000.0)
    st = rp.check(probe=_down, now=1000.0 + 4 * 86400)      # the real four days
    assert st["state"] == "broken"
    assert st["down_for_secs"] == pytest.approx(4 * 86400, abs=1)
    assert st["last_ok_at"], "last-known-good must survive the failure"


def test_remediation_does_not_tell_anyone_to_restart_it():
    """The worker died of OOM (137). Restarting is the owner's call, not the monitor's."""
    st = rp.check(probe=_down, now=1000.0)
    rem = st["remediation"].lower()
    assert "restart" not in rem, f"the monitor is prescribing a restart: {rem}"
    assert "seo" in rem


# ── recovered ───────────────────────────────────────────────────────────────
def test_recovery_flips_to_ok_but_keeps_the_failure_on_record():
    rp.check(probe=_ok, now=1000.0)
    rp.check(probe=_down, now=2000.0)
    st = rp.check(probe=_ok, now=3000.0)
    assert st["state"] == "ok" and st["healthy"] is True
    assert st["last_failure_at"], "a recovery must not erase that it broke"
    assert st["last_ok_at"]


# ── unknown / stale — the fail-closed cases ─────────────────────────────────
def test_never_probed_is_unknown_not_ok():
    st = rp.status(now=1000.0)
    assert st["state"] == "unknown"
    assert st["healthy"] is False, "no evidence must never read as healthy"


def test_a_stale_verdict_goes_unknown_rather_than_staying_ok():
    """The worst failure available here: reporting a four-day-old 'ok'."""
    rp.check(probe=_ok, now=1000.0)
    fresh = rp.status(now=1000.0 + rp.STALE_AFTER_SECS - 1)
    assert fresh["state"] == "ok"
    stale = rp.status(now=1000.0 + rp.STALE_AFTER_SECS + 1)
    assert stale["state"] == "unknown" and stale["healthy"] is False
    assert "stale" in stale["reason"]


def test_a_probe_that_raises_is_broken_not_ok():
    def _explode():
        raise RuntimeError("boom")
    st = rp.check(probe=_explode, now=1000.0)
    assert st["state"] == "broken" and st["healthy"] is False
    assert "probe_raised" in st["last_error"]


def test_an_unreadable_store_never_reports_healthy(monkeypatch):
    monkeypatch.setattr(rp, "_conn",
                        lambda conn=None: (_ for _ in ()).throw(sqlite3.Error("gone")))
    try:
        st = rp.status(now=1000.0)
    except Exception:
        pytest.fail("status() must not raise; an unreadable store is a closed door")
    assert st["healthy"] is False


# ── the message a human reads ───────────────────────────────────────────────
def test_the_summary_blames_the_dependency_not_owner_os():
    rp.check(probe=_ok, now=1000.0)
    rp.check(probe=_down, now=2000.0)
    line = rp.summary_line(rp.status(now=2000.0))
    assert "BROKEN" in line
    assert "seo" in line
    assert "core is separate" in line, \
        "the line must stop the reader concluding Owner OS is at fault"


def test_the_healthy_summary_is_unambiguous():
    rp.check(probe=_ok, now=1000.0)
    assert "OK" in rp.summary_line(rp.status(now=1000.0))


# ── the periodic watch: speak on CHANGE, never per tick ─────────────────────
class _Emit:
    def __init__(self): self.calls = []
    def __call__(self, source, type, **kw):
        self.calls.append({"source": source, "type": type, **kw}); return {"event_id": len(self.calls)}


def test_a_multi_day_outage_is_two_messages_not_hundreds():
    """The incident lasted four days. At a 120s tick that is 2880 chances to shout.

    This is the same noise failure removed from the stall doctor earlier the same day:
    a standing condition must announce itself once, not on every poll.
    """
    emit = _Emit()
    t = 1000.0
    rp.watch_once(probe=_ok, emit_fn=emit, now=t)          # healthy
    for i in range(40):                                     # ~80 minutes of outage
        t += 120
        rp.watch_once(probe=_down, emit_fn=emit, now=t)
    broken = [c for c in emit.calls if c["type"] == "reverse_path_broken"]
    assert len(broken) == 1, f"the outage was announced {len(broken)} times"
    t += 120
    rp.watch_once(probe=_ok, emit_fn=emit, now=t)           # recovery
    rec = [c for c in emit.calls if c["type"] == "reverse_path_recovered"]
    assert len(rec) == 1, f"recovery announced {len(rec)} times"


def test_the_broken_alert_carries_what_the_owner_needs():
    emit = _Emit()
    rp.watch_once(probe=_ok, emit_fn=emit, now=1000.0)
    rp.watch_once(probe=_down, emit_fn=emit, now=2000.0)
    a = [c for c in emit.calls if c["type"] == "reverse_path_broken"][0]
    assert a["severity"] == "critical" and a["owner_action_required"] is True
    assert "seo" in a["payload"]["depends_on"]
    assert a["payload"]["last_ok_at"], "must say since when"
    assert "core is separate" in a["action_taken"]


def test_a_steady_healthy_path_says_nothing_at_all():
    emit = _Emit()
    t = 1000.0
    for _ in range(20):
        t += 120
        rp.watch_once(probe=_ok, emit_fn=emit, now=t)
    assert [c for c in emit.calls if c["type"] == "reverse_path_broken"] == []
    # the first transition from "never probed" to ok is a recovery from unknown, once
    assert len([c for c in emit.calls if c["type"] == "reverse_path_recovered"]) <= 1


def test_a_flapping_path_reports_each_real_transition():
    """Suppression must not hide a path that is genuinely coming and going."""
    emit = _Emit()
    t = 1000.0
    for probe in (_ok, _down, _ok, _down, _ok):
        t += 120
        rp.watch_once(probe=probe, emit_fn=emit, now=t)
    assert len([c for c in emit.calls if c["type"] == "reverse_path_broken"]) == 2


def test_watch_once_reports_whether_it_changed():
    emit = _Emit()
    first = rp.watch_once(probe=_ok, emit_fn=emit, now=1000.0)
    again = rp.watch_once(probe=_ok, emit_fn=emit, now=1120.0)
    assert first["changed"] is True and again["changed"] is False
    assert again["previous_state"] == "ok"


def test_the_watch_loop_offloads_its_probe_to_a_thread():
    """The probe is a blocking socket read in the process serving the MCP control path.

    Two worker ticks were found running blocking work on the event loop earlier this
    same session; this asserts by parsing the body that this one does not join them.
    """
    import ast, inspect, textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(rp.watch_loop)))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "to_thread"]
    assert calls, "watch_loop runs its blocking probe on the event loop"


def test_a_clean_start_does_not_announce_a_recovery():
    """Found by the outage test emitting two recoveries on a run with one outage.

    unknown -> ok is what EVERY service start looks like. Calling it a recovery tells
    the owner something was fixed when nothing had broken — a restart-triggered lie,
    and the fastest way to teach someone to ignore the channel.
    """
    emit = _Emit()
    rp.watch_once(probe=_ok, emit_fn=emit, now=1000.0)
    assert [c for c in emit.calls if c["type"] == "reverse_path_recovered"] == []
    assert [c for c in emit.calls if c["type"] == "reverse_path_broken"] == []


def test_recovery_after_the_watch_was_interrupted_mid_outage_is_still_announced():
    """The opposite trap: suppressing unknown -> ok must not swallow a REAL recovery.

    If the watch stops during an outage its verdict goes stale, so the first probe after
    it resumes sees unknown -> ok. A failure is on record with no success after it, so
    that is genuine news and must be said.
    """
    emit = _Emit()
    rp.watch_once(probe=_ok, emit_fn=emit, now=1000.0)       # good
    rp.watch_once(probe=_down, emit_fn=emit, now=2000.0)     # breaks
    # ... watch stops; the verdict ages past STALE_AFTER_SECS ...
    later = 2000.0 + rp.STALE_AFTER_SECS + 60
    assert rp.status(now=later)["state"] == "unknown"
    rp.watch_once(probe=_ok, emit_fn=emit, now=later)        # resumes, and it is back
    rec = [c for c in emit.calls if c["type"] == "reverse_path_recovered"]
    assert len(rec) == 1, "a real recovery was swallowed by the stale-start guard"
