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

DISPLAY = os.getenv("COMPANION_DISPLAY", ":99")
POLL_SECS = int(os.getenv("COMPANION_POLL_SECS", "20"))


def _x(*args, timeout=15):
    return subprocess.run(["xdotool", *args], env={**os.environ, "DISPLAY": DISPLAY},
                          capture_output=True, text=True, timeout=timeout)


def _window() -> str:
    r = _x("search", "--onlyvisible", "--name", "Google Chrome")
    ids = [x for x in (r.stdout or "").split() if x.strip()]
    return ids[0] if ids else ""


def submit(phrase: str, conversation: str) -> bool:
    """Focus the composer and submit the fixed phrase. Never reads anything back."""
    wid = _window()
    if not wid:
        return False
    _x("windowactivate", "--sync", wid)
    time.sleep(1)
    # Navigate to the bound conversation so the phrase can never land in another chat.
    _x("key", "--window", wid, "ctrl+l"); time.sleep(0.5)
    _x("type", "--clearmodifiers", "--delay", "15", conversation); time.sleep(0.3)
    _x("key", "--clearmodifiers", "Return"); time.sleep(6)
    _x("mousemove", "640", "720", "click", "1"); time.sleep(1.5)
    _x("type", "--clearmodifiers", "--delay", "25", phrase); time.sleep(1)
    _x("key", "--clearmodifiers", "Return")
    return True


def main() -> None:
    from core import wake_bridge as wb
    while True:
        try:
            p = wb.pending_wake()
            if p.get("pending"):
                ok = submit(p["phrase"], p["conversation"])
                if ok:
                    # Acknowledge so the same event can never be submitted twice.
                    wb.acknowledge(p["event_id"])
                    print(f"submitted wake for event {p['event_id']}", flush=True)
                else:
                    print("no browser window; will retry", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"companion error: {str(e)[:160]}", flush=True)
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
