"""Shared set-up for the full-cycle tests and `tools/fullcycle_demo.py`.

Everything lives under one scratch directory: a copy of the tinycalc project (its own
git repo), a private tmux socket, the fake agent's scenario/log, and — set by the
caller — a private control-plane DB. Nothing here can reach a live project, pane or DB.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))

SOLUTIONS = {
    "add": {"tinycalc/add.py": "def add(a, b):\n    return a + b\n"},
    "mul": {"tinycalc/mul.py": "def mul(a, b):\n    return a * b\n"},
    "cli": {"tinycalc/cli.py": (
        "import sys\nfrom tinycalc.add import add\nfrom tinycalc.mul import mul\n\n"
        "OPS = {'add': add, 'mul': mul}\n\n"
        "if __name__ == '__main__':\n"
        "    op, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])\n"
        "    print(OPS[op](a, b))\n")},
}
BROKEN = {"add": {"tinycalc/add.py": "def add(a, b):\n    return a - b\n"},
          "mul": {"tinycalc/mul.py": "def mul(a, b):\n    return a + b\n"}}

CLI_TASK = {"key": "cli", "title": "Command-line entry point",
            "goal": "Create tinycalc/cli.py: `python -m tinycalc.cli add|mul A B` prints the result.",
            "acceptance_criteria": ["python -m tinycalc.cli add 2 3 prints 5"],
            "depends_on": ["add", "mul"],
            "checks": [[sys.executable, "-m", "tinycalc.cli", "add", "2", "3"]]}


def _py(obj):
    if isinstance(obj, str):
        return obj.replace("{python}", sys.executable)
    if isinstance(obj, list):
        return [_py(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _py(v) for k, v in obj.items()}
    return obj


def load_spec() -> dict:
    with open(os.path.join(HERE, "spec.json")) as fh:
        return _py(json.load(fh))


def make_project(base: str) -> str:
    root = os.path.join(base, "tinycalc")
    shutil.copytree(os.path.join(HERE, "project"), root)
    git = ["git", "-C", root, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "fixture: empty tinycalc"], check=True)
    return root


def write_scenario(base: str, behaviours: dict) -> tuple:
    path = os.path.join(base, "scenario.json")
    with open(path, "w") as fh:
        json.dump({"behaviours": behaviours, "solutions": SOLUTIONS, "broken": BROKEN}, fh)
    return path, os.path.join(base, "agent_log.jsonl")


def make_adapter(base: str, root: str, behaviours: dict, *, host=None, socket=None):
    from core.fullcycle.hosts import LocalHost
    from core.fullcycle.tmux_adapter import TmuxAgentAdapter
    scenario, log = write_scenario(base, behaviours)
    a = TmuxAgentAdapter(host or LocalHost(),
                         socket=socket or os.path.join(base, f"tmux-{uuid.uuid4().hex[:8]}"),
                         session="agent",
                         launch_argv=[sys.executable, os.path.join(HERE, "fake_agent.py")],
                         workdir=root, state_dir=os.path.join(base, "adapter_state"),
                         env={"FULLCYCLE_FAKE_SCENARIO": scenario, "FULLCYCLE_FAKE_LOG": log})
    r = a.ensure_session()
    assert r["ok"], r
    wait_for(lambda: "fake agent ready" in a._tail(), 10)
    return a, log


def wait_for(cond, timeout: float, step: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


def agent_log(log: str) -> list:
    if not os.path.exists(log):
        return []
    with open(log) as fh:
        return [json.loads(x) for x in fh if x.strip()]


class StubReviewer:
    """Plays the GPT tech lead through the SAME inbox ChatGPT would use. Mocked: it is
    code, not a model. `script` maps task key -> list of decisions per review."""

    def __init__(self, script: dict | None = None, *, next_tasks: dict | None = None):
        self.script = script or {}
        self.next_tasks = next_tasks or {}
        self.seen: list = []

    def decide(self, review: dict) -> dict:
        from core.fullcycle import engine as fe
        key = review["payload"].get("task") or review["task_id"].split(":", 1)[1]
        fe.ack_review(review["review_id"], by="stub-gpt")
        n = sum(1 for s in self.seen if s["key"] == key)
        plan = self.script.get(key, ["approve"])
        choice = plan[min(n, len(plan) - 1)]
        if review["kind"] == "blocked":
            choice = "answer"
        if review["kind"] == "project":
            choice = "add_tasks" if self.next_tasks.get("__project__") else "escalate"
        kw = {"feedback": "" if choice == "approve" else f"stub feedback for {key}",
              "next_tasks": self.next_tasks.get(key if choice != "add_tasks" else "__project__")
              if choice in ("approve", "add_tasks") else None}
        r = fe.submit_decision(review["review_id"], choice,
                               key=f"stub:{review['review_id']}", by="stub-gpt", **kw)
        self.seen.append({"key": key, "kind": review["kind"], "choice": choice,
                          "review_id": review["review_id"], "result": r})
        return r


def run(engine, reviewer, *, until, timeout: float = 30, step: float = 0.15):
    """Tick the engine and let the reviewer answer the inbox until `until(status)`."""
    from core.fullcycle import engine as fe
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = engine.tick()
        for rv in fe.pending_reviews(engine.project_id):
            reviewer.decide(rv)
        st = engine.status()
        if until(st):
            return st
        time.sleep(step)
    raise AssertionError(f"timeout; last tick={last}; status={engine.status()}")
