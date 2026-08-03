"""Canonical multi-signal agent state estimation.

A single pane snapshot is NOT enough: Claude briefly shows the input prompt between the
model finishing and the next render, so prompt/layout heuristics alone classify an actively
thinking agent as idle (the `Pouncing… (8s · thinking)` false-idle). This estimator fuses
several signals and NEVER declares idle from one ambiguous snapshot:

  * active-execution markers in the tail (spinner timer / `· thinking` / token counter /
    `esc to interrupt`) — reliable ACTIVE evidence;
  * conversation .jsonl mtime movement within the active window — the model is writing;
  * process CPU activity (best-effort);
  * pending input line;
  * an idle-dwell threshold (idle must be sustained AND the conversation quiet).

Precedence: any ACTIVE signal → working. Otherwise a KNOWN non-idle base (waiting/blocked/
dead) passes through. A bare idle layout is only trusted as `idle` when it is quiet AND
sustained; conflicting or unsettled evidence becomes `unknown` (never idle), so the actuator
can refuse to command a possibly-working agent.
"""
from __future__ import annotations

import os
from typing import Optional

from core.agent_control import _STATE_ACTIVE_RUN_RE

# conversation mtime moving within this window = the model is actively producing output
ACTIVE_CONV_WINDOW_SECS = int(os.getenv("CP_ACTIVE_CONV_WINDOW_SECS", "10"))
IDLE_DWELL_SECS = int(os.getenv("CP_IDLE_DWELL_SECS", "15"))
_KNOWN_NONIDLE = ("waiting_owner", "waiting_input", "externally_blocked", "dead", "failed",
                  "shell_running", "working")


def has_active_marker(tail: str) -> bool:
    return bool(_STATE_ACTIVE_RUN_RE.search(tail or ""))


def estimate(*, base_state: str, tail: str = "", conv_moved: bool = False,
             cpu_active: bool = False, pending: str = "", idle_confirmed: bool = False) -> dict:
    """Pure estimate. Returns {state, reason, active, signals}. `conv_moved`,
    `cpu_active`, `idle_confirmed` are computed by the live wrapper / caller."""
    active_marker = has_active_marker(tail)
    signals = {"active_marker": active_marker, "conv_moved": conv_moved,
               "cpu_active": cpu_active, "pending": bool(pending and pending.strip()),
               "base_state": base_state, "idle_confirmed": idle_confirmed}

    if active_marker or conv_moved or cpu_active:
        return {"state": "working", "reason": "active_evidence", "active": True, "signals": signals}

    if base_state in _KNOWN_NONIDLE and base_state not in ("working",):
        return {"state": base_state, "reason": "known_base", "active": False, "signals": signals}

    if signals["pending"]:
        return {"state": "waiting_input", "reason": "pending_input", "active": False, "signals": signals}

    # bare idle layout — trust it ONLY when quiet AND sustained
    if base_state in ("idle", "unknown"):
        if not idle_confirmed:
            return {"state": "unknown", "reason": "idle_unconfirmed_or_conflicting",
                    "active": False, "signals": signals}
        return {"state": "idle", "reason": "quiet_and_sustained", "active": False, "signals": signals}

    return {"state": base_state or "unknown", "reason": "passthrough", "active": False,
            "signals": signals}


# ── live signal gathering ────────────────────────────────────────────────────
def _proc_cpu_active(pid: Optional[int], sample_secs: float = 0.25) -> bool:
    """Best-effort: is the process burning CPU right now? Two /proc/<pid>/stat samples.
    Returns False on any error (absence is not evidence of activity)."""
    if not pid:
        return False
    try:
        def _jiffies():
            with open(f"/proc/{pid}/stat") as f:
                parts = f.read().split()
            return int(parts[13]) + int(parts[14])          # utime + stime
        import time as _t
        a = _jiffies(); _t.sleep(sample_secs); b = _jiffies()
        return (b - a) > 0
    except Exception:  # noqa: BLE001
        return False


def estimate_live(target: str, *, prev_conv_mtime: Optional[str] = None,
                  idle_since_ts: Optional[float] = None, now_ts: Optional[float] = None) -> dict:
    """Gather live signals for `target` and estimate. Read-only."""
    import time
    from datetime import datetime
    from core import agent_control as ac
    now_ts = now_ts if now_ts is not None else time.time()
    st = ac.agent_status(target)
    tail = st.get("recent_activity") or ""
    base = st.get("state")
    pending = ac.pending_input_text(target)
    cwd = st.get("cwd") or ""
    conv = (ac.conversation_evidence(cwd) or {}).get("latest") or {}
    cur_mtime = conv.get("modified_at")
    conv_moved = False
    if cur_mtime:
        try:
            mt = datetime.fromisoformat(cur_mtime).timestamp()
            conv_moved = (now_ts - mt) < ACTIVE_CONV_WINDOW_SECS
        except Exception:  # noqa: BLE001
            conv_moved = False
    cpu_active = _proc_cpu_active(st.get("claude_pid"))
    idle_confirmed = (base == "idle" and not conv_moved
                      and idle_since_ts is not None and (now_ts - idle_since_ts) >= IDLE_DWELL_SECS)
    out = estimate(base_state=base, tail=tail, conv_moved=conv_moved, cpu_active=cpu_active,
                   pending=pending, idle_confirmed=idle_confirmed)
    out["conv_mtime"] = cur_mtime
    out["base_state"] = base
    return out
