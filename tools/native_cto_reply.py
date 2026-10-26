#!/usr/bin/env python3
"""Controller-session reply to one Owner OS CTO request (provider `claude_session`).

The request typed into the controller pane contains the exact command, e.g.

  venv/bin/python tools/native_cto_reply.py --key 'cp:123' --nonce 'ab12…' <<'JSON'
  {"decision": "...", "rationale": "...", "owner_needed": false, "message_to_agent": "..."}
  JSON

The reply is accepted once, only while that event is `awaiting_controller` and only with
the nonce of its CURRENT request; it then records the decision and queues the advice for
the worker exactly like a synchronous provider. Prints one JSON line; exit 0 = accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MAX_STDIN = 64 * 1024


def main(argv=None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key", required=True)
    ap.add_argument("--nonce", required=True)
    a = ap.parse_args(argv)
    raw = (stdin or sys.stdin).read(MAX_STDIN + 1)
    if len(raw) > MAX_STDIN:
        print(json.dumps({"ok": False, "reason": "reply_too_large"}))
        return 1
    try:
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            raise ValueError("not an object")
    except ValueError as e:
        print(json.dumps({"ok": False, "reason": f"bad_json:{e}"}))
        return 1
    from core import cto_bridge as cb
    from core import native_owner_os as no
    res = cb.submit_reply(a.key, a.nonce, reply, emit_owner=no._emit_owner)
    print(json.dumps({k: res.get(k) for k in ("ok", "reason", "status", "owner_needed",
                                              "message_queued") if k in res}))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
