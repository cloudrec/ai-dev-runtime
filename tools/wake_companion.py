"""Wake companion — submits ONE fixed phrase into the active control chat.

It asks the bridge whether a wake is pending and does exactly what it is told. It makes no
decisions, reads no page content, parses no assistant output, and knows no conversation URL
of its own: the target comes from the rotatable pointer at submission time, so a rebind
between decision and delivery goes to the new chat rather than a stale one.

The ONLY text it can ever type is `wake_bridge.WAKE_PHRASE`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, "/root/ai-dev-runtime")
sys.path.insert(0, "/root/ai-dev-runtime/tools")

DISPLAY = os.getenv("COMPANION_DISPLAY", ":99")
POLL_SECS = int(os.getenv("COMPANION_POLL_SECS", "20"))


def _x(*args, timeout=15):
    return subprocess.run(["xdotool", *args], env={**os.environ, "DISPLAY": DISPLAY},
                          capture_output=True, text=True, timeout=timeout)


def _window() -> str:
    r = _x("search", "--onlyvisible", "--name", "Google Chrome")
    ids = [x for x in (r.stdout or "").split() if x.strip()]
    return ids[0] if ids else ""


def _frame_signature() -> str:
    """A cheap hash of the framebuffer. Used ONLY to detect that something changed — it is
    never decoded, parsed or inspected, so no page or chat content is read."""
    import hashlib
    try:
        r = subprocess.run(["import", "-window", "root", "png:-"],
                           env={**os.environ, "DISPLAY": DISPLAY},
                           capture_output=True, timeout=20)
        return hashlib.sha256(r.stdout or b"").hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def submit(phrase: str, conversation: str) -> bool:
    """Submit the fixed phrase into the bound conversation. Fail closed at every step.

    No fixed coordinates: focus is keyboard-only. Clicking a hardcoded pixel was the fragile
    part — a layout change silently sent the phrase nowhere, and a click cannot be verified.
    Escape dismisses any transient overlay and returns focus to the page; ChatGPT then routes
    typing to its composer, which is where a keyboard user would land too.

    Nothing is read back except a framebuffer HASH, used only to prove the screen changed.
    """
    wid = _window()
    if not wid:
        return False
    _x("windowactivate", "--sync", wid)
    time.sleep(1)

    # FAIL CLOSED: the window we are about to type into must actually be the focused one.
    active = (_x("getactivewindow").stdout or "").strip()
    if active != wid:
        print(f"refusing: chrome window {wid} is not focused (active={active or 'none'})",
              flush=True)
        return False

    # Navigate to the bound conversation so the phrase can never land in another chat.
    _x("key", "--window", wid, "ctrl+l"); time.sleep(0.5)
    _x("type", "--clearmodifiers", "--delay", "15", conversation); time.sleep(0.3)
    _x("key", "--clearmodifiers", "Return"); time.sleep(8)

    before = _frame_signature()
    _x("key", "--clearmodifiers", "Escape"); time.sleep(0.8)
    _x("type", "--clearmodifiers", "--delay", "25", phrase); time.sleep(1.5)

    # FAIL CLOSED: typing must have changed the screen. If the keystrokes went nowhere the
    # frame is identical, and pressing Enter would submit into the void.
    typed = _frame_signature()
    if before and typed and before == typed:
        print("refusing: typing did not change the screen — composer not focused", flush=True)
        _x("key", "--clearmodifiers", "ctrl+a"); _x("key", "--clearmodifiers", "Delete")
        return False

    _x("key", "--clearmodifiers", "Return"); time.sleep(4)

    # FAIL CLOSED: submission must change the screen again.
    after = _frame_signature()
    if typed and after and typed == after:
        print("refusing to claim success: screen unchanged after Return", flush=True)
        return False
    return True


def main() -> None:
    from core import wake_bridge as wb
    while True:
        try:
            p = wb.pending_wake()
            if p.get("pending"):
                # Structural CDP path first: it can VERIFY. The keyboard path cannot
                # confirm a keystroke reached the composer, which is how three earlier
                # wakes reported success without delivering anything.
                res = {"ok": False, "reason": "not_attempted"}
                try:
                    from cdp_composer import submit_phrase
                    res = submit_phrase(p["conversation"], p["phrase"],
                                        source="companion", event_id=p["event_id"])
                    ok = bool(res.get("ok"))
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "reason": f"cdp_unavailable:{type(e).__name__}"}
                    ok = False
                if ok:
                    # Acknowledge ONLY on verified delivery. Anything else leaves the wake
                    # pending, which is what makes a failed submission retryable instead of
                    # silently consumed.
                    wb.acknowledge(p["event_id"])
                    print(f"delivered wake for event {p['event_id']}: {res.get('reason')}",
                          flush=True)
                else:
                    # The old text claimed "no browser window" for every failure, including
                    # a plain cooldown refusal, which sent three investigations the wrong way.
                    print(f"not delivered for event {p['event_id']}; stays pending "
                          f"({res.get('reason')})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"companion error: {str(e)[:160]}", flush=True)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
