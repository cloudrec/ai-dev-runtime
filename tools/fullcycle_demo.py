#!/usr/bin/env python3
"""Run the full-cycle loop once, end to end, in a throw-away directory.

    python tools/fullcycle_demo.py [--keep] [--scenario crash|wrong|reject|happy]

Builds the tinycalc fixture project (its own git repo), starts the fixture agent in a
tmux pane on a private socket, stores the spec, and ticks the engine while a scripted
reviewer answers the review inbox — the inbox ChatGPT would answer. Prints every state
change and the final git log. Uses its own control-plane DB; touches nothing live.

The agent and the reviewer are stand-ins: this demonstrates the orchestration, not a
model. For a real agent, construct the engine with AgentControlAdapter (an existing
Claude pane, through core.agent_control's policy). Starting a NEW agent pane for the
engine is not supported yet: it must go through the agent creation guard.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tests", "fixtures", "fullcycle_demo"))

SCENARIOS = {"happy": ({}, {}),
             "crash": ({"add": ["crash", "correct"]}, {}),
             "wrong": ({"mul": ["wrong", "correct"]}, {}),
             "reject": ({}, {"add": ["reject", "approve"]})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="happy")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = ap.parse_args()
    base = tempfile.mkdtemp(prefix="fullcycle-demo-")
    os.environ["CONTROL_PLANE_DB"] = os.path.join(base, "control_plane.db")
    os.environ["AGENT_CONTROL_DB"] = os.path.join(base, "agent_control.db")

    import harness as h
    from core.fullcycle import engine as fe
    from core.fullcycle import spec as specs

    behaviours, reviews = SCENARIOS[args.scenario]
    root = h.make_project(base)
    specs.create_project("tinycalc", name="tinycalc", root=root, executor="fixture")
    specs.put_spec("tinycalc", h.load_spec(), by="demo")
    adapter, log = h.make_adapter(base, root, behaviours)
    try:
        engine = fe.Engine("tinycalc", adapter, holder="demo")
        reviewer = h.StubReviewer(reviews, next_tasks={"__project__": [h.CLI_TASK]})
        seen = 0
        orig_tick = engine.tick

        def tick():
            nonlocal seen
            r = orig_tick()
            for entry in engine.log:
                print("  " + json.dumps(entry, default=str)[:160])
            for d in reviewer.seen[seen:]:
                print(f"  reviewer: {d['kind']} {d['key']} -> {d['choice']}")
            seen = len(reviewer.seen)
            return r
        engine.tick = tick
        st = h.run(engine, reviewer, until=lambda s: s["done"], timeout=90)
        print("\nRESULT:", "PROJECT DONE" if st["done"] else "NOT DONE")
        for t in st["tasks"]:
            print(f"  {t['key']:4} {t['state']:14} attempts={t['attempt']} origin={t['origin']}")
        print("\ngit log:")
        print(os.popen(f"git -C {root} log --oneline").read())
        return 0 if st["done"] else 1
    finally:
        adapter.kill()
        if args.keep:
            print(f"kept: {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
