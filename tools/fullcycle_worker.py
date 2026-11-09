#!/usr/bin/env python3
"""Tick the full-cycle engine of each registered project, forever.

    FULLCYCLE_PROJECTS="<project_id>=<tmux session>[,...]" fullcycle_worker.py [--once]

One engine per project, each driving an EXISTING agent through core.agent_control
(AgentControlAdapter, actor `fullcycle:engine`): every send passes the automation policy
of THIS process — the session must be listed as `<session>@fullcycle:engine` in
AGENT_AUTOMATION_TARGETS — and is refused while the agent is mid-turn. The worker never
creates, restarts or stops an agent, never pushes or deploys, and never answers an owner
gate; review events reach the reviewer's chat through the wake bridge.

A project that is not registered (tools/fullcycle_register.py), or whose engine raises, is
logged and retried next round; one bad project never stops the others.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TICK_SECS = float(os.getenv("FULLCYCLE_TICK_SECS", "20"))


def projects(raw: str) -> dict:
    out = {}
    for item in (raw or "").split(","):
        pid, _, session = item.strip().partition("=")
        if pid and session:
            out[pid.strip()] = session.strip()
    return out


def round_projects(wanted: dict, intake) -> dict:
    """The configured projects, then every intake project not done yet (its adapter
    starts and readies the agent itself, see `intake.adapter`)."""
    out = dict(wanted)
    try:
        todo = intake.active()
    except Exception as ex:  # noqa: BLE001 — the configured projects still run
        _log(what="intake_error", error=f"{type(ex).__name__}: {ex}"[:300])
        return out
    for p in todo:
        out.setdefault(p["id"], p)
    return out


def settle_intake(pid: str, result: dict, intake) -> None:
    """Project done and no task left in flight: stop its agent, free the slot."""
    tasks = result.get("tasks") or {}
    if result.get("project") != "done" or not tasks:
        return
    if any(state not in ("VERIFIED_DONE", "CANCELLED") for state in tasks.values()):
        return
    for p in intake.projects():
        if p["id"] == pid and intake.stop_agent(p):
            _log(what="intake_agent_stopped", project=pid)


def notify_round(intake) -> None:
    """The owner's Telegram notice for each finished intake project, once."""
    try:
        for n in intake.notify_finished():
            _log(what="intake_notice", **n)
    except Exception as ex:  # noqa: BLE001 — never stops the loop
        _log(what="intake_notice", state="error", error=f"{type(ex).__name__}: {ex}"[:300])


def _log(**kw) -> None:
    print(json.dumps({"ts": round(time.time(), 1), **kw}, default=str), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one round, then exit")
    args = ap.parse_args()
    from core.fullcycle import engine as fe
    from core.fullcycle.tmux_adapter import AgentControlAdapter

    from core.fullcycle import intake
    wanted = projects(os.getenv("FULLCYCLE_PROJECTS", ""))
    if not wanted and not intake.intake_root():
        _log(what="no_projects", hint="set FULLCYCLE_PROJECTS=<project_id>=<session> "
                                      "or FULLCYCLE_INTAKE_ROOT")
        return 2
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    holder = f"fullcycle-worker:{socket.gethostname()}:{os.getpid()}"
    engines: dict = {}
    while not stop["now"]:
        for pid, session in round_projects(wanted, intake).items():
            try:
                e = engines.get(pid)
                if e is None:
                    a = intake.adapter(session) if isinstance(session, dict) \
                        else AgentControlAdapter(session)
                    e = engines[pid] = fe.Engine(pid, a, holder=holder)
                if hasattr(e.adapter, "prepare"):
                    ready = e.adapter.prepare()
                    if ready.get("created") or not ready.get("ok"):
                        _log(what="intake_agent", project=pid, **ready)
                    if not ready.get("ok"):
                        continue
                r = e.tick()
                if r.get("log") or r.get("skipped"):
                    _log(**{**r, "project": pid, "result": r.get("project")})
                if pid not in wanted:
                    settle_intake(pid, r, intake)
            except Exception as ex:  # noqa: BLE001 — logged, retried next round
                engines.pop(pid, None)
                _log(project=pid, what="error", error=f"{type(ex).__name__}: {ex}"[:500])
        notify_round(intake)
        if args.once:
            break
        for _ in range(int(TICK_SECS * 10)):
            if stop["now"]:
                break
            time.sleep(0.1)
    for e in engines.values():
        try:
            e.release()
        except Exception:  # noqa: BLE001 — the lease then expires on its own
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
