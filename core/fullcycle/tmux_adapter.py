"""The live executor: an EXISTING agent in a tmux pane, reached through core.agent_control.

Every send passes the policy already in force there (allowlist / grants / payment
hard-deny, refusal while the agent is mid-turn, idempotency keys, audit). It never
creates, restarts or stops an agent: a crash is reported, and the engine blocks the task
for the owner. agent_control stays the ONLY live module that types into a pane, and
pane creation stays behind agent_creation_policy's creation guard
(tests/test_agent_automation_policy.py, tests/test_agent_creation_adversarial.py).

An adapter that STARTS its own agent pane (needed for remote hosts, and for Qwen/Codex)
is deliberately not here: it would create agents outside the creation guard. The tests
use one on a private socket with a non-agent stand-in process
(tests/fixtures/fullcycle_demo/private_tmux.py). Bringing that into core is a TODO that
must go through the creation guard and the owner's creation approval.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from core import permission_resolver as _pr
from core.fullcycle.executor import (CRASHED, DONE, UNKNOWN, WAITING_PERMISSION,
                                     WORKING, ExecutorAdapter, Observation, TaskEnvelope)

def parse_result(text: Optional[str], handle: dict) -> Optional[dict]:
    """The agent's result, or None when absent, unparsable, or for another attempt
    (a late file from attempt 1 must never close attempt 2)."""
    if not text:
        return None
    try:
        r = json.loads(text)
    except ValueError:
        return None
    if not isinstance(r, dict) or r.get("task_id") != handle.get("task_id") \
            or r.get("attempt") != handle.get("attempt"):
        return None
    if r.get("status") not in ("done", "blocked", "context_exhausted"):
        return None
    files = r.get("changed_files") or []
    r["changed_files"] = [f for f in files if isinstance(f, str)]
    r["summary"] = str(r.get("summary") or "")[:4000]
    if r["status"] == "context_exhausted":
        r["handoff"], r["handoff_problem"] = clean_handoff(r.get("handoff"))
    return r


HANDOFF_MAX = 8000
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_handoff(raw) -> tuple:
    """(text, problem). A compact handoff is plain text up to HANDOFF_MAX characters.
    Never silently altered: control characters are removed and an over-long one is cut,
    and both are named in `problem`; a missing/non-text/empty one yields "" + problem.
    A malformed handoff still reaches the tech lead — it is reported, not dropped."""
    if raw is None:
        return "", "handoff_missing"
    if not isinstance(raw, str):
        return "", "handoff_not_text"
    text = _CTRL.sub("", raw)
    problems = ["handoff_control_chars_removed"] if text != raw else []
    text = text.strip()
    if not text:
        return "", "handoff_empty"
    if len(text) > HANDOFF_MAX:
        text = text[:HANDOFF_MAX]
        problems.append(f"handoff_truncated_to_{HANDOFF_MAX}")
    return text, ",".join(problems)


def _permission(tail: str) -> Optional[Observation]:
    if _pr.is_permission_prompt(tail):
        return Observation(WAITING_PERMISSION,
                           {"command": _pr.extract_pending_command(tail) or ""})
    return None


class AgentControlAdapter(ExecutorAdapter):
    """An existing live agent; files are on this host."""
    name = "agent_control"
    owns_agent = False

    def __init__(self, target: str, *, actor: str = "fullcycle:engine", host=None, ac=None):
        from core.fullcycle.hosts import LocalHost
        if ac is None:
            from core import agent_control as ac
        self.target, self.actor, self.ac = target, actor, ac
        self.host = host or LocalHost()

    def health(self) -> dict:
        try:
            st = self.ac.agent_status(self.target, actor=self.actor)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        return {"ok": bool(st.get("alive") and st.get("is_agent")), "state": st.get("state")}

    def submit(self, env: TaskEnvelope, idempotency_key: str) -> dict:
        handle = {"target": self.target, "task_id": env.task_id, "attempt": env.attempt,
                  "envelope": env.envelope_path, "result": env.result_path,
                  "key": idempotency_key}
        self.host.write_file(env.envelope_path, env.to_json())
        try:
            r = self.ac.agent_send(self.target, env.prompt(), idempotency_key=idempotency_key,
                                   actor=self.actor, source="fullcycle")
        except Exception as e:  # noqa: BLE001 — AgentControlError carries the refusal
            return {"ok": False, "already": False, "reason": f"refused:{e}", "handle": handle}
        if r.get("duplicate"):
            # agent_control answers a reused key from its delivery record without
            # touching the pane; "delivered" is False there by design
            return {"ok": bool(r.get("submitted")), "already": True,
                    "reason": "already_sent" if r.get("submitted") else "prior_send_failed",
                    "handle": handle}
        if r.get("delivered") or r.get("submitted"):
            return {"ok": True, "already": False, "reason": "sent", "handle": handle}
        return {"ok": False, "already": False,
                "reason": f"refused:{r.get('refused') or r.get('reason') or 'not_delivered'}",
                "handle": handle}

    def poll(self, handle: dict) -> Observation:
        if parse_result(self.host.read_file(handle["result"]), handle):
            return Observation(DONE)
        try:
            st = self.ac.agent_status(self.target, actor=self.actor)
        except Exception as e:  # noqa: BLE001
            return Observation(UNKNOWN, {"reason": str(e)})
        if not st.get("alive") or not st.get("is_agent"):
            return Observation(CRASHED, {"state": st.get("state")})
        return _permission(st.get("recent_activity") or "") or Observation(WORKING)

    def collect(self, handle: dict) -> Optional[dict]:
        return parse_result(self.host.read_file(handle["result"]), handle)

    # agent_control.classify_state: idle at the prompt, or no live agent process
    _QUIESCENT = ("waiting_input", "dead", "stale")
    _ACTIVE = ("working", "shell_running", "waiting_owner")

    def quiescent(self, handle: dict) -> Optional[bool]:
        try:
            st = self.ac.agent_status(self.target, actor=self.actor)
        except Exception:  # noqa: BLE001 — unknown is not idle
            return None
        state = st.get("state")
        if state in self._QUIESCENT:
            return True
        if state in self._ACTIVE:
            return False
        return None
