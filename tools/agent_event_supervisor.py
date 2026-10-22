#!/usr/bin/env python3
"""Owner CLI for the agent event supervisor (core/agent_event_supervisor.py).

  status  [target]              read-only ledger dump (JSON)
  history <target>              state transitions, newest first (JSON)
  pause   <target> --by NAME    sticky: nothing is woken/verified/continued until unpause
  unpause <target> --by NAME    explicit owner resume (never automatic)

Pause/unpause are deliberately NOT on the HTTP API: the API surface is read-only, and
leaving PAUSED must be an explicit, attributable owner act on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent_event_supervisor as aes  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agent_event_supervisor")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("target", nargs="?", default="")
    h = sub.add_parser("history")
    h.add_argument("target")
    for name in ("pause", "unpause"):
        p = sub.add_parser(name)
        p.add_argument("target")
        p.add_argument("--by", required=True)
        if name == "pause":
            p.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out = aes.status(a.target or None)
    elif a.cmd == "history":
        out = aes.history(a.target)
    elif a.cmd == "pause":
        out = aes.pause(a.target, by=a.by, reason=a.reason)
    else:
        out = aes.unpause(a.target, by=a.by)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
