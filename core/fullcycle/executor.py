"""Technology-agnostic executor contract.

Every executor — Claude Code in a tmux pane today, Qwen or Codex CLI later, local or
over SSH — is driven through the same four verbs and the same two files:

  submit(envelope, key)  hand the task over; idempotent per key
  poll(handle)           working | done | crashed | waiting_permission | unknown
  collect(handle)        the agent's result file, parsed
  restart(handle)        bring a crashed agent back, ONLY if this adapter owns it

The envelope is written as JSON next to the project (`.fullcycle/tasks/`); the agent is
told, in one line, to read it and to write its result JSON to `result_path`. Nothing in
the contract depends on what the agent is: any CLI agent that can read a file and write
one can take part. The result file is the agent's CLAIM; the engine verifies it (checks,
git diff against scope) before anything is reported done.

Result file: {"task_id", "attempt", "status": "done"|"blocked", "summary": str,
              "changed_files": [str], "question"?: str}
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Optional

WORKING = "working"
DONE = "done"
CRASHED = "crashed"
WAITING_PERMISSION = "waiting_permission"
UNKNOWN = "unknown"
OBSERVATIONS = frozenset({WORKING, DONE, CRASHED, WAITING_PERMISSION, UNKNOWN})


class ExecutorUnavailable(RuntimeError):
    """The executor exists as a name but may not be dispatched to (untested/TODO)."""


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    attempt: int
    project_id: str
    workdir: str
    goal: str
    acceptance_criteria: tuple
    scope: tuple = ()
    do_not_touch: tuple = ()
    feedback: str = ""
    envelope_path: str = ""
    result_path: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["result_format"] = {"task_id": self.task_id, "attempt": self.attempt,
                              "status": "done | blocked", "summary": "str",
                              "changed_files": ["path relative to workdir"],
                              "question": "only when status=blocked"}
        return json.dumps(d, indent=1)

    def prompt(self) -> str:
        """One line: a multi-line paste is where tmux delivery goes wrong."""
        return (f"[Owner OS task {self.task_id} attempt {self.attempt}] Read the task "
                f"envelope {self.envelope_path} and do exactly that work inside "
                f"{self.workdir}, touching only its scope. When finished (or blocked), "
                f"write the result JSON described in the envelope to {self.result_path}.")


@dataclass
class Observation:
    state: str
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in OBSERVATIONS:
            raise ValueError(f"unknown observation {self.state!r}")


class ExecutorAdapter(ABC):
    name: str = "abstract"
    #: can this adapter restart its own agent after a crash?
    owns_agent: bool = False

    @abstractmethod
    def health(self) -> dict: ...

    @abstractmethod
    def submit(self, env: TaskEnvelope, idempotency_key: str) -> dict:
        """{ok: bool, already: bool, reason: str, handle: dict}"""

    @abstractmethod
    def poll(self, handle: dict) -> Observation: ...

    @abstractmethod
    def collect(self, handle: dict) -> Optional[dict]: ...

    def restart(self, handle: dict) -> dict:
        return {"ok": False, "reason": "adapter_does_not_own_agent"}

    def quiescent(self, handle: dict) -> Optional[bool]:
        """Is the agent provably NOT working on anything (idle at its prompt, or its
        process gone)? True only on positive evidence; False = active; None = cannot
        tell. The engine resends after a stall only on True."""
        return None

    def idle_evidence(self, handle: dict) -> tuple:
        """(quiescent, fingerprint): `quiescent` as above, plus an opaque fingerprint of
        what the agent's screen shows (None = not available). The engine needs the agent
        idle CONTINUOUSLY with an unchanged fingerprint for QUIET_CONFIRM_SECS before it
        acts on "idle" — a single reading can be a misclassified active pane."""
        return self.quiescent(handle), None


# ── executor profiles ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Profile:
    """How to start an agent CLI in a pane. `tested` is a fact about THIS codebase:
    a profile no test has driven end to end is refused at dispatch."""
    name: str
    launch_argv: tuple
    tested: bool
    note: str = ""


PROFILES = {
    "claude": Profile("claude", ("claude",), tested=True,
                      note="Claude Code CLI; live agents are reached via agent_control"),
    "qwen": Profile("qwen", ("qwen",), tested=False,
                    note="TODO: Qwen Code CLI — no end-to-end run recorded yet"),
    "codex": Profile("codex", ("codex",), tested=False,
                     note="TODO: OpenAI Codex CLI — no end-to-end run recorded yet"),
    # The fixture agent used by the integration tests and the demo; never a product.
    "fixture": Profile("fixture", (), tested=True, note="tests/fixtures/fullcycle_demo"),
}


def profile(name: str) -> Profile:
    p = PROFILES.get(name)
    if p is None:
        raise ExecutorUnavailable(f"unknown executor profile {name!r}")
    if not p.tested:
        raise ExecutorUnavailable(f"executor {name!r} is not tested yet: {p.note}")
    return p
