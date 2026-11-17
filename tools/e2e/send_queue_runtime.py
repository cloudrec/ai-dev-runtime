#!/usr/bin/env python3
"""Disposable runtime for the send-queue E2E: the real /api/v1 router over HTTP, with
synthetic panes. Nothing reaches tmux; typed text is appended to $E2E_DIR/typed.jsonl.
Env: E2E_DIR (scratch), E2E_PORT, RUNTIME_TOKEN (random, set by the caller)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
D = os.environ["E2E_DIR"]
os.environ.update(AGENT_CONTROL_DB=f"{D}/ac.db", CONTROL_PLANE_DB=f"{D}/cp.db",
                  AGENT_CONTROL_AUDIT=f"{D}/audit.jsonl",
                  AGENT_CREATION_CONFIG=f"{D}/policy.yaml",
                  AGENT_AUTOMATION_OPEN_CALLERS="api", OWNEROS_RUNTIME_ENV=f"{D}/empty.env")
os.environ.pop("AGENT_AUTOMATION_TARGETS", None)
open(f"{D}/policy.yaml", "w").write("hard_deny_dirs:\n  - /opt/payment-orchestrator\n")
open(f"{D}/empty.env", "a").close()

from core import agent_control as ac  # noqa: E402
from core import send_queue as sq  # noqa: E402

PANES = {"toy-agent": {"target": "toy-agent:0.0", "session": "toy-agent", "alive": True,
                       "pane_id": "%71", "pid": 7101, "cwd": f"{D}/toy"},
         "pay-synthetic": {"target": "pay-synthetic:0.0", "session": "pay-synthetic",
                           "alive": True, "pane_id": "%72", "pid": 7102,
                           "cwd": "/opt/payment-orchestrator"}}


def pane(target):
    p = PANES.get(target.split(":")[0])
    if not p:
        raise ac.AgentControlError(f"no existing agent at {target!r}")
    return dict(p)


def tmux(args, stdin=None):
    if args and args[0] == "load-buffer" and stdin is not None:
        with open(f"{D}/typed.jsonl", "a") as fh:
            fh.write(json.dumps({"text": stdin if isinstance(stdin, str) else
                                 stdin.decode()}) + "\n")
    return 0, "", ""


ac._pane_is_live_agent = pane
ac._busy_for_send = lambda t: "working" if os.path.exists(f"{D}/busy") else ""
ac._tmux = tmux
ac.time.sleep = lambda s: None


def drainer():
    while True:
        try:
            if os.getenv("SEND_QUEUE_ENABLED") == "1":
                sq.drain()
        except Exception as e:  # noqa: BLE001
            print("drain error", e, flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    import uvicorn
    from fastapi import FastAPI

    from api import v1
    app = FastAPI()
    app.include_router(v1.router)
    threading.Thread(target=drainer, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["E2E_PORT"]), log_level="warning")
