"""Fail-closed verification that a NATIVE continuation actually produced work.

DORMANT BY CONSTRUCTION. Nothing here runs until a human names a canary target in
`NATIVE_CANARY_TARGET`; with it unset, `is_active()` is False, `observe()` is a no-op,
and this module writes nothing anywhere. Selecting the canary is an owner decision that
the handoff records as declined ("no canary selected; P4 verified-continuation remains
deferred"), so the mechanism ships inert and waits.

WHY IT EXISTS. Zero-ping DELIVERY is proven: the supervisor issues continuations to
existing agents unprompted — 12 in three hours on 2026-09-04, `agent_created=False`,
five of them from the idle sweep with no triggering event at all. Zero-ping
EFFECTIVENESS is not, and the reason is structural rather than a bug:
`closed_loop_wake.register_delivery` tracks "a wake that a companion DELIVERY just
confirmed landed (a real ChatGPT user turn)". A native continuation never passes through
companion delivery, so it is never registered, and no closed-loop line references any
native-continuation event id. Silence afterwards is consistent with an agent working and
is not evidence that it is.

THE EVIDENCE THIS USES INSTEAD is the native Stop hook, which is already live and has no
browser in its path: a NEW `agent_turn_stopped` for the target whose digest DIFFERS from
the one at continuation time. A turn boundary proves the agent ran; the digest change
proves it is not the same stalled frame re-observed.

THREE RULES, and each exists because the obvious implementation gets it wrong:

* ATTRIBUTION. If any wake_delivery to that target's route landed between the
  continuation and the observed turn, the sample is `unattributable` and discarded — the
  chat may have done the work. Without this the mechanism would credit ChatGPT delivery
  to the supervisor, which is precisely the confusion it exists to resolve.
* FAIL CLOSED. No qualifying turn inside the window records `continuation_unverified`.
  Absence of evidence is recorded as failure, never as silence and never as success.
* PENDING IS NOT A VERDICT. Before the timeout there is simply no answer yet, and that
  state must never be read as success.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

CANARY_ENV = "NATIVE_CANARY_TARGET"
DEFAULT_TIMEOUT_SECS = int(os.getenv("NATIVE_CANARY_TIMEOUT_SECS", "600"))
DEFAULT_REQUIRED = int(os.getenv("NATIVE_CANARY_REQUIRED", "3"))

VERIFIED = "verified"
UNATTRIBUTABLE = "unattributable"
UNVERIFIED = "continuation_unverified"
PENDING = "pending"
DORMANT = "dormant"


def canary_target() -> str:
    """The single owner-named canary, or "" when none has been chosen."""
    return (os.getenv(CANARY_ENV) or "").strip()


def is_active() -> bool:
    return bool(canary_target())


def classify_continuation(*, t0: float, digest_at_t0: str,
                          turns: Sequence[dict], deliveries: Sequence[dict],
                          now: float,
                          timeout_secs: int = DEFAULT_TIMEOUT_SECS) -> dict:
    """One sample's verdict. Pure — no I/O, no clock of its own, no writes.

    `turns`      `agent_turn_stopped` records for the target: {"ts": float, "digest": str}
    `deliveries` wake_delivery rows to that target's route: {"ts": float}
    """
    qualifying = sorted(
        (t for t in turns
         if float(t.get("ts", 0)) > t0 and (t.get("digest") or "") != digest_at_t0),
        key=lambda t: float(t["ts"]))
    if not qualifying:
        # Fail closed. A window that has expired with no turn is a FAILURE, and one that
        # has not expired yet is not an answer — neither may read as success.
        if now - t0 >= timeout_secs:
            return {"verdict": UNVERIFIED, "reason": "no_qualifying_turn_before_timeout",
                    "waited_secs": round(now - t0)}
        return {"verdict": PENDING, "reason": "window_still_open",
                "waited_secs": round(now - t0)}
    turn = qualifying[0]
    interfering = [d for d in deliveries
                   if t0 < float(d.get("ts", 0)) <= float(turn["ts"])]
    if interfering:
        return {"verdict": UNATTRIBUTABLE,
                "reason": "wake_delivery_landed_in_the_interval",
                "deliveries_in_interval": len(interfering),
                "turn_ts": float(turn["ts"])}
    return {"verdict": VERIFIED, "reason": "new_turn_boundary_with_changed_digest",
            "turn_ts": float(turn["ts"]),
            "latency_secs": round(float(turn["ts"]) - t0)}


def rollup(samples: Sequence[dict], *, required: int = DEFAULT_REQUIRED) -> dict:
    """Consecutive ATTRIBUTABLE successes. Any `continuation_unverified` resets the
    streak; an `unattributable` sample is discarded, so it neither advances nor resets;
    `pending` is not a sample yet. Fail closed: no samples is `proven=False`."""
    streak = 0
    for s in samples:
        v = s.get("verdict")
        if v == VERIFIED:
            streak += 1
        elif v == UNVERIFIED:
            streak = 0
        # UNATTRIBUTABLE and PENDING are deliberately neutral
    return {"streak": streak, "required": max(1, required),
            "proven": streak >= max(1, required)}


def observe(*, target: str = "", **kwargs) -> dict:
    """Entry point. A no-op while no canary is named — it writes nothing, reads no
    database, and can never return a success verdict."""
    canary = canary_target()
    if not canary:
        return {"active": False, "verdict": DORMANT,
                "reason": "no canary selected; owner decision outstanding"}
    if target and target != canary:
        return {"active": False, "verdict": DORMANT,
                "reason": f"target is not the canary ({canary})"}
    return dict(classify_continuation(**kwargs), active=True, target=canary)
