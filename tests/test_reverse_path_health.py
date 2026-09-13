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
