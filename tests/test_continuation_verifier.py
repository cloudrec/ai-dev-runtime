"""Fail-closed verification of a NATIVE continuation.

The mechanism exists because zero-ping DELIVERY is proven and zero-ping EFFECTIVENESS is
not: `closed_loop_wake.register_delivery` only tracks companion deliveries, so a native
continuation is never registered and nothing shows it produced work. These tests pin the
two properties that make the answer trustworthy — a ChatGPT delivery can never be
credited to the supervisor, and absence of evidence is recorded as failure — and the
dormancy that keeps the whole thing inert until an owner names a canary.
"""
from __future__ import annotations

from core import continuation_verifier as cv

T0 = 1000.0
D0 = "digest-at-continuation"


def _classify(**kw):
    base = dict(t0=T0, digest_at_t0=D0, turns=[], deliveries=[],
                now=T0 + 10, timeout_secs=600)
    base.update(kw)
    return cv.classify_continuation(**base)


# ── a ChatGPT delivery must never be credited to the supervisor ────────────

def test_a_delivery_in_the_interval_makes_the_sample_unattributable():
    """The chat may have done the work. Crediting it to the supervisor is exactly the
    confusion this mechanism exists to resolve."""
    out = _classify(turns=[{"ts": T0 + 60, "digest": "new"}],
                    deliveries=[{"ts": T0 + 30}], now=T0 + 90)
    assert out["verdict"] == cv.UNATTRIBUTABLE
    assert out["deliveries_in_interval"] == 1


def test_a_delivery_after_the_turn_does_not_taint_the_sample():
    """Only the interval between the continuation and the turn can explain the turn."""
    out = _classify(turns=[{"ts": T0 + 60, "digest": "new"}],
                    deliveries=[{"ts": T0 + 120}], now=T0 + 200)
    assert out["verdict"] == cv.VERIFIED


def test_a_delivery_before_the_continuation_does_not_taint_the_sample():
    out = _classify(turns=[{"ts": T0 + 60, "digest": "new"}],
                    deliveries=[{"ts": T0 - 30}], now=T0 + 90)
    assert out["verdict"] == cv.VERIFIED


def test_a_delivery_exactly_at_the_turn_still_taints_it():
    """The boundary belongs to the sceptical side."""
    out = _classify(turns=[{"ts": T0 + 60, "digest": "new"}],
                    deliveries=[{"ts": T0 + 60}], now=T0 + 90)
    assert out["verdict"] == cv.UNATTRIBUTABLE


# ── absence of evidence fails closed ───────────────────────────────────────

def test_no_turn_before_the_timeout_is_recorded_as_failure():
    out = _classify(turns=[], now=T0 + 601)
    assert out["verdict"] == cv.UNVERIFIED
    assert out["reason"] == "no_qualifying_turn_before_timeout"


def test_no_turn_inside_the_window_is_pending_and_never_success():
    out = _classify(turns=[], now=T0 + 60)
    assert out["verdict"] == cv.PENDING
    assert out["verdict"] != cv.VERIFIED


def test_a_turn_with_the_UNCHANGED_digest_proves_nothing():
    """The same stalled frame observed twice is not a turn boundary."""
    out = _classify(turns=[{"ts": T0 + 60, "digest": D0}], now=T0 + 700)
    assert out["verdict"] == cv.UNVERIFIED


def test_a_turn_that_predates_the_continuation_proves_nothing():
    out = _classify(turns=[{"ts": T0 - 5, "digest": "new"}], now=T0 + 700)
    assert out["verdict"] == cv.UNVERIFIED


def test_the_earliest_qualifying_turn_is_the_one_judged():
    """A later clean turn must not rescue a tainted first one."""
    out = _classify(turns=[{"ts": T0 + 30, "digest": "a"}, {"ts": T0 + 300, "digest": "b"}],
                    deliveries=[{"ts": T0 + 10}], now=T0 + 400)
    assert out["verdict"] == cv.UNATTRIBUTABLE


# ── the streak rule ────────────────────────────────────────────────────────

def test_consecutive_successes_are_required():
    s = [{"verdict": cv.VERIFIED}] * 2
    assert cv.rollup(s, required=3)["proven"] is False
    assert cv.rollup(s + [{"verdict": cv.VERIFIED}], required=3)["proven"] is True


def test_any_unverified_resets_the_streak():
    s = [{"verdict": cv.VERIFIED}] * 2 + [{"verdict": cv.UNVERIFIED}] + \
        [{"verdict": cv.VERIFIED}]
    r = cv.rollup(s, required=3)
    assert r["streak"] == 1 and r["proven"] is False


def test_an_unattributable_sample_is_discarded_not_counted():
    """Discarded means neutral: it neither advances the streak nor destroys it."""
    s = [{"verdict": cv.VERIFIED}, {"verdict": cv.UNATTRIBUTABLE}, {"verdict": cv.VERIFIED}]
    assert cv.rollup(s, required=2)["streak"] == 2


def test_pending_is_neutral_too():
    s = [{"verdict": cv.VERIFIED}, {"verdict": cv.PENDING}, {"verdict": cv.VERIFIED}]
    assert cv.rollup(s, required=2)["proven"] is True


def test_no_samples_is_not_proof():
    assert cv.rollup([], required=3)["proven"] is False


def test_required_is_never_less_than_one():
    assert cv.rollup([], required=0)["proven"] is False


# ── dormancy: inert until an owner names a canary ──────────────────────────

def test_it_is_dormant_with_no_canary_selected(monkeypatch):
    monkeypatch.delenv(cv.CANARY_ENV, raising=False)
    assert cv.is_active() is False
    out = cv.observe(target="anything")
    assert out["active"] is False and out["verdict"] == cv.DORMANT


def test_observe_can_never_return_success_while_dormant(monkeypatch):
    monkeypatch.delenv(cv.CANARY_ENV, raising=False)
    out = cv.observe(target="whatever", t0=T0, digest_at_t0=D0,
                     turns=[{"ts": T0 + 60, "digest": "new"}], deliveries=[],
                     now=T0 + 90)
    assert out["verdict"] != cv.VERIFIED


def test_a_non_canary_target_is_ignored_even_once_active(monkeypatch):
    monkeypatch.setenv(cv.CANARY_ENV, "the-canary:0.0")
    out = cv.observe(target="some-other-agent:0.0")
    assert out["active"] is False and out["verdict"] == cv.DORMANT


def test_the_named_canary_is_judged_once_active(monkeypatch):
    monkeypatch.setenv(cv.CANARY_ENV, "the-canary:0.0")
    out = cv.observe(target="the-canary:0.0", t0=T0, digest_at_t0=D0,
                     turns=[{"ts": T0 + 60, "digest": "new"}], deliveries=[],
                     now=T0 + 90)
    assert out["active"] is True and out["verdict"] == cv.VERIFIED


def test_the_repo_ships_with_no_canary_selected():
    """Shipping this armed would activate an owner-deferred decision."""
    import os
    assert not (os.getenv(cv.CANARY_ENV) or "").strip(), \
        "NATIVE_CANARY_TARGET must be unset until the owner names one"


def test_the_module_writes_nothing_and_opens_no_database():
    """Dormant means dormant: no store import, no connect, no execute."""
    import inspect
    src = inspect.getsource(cv)
    for forbidden in ("sqlite3", "connect(", "INSERT", "UPDATE", "execute(",
                      "control_plane.store", "emit("):
        assert forbidden not in src, f"{forbidden} has no business in dormant instrumentation"
