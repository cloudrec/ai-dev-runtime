"""Deterministic in-process canary harness (no live tmux, no real agent).

Simulates the full P4 actuation path — lease → deliver → consume → verify → CTO event —
against a fake pane, so the pipeline can be proven offline without commanding a real agent
and without enabling any live flag. The harness ARMS the actuator (ENABLED + a single-agent
canary allowlist) only for the duration of one simulated run, in-process, and RESTORES the
module globals afterwards — the deployed service (a separate process reading env at import)
stays dormant regardless.

Use for simulated PASS evidence; it is NOT a substitute for the still-gated real-agent
proof (which requires a confidently-idle, non-excluded live agent).
"""
from __future__ import annotations

import contextlib

from core.control_plane import actuator as act
from core.control_plane import api as cp


@contextlib.contextmanager
def armed(target: str):
    """Temporarily arm the actuator for exactly one canary target, then restore. Safe:
    mutates only in-process module globals; the live service is unaffected."""
    e0, c0 = act.ENABLED, act.CANARY_AGENTS
    act.ENABLED = True
    act.CANARY_AGENTS = frozenset({target})
    try:
        yield
    finally:
        act.ENABLED, act.CANARY_AGENTS = e0, c0


class SimulatedPane:
    """A ctrl compatible with the Actuator. Models a real Claude pane: idle at a clean
    prompt, then on delivery it consumes the line, advances the conversation mtime, and
    transitions to working. Configurable to model negative cases."""

    def __init__(self, *, active_marker: bool = False, will_consume: bool = True,
                 consume_on_enter: int = 1, conv: str = "m0"):
        self.will_consume = will_consume
        self.consume_on_enter = consume_on_enter
        self.sends = 0
        self.s = {
            "pending": "",
            "conv": conv,
            "state": "working" if active_marker else "idle",
            "tail": "✶ Pouncing… (8s · thinking)" if active_marker else "❯ ",
            "enters": 0,
        }

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"], "conv_mtime": self.s["conv"],
                "state": self.s["state"], "activity": self.s["tail"]}

    def _consume(self):
        self.s["enters"] += 1
        if self.will_consume and self.s["enters"] >= self.consume_on_enter:
            self.s.update(pending="", conv="m1", state="working",
                          tail="✶ Working… (2s · ↑ 100 tokens)")
        return 0

    def enter(self, target):
        return self._consume()

    def robust_submit(self, target, text):
        self.s["pending"] = text
        return self._consume() == 0

    def send(self, target, text, idem):
        self.sends += 1
        self.s["pending"] = text
        return {"submitted": self._consume() == 0}


def run_canary(target: str, action: str, *, lease=None, pane: SimulatedPane = None,
               conversation_id: str = "cv-sim", cwd: str = "/sim/project",
               sleep=lambda _: None) -> dict:
    """One simulated canary actuation. Returns {result, pane, lease}. Arms the actuator
    only for `target` for the duration of the call."""
    pane = pane if pane is not None else SimulatedPane()
    with armed(target):
        lease = lease or cp.acquire_lease(f"agent:{target}", "sim_canary", ttl_secs=120)
        result = act.actuate(target=target, action_text=action, controller="sim_canary",
                             conversation_id=conversation_id, cwd=cwd, lease=lease, ctrl=pane,
                             sleep=sleep)
    return {"result": result, "pane": pane, "lease": lease}
