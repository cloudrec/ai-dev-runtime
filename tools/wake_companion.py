"""Wake companion — submits ONE fixed phrase into the active control chat.

It asks the bridge whether a wake is pending and does exactly what it is told. It makes no
decisions, reads no page content, parses no assistant output, and knows no conversation URL
of its own: the target comes from the rotatable pointer at submission time, so a rebind
between decision and delivery goes to the new chat rather than a stale one.

The ONLY text it can ever type is `wake_bridge.WAKE_PHRASE`, and the ONLY way it can type
it is `cdp_composer.submit_phrase`, which navigates the tab to the BOUND conversation and
verifies delivery structurally. There is deliberately no synthetic-keyboard fallback: that
path typed into whatever window happened to be focused, which is exactly how a phrase could
land in the wrong chat, and it could never verify a keystroke arrived.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "/root/ai-dev-runtime")
sys.path.insert(0, "/root/ai-dev-runtime/tools")

POLL_SECS = int(os.getenv("COMPANION_POLL_SECS", "20"))


def tick(wb) -> dict:
    """One poll: ask the bridge, deliver if told to, acknowledge ONLY on verified delivery.

    Split from the loop so the behaviour is testable. Returns what happened, always with a
    reason — the old text claimed "no browser window" for every failure, including a plain
    cooldown refusal, which sent three investigations the wrong way.
    """
    p = wb.pending_wake()
    if not p.get("pending"):
        return {"acted": False, "reason": p.get("reason", "nothing_pending")}
    # The conversation used is the one the bridge resolved THIS tick from the rotatable
    # pointer. It is never cached across ticks, so a rebind takes effect on the next poll.
    res = {"ok": False, "reason": "not_attempted"}
    try:
        from cdp_composer import submit_phrase
        res = submit_phrase(p["conversation"], p["phrase"],
                            source="companion", event_id=p["event_id"],
                            actionable=bool(p.get("actionable")))
        ok = bool(res.get("ok"))
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "reason": f"cdp_unavailable:{type(e).__name__}"}
        ok = False
    if ok:
        # Acknowledge ONLY on verified delivery. Anything else leaves the wake pending,
        # which is what makes a failed submission retryable instead of silently consumed.
        wb.acknowledge(p["event_id"])
        print(f"delivered wake for event {p['event_id']} to {p['conversation']}: "
              f"{res.get('reason')}", flush=True)
    else:
        print(f"not delivered for event {p['event_id']}; stays pending "
              f"({res.get('reason')})", flush=True)
    return {"acted": True, "ok": ok, "event_id": p["event_id"],
            "conversation": p["conversation"], "reason": res.get("reason")}


def main() -> None:
    from core import wake_bridge as wb
    while True:
        try:
            tick(wb)
        except Exception as e:  # noqa: BLE001
            print(f"companion error: {str(e)[:160]}", flush=True)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
