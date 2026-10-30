#!/usr/bin/env python3
"""Tell the ChatGPT tech lead that a work stage is finished — the agent's doorbell.

    stage_done.py --summary "what was done" [--ref branch@sha] [--report path]
                  [--tests "N passed"] [--next "what to review / proposed next step"]
                  [--blocker "needs owner: ..."]...

A plain turn end (`agent_turn_stopped`) is a record, never a wake, by design
(hooks/owneros_hook.py). Finishing a stage is different: the reviewer must look now and
send the next step. This emits `task_completed` through core.control_plane.cto.emit —
the path that consults the wake bridge at once (a raw append_event does not) — with the
agent identity resolved exactly as the Owner OS hook resolves it, and without paging
Telegram. One event per (agent, ref, summary) per day: rerunning it does not re-wake.

Refused for an agent whose directory is under the payment hard-deny policy: those
agents take no automated continuation, so a doorbell would only invite one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

REPO = "/root/ai-dev-runtime"


def _env_flags() -> None:
    """The bridge is enabled by the services' EnvironmentFile; an agent's shell does not
    load it. Only the two WAKE_BRIDGE_* flags are read from it — nothing else."""
    path = os.path.join(REPO, "configs", ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k in ("WAKE_BRIDGE_ENABLED", "WAKE_BRIDGE_KILL_SWITCH"):
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--summary", required=True)
    ap.add_argument("--ref", default="")
    ap.add_argument("--report", default="")
    ap.add_argument("--tests", default="")
    ap.add_argument("--next", default="")
    ap.add_argument("--blocker", action="append", default=[])
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "hooks"))
    _env_flags()
    from core import agent_creation_policy as acp
    try:
        if acp.hard_denied(a.cwd, "", acp.load_config()):
            print(json.dumps({"ok": False, "reason": "payment_hard_denied"}))
            return 2
    except Exception:  # noqa: BLE001 — unreadable policy: fail closed
        print(json.dumps({"ok": False, "reason": "payment_policy_unreadable"}))
        return 2
    import owneros_hook as hook
    if not os.environ.get("OWNEROS_AGENT_TARGET") and os.environ.get("TMUX_PANE"):
        # the pane this agent runs in IS its Owner OS target (session:window.pane)
        import subprocess
        t = subprocess.run(["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"],
                            "#{session_name}:#{window_index}.#{pane_index}"],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        if t:
            os.environ["OWNEROS_AGENT_TARGET"] = t
    ident = hook._identity({"cwd": a.cwd, "session_id": os.environ.get("CLAUDE_SESSION_ID", "")})
    payload = {"stage_done": True, "summary": a.summary[:2000], "ref": a.ref[:200],
               "report": a.report[:400], "tests": a.tests[:400], "next": a.next[:2000],
               "blockers": [b[:400] for b in a.blocker][:10], "cwd": ident["cwd"]}
    digest = hashlib.sha256(json.dumps([ident["agent"], a.ref, a.summary]).encode()).hexdigest()[:16]
    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "identity": ident, "payload": payload}))
        return 0
    from core.control_plane import cto
    r = cto.emit("agent_stage_done", "task_completed", project_id=ident["project"],
                 agent_id=ident["agent"], severity="high", payload=payload,
                 correlation_id=f"stage:{digest}", dedup_key=f"stage_done:{digest}",
                 dedup_window_secs=86400, push=False)
    eid = r["event_id"]
    from core.control_plane.api import _c
    conn, _ = _c(None)
    try:
        row = conn.execute("SELECT decision, reason, route_key FROM wake_audit WHERE event_id=?"
                           " ORDER BY id DESC LIMIT 1", (eid,)).fetchone()
    finally:
        conn.close()
    out = {"ok": True, "event_id": eid, "identity": {"agent": ident["agent"],
                                                     "project": ident["project"]},
           "wake": {"decision": row[0], "reason": row[1], "route": row[2]} if row else None}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
