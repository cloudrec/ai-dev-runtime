#!/usr/bin/env python3
"""Bounded, isolated keyless-CTO loop for ONE sandbox worker and ONE controller session.

Not a service: a one-shot harness run from a dev worktree. It never touches the live
ai-runtime ledgers — every store is redirected into --state-dir before anything imports:
  CONTROL_PLANE_DB, AGENT_CONTROL_DB, AGENT_CONTROL_AUDIT -> state dir;
  NATIVE_OWNER_OS_AGENTS = the worker session only; OWNER_OS_CTO_CONTROLLER = controller;
  provider claude_session; delivery agent_control; goals OFF.
Typing uses the real tmux server (the worker and the controller are real Claude panes),
guarded three times: code scope, a session listing filtered to exactly those two panes, and
a send wrapper that raises for any other target.

Flow: preflight (fail closed) -> emit one test event for the worker into the sandbox bus ->
tick every --interval s: request -> controller (when idle) -> controller replies with
tools/native_cto_reply.py -> advice -> worker (when idle) -> worker changes notes.txt ->
harness emits the follow-up event once (stands in for the worker's lifecycle hook) ->
second decision -> done. Stops on --max-secs, on STOP in the state dir, or when finished.
Writes run.jsonl (every tick) and summary.json (evidence) into the state dir.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLOSED = {"aes-canary", "bootstrap", "giftcards-bot-review-opus"}
DENIED = {"payorch-ha-fresh"}
LIVE_AC_DB = "/root/ai-dev-runtime/agent_control.db"


class Refused(Exception):
    pass


def session_of(target: str) -> str:
    return (target or "").strip().split(":")[0]


def preflight_names(worker: str, controller: str) -> None:
    w, c = session_of(worker), session_of(controller)
    if not w or not c:
        raise Refused("worker and controller targets are required")
    if w == c:
        raise Refused("controller and worker must be different sessions (no self-review)")
    for name in (w, c):
        if name.lower() in DENIED:
            raise Refused(f"{name} is hard-denied")
        if name in CLOSED:
            raise Refused(f"{name} was closed by the owner and must not be used")


def isolate(state: str, worker: str, controller: str) -> None:
    os.makedirs(state, exist_ok=True)
    os.environ.update({
        "CONTROL_PLANE_DB": os.path.join(state, "cp.db"),
        "AGENT_CONTROL_DB": os.path.join(state, "ac.db"),
        "AGENT_CONTROL_AUDIT": os.path.join(state, "audit.jsonl"),
        "NATIVE_OWNER_OS_AGENTS": session_of(worker),
        "OWNER_OS_CTO_PROVIDER": "claude_session",
        "OWNER_OS_CTO_CONTROLLER": controller,
        "OWNER_OS_CTO_DELIVERY_MODE": "agent_control",
        "NATIVE_GOALS_ENABLED": "0",
        "NATIVE_OWNER_OS_ENABLED": "1",
    })
    for k in ("OPENAI_API_KEY", "OWNER_OS_CTO_MODEL", "OWNER_OS_CTO_BASE_URL"):
        os.environ.pop(k, None)


def filtered_listing(full: dict, worker: str, controller: str) -> dict:
    """Exactly the two panes, each located once through native state, or refuse."""
    keep = []
    for want in (worker, controller):
        hits = [s for s in full.get("sessions", []) if s.get("tmux_target") == want]
        if len(hits) != 1:
            raise Refused(f"{want}: expected exactly one native session, found {len(hits)}")
        keep.append(hits[0])
    return {**full, "sessions": keep}


def guard_send(allowed: set, real_send):
    def send(target, text, idempotency_key=None, **kw):
        if target not in allowed:
            raise Refused(f"send to {target!r} outside {sorted(allowed)}")
        return real_send(target, text, idempotency_key=idempotency_key, **kw)
    return send


def live_deliveries_since(rowid: int, targets: set) -> list:
    """Read-only: who ELSE typed into the two panes during the run (live supervisors)."""
    try:
        c = sqlite3.connect(f"file:{LIVE_AC_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute("SELECT rowid,idempotency_key,target,created_at FROM deliveries "
                             "WHERE rowid>?", (rowid,)).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []
    return [r for r in rows if r[2] in targets]


def live_hwm() -> int:
    try:
        c = sqlite3.connect(f"file:{LIVE_AC_DB}?mode=ro", uri=True, timeout=5)
        try:
            return c.execute("SELECT COALESCE(MAX(rowid),0) FROM deliveries").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--worker", required=True, help="tmux target, e.g. name:0.0")
    ap.add_argument("--controller", required=True)
    ap.add_argument("--workdir", required=True, help="worker directory with notes.txt")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--max-secs", type=float, default=1800.0)
    ap.add_argument("--question", default="notes.txt has step 1 done. What is the next "
                    "single safe step from TASK.md?")
    ap.add_argument("--preflight-only", action="store_true")
    a = ap.parse_args(argv)
    preflight_names(a.worker, a.controller)
    isolate(a.state_dir, a.worker, a.controller)
    sys.path.insert(0, ROOT)
    from core import agent_control as ac
    from core import cto_bridge as cb
    from core import native_agents as na
    from core import native_owner_os as no
    from core.control_plane import cto

    log_path = os.path.join(a.state_dir, "run.jsonl")

    def log(**kw):
        kw["ts"] = time.time()
        with open(log_path, "a") as fh:
            fh.write(json.dumps(kw, default=str) + "\n")

    provider, gate = cb.provider_from_env()
    if provider is None:
        raise Refused(f"provider gate: {gate}")
    full = na.list_sessions()
    if full.get("source") != na.SOURCE_NATIVE:
        raise Refused(f"native session state unavailable ({full.get('source')})")
    listing = filtered_listing(full, a.worker, a.controller)
    allowed = {a.worker, a.controller}
    ac.agent_send = guard_send(allowed, ac.agent_send)
    notes = os.path.join(a.workdir, "notes.txt")
    before = open(notes).read()
    hwm = live_hwm()
    log(event="preflight_ok", worker=a.worker, controller=a.controller, gate=gate,
        listing=[(s["tmux_target"], s["state"]) for s in listing["sessions"]],
        live_deliveries_hwm=hwm)
    if a.preflight_only:
        print(json.dumps({"ok": True, "preflight": "passed"}))
        return 0

    no.run_once(listing=listing, provider=provider, gate=gate)          # cursor -> head

    def emit(question):
        return cto.emit("sandbox_harness", "milestone_review", project_id="owneros-sandbox",
                        agent_id=a.worker, severity="info", owner_action_required=False,
                        payload={"question": question}, push=False,
                        dedup_window_secs=0)["event_id"]
    first = emit(a.question)
    log(event="emitted", event_id=first)
    followup, deadline, done = None, time.time() + a.max_secs, False
    while time.time() < deadline and not os.path.exists(os.path.join(a.state_dir, "STOP")):
        listing = filtered_listing(na.list_sessions(), a.worker, a.controller)
        res = no.run_once(listing=listing, provider=provider, gate=gate)
        log(event="tick", states=[(s["tmux_target"], s["state"]) for s in listing["sessions"]],
            delivered=res.get("delivered"), retried=res.get("retried"),
            errors=res.get("errors"))
        now = open(notes).read()
        if followup is None and now != before:
            followup = emit(f"notes.txt now ends with: {now.strip().splitlines()[-1]!r}. "
                            "Is the task complete?")
            log(event="worker_result", notes=now, followup_event=followup)
        if followup is not None:
            conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
            st = conn.execute("SELECT status FROM cto_decision WHERE event_key=?",
                              (f"cp:{followup}",)).fetchone()
            conn.close()
            if st and st[0] in ("decided", "provider_failed"):
                done = True
                break
        time.sleep(a.interval)
    sc = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    try:
        sent = sc.execute("SELECT idempotency_key,target FROM deliveries ORDER BY rowid"
                          ).fetchall()
    except sqlite3.Error:
        sent = []
    sc.close()
    cp = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    summary = {
        "finished": done, "worker": a.worker, "controller": a.controller,
        "notes_before": before, "notes_after": open(notes).read(),
        "sandbox_deliveries": sent,
        "out_of_scope_sends": [s for s in sent if s[1] not in allowed],
        "requests_to_non_controller": [s for s in sent if s[0].startswith("ctoreq:")
                                       and s[1] != a.controller],
        "advice_to_non_worker": [s for s in sent if s[0].startswith("cto:")
                                 and s[1] != a.worker],
        "decisions": cp.execute("SELECT event_key,status,decision,owner_needed,error FROM "
                                "cto_decision ORDER BY latched_ts").fetchall(),
        "outbox": cp.execute("SELECT msg_key,to_agent,state,receipt FROM native_outbox "
                             "ORDER BY created_at").fetchall(),
        "other_senders_into_these_panes": live_deliveries_since(hwm, allowed),
    }
    cp.close()
    with open(os.path.join(a.state_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(json.dumps({"finished": done, "sends": len(sent),
                      "out_of_scope": len(summary["out_of_scope_sends"])}))
    return 0 if done and not summary["out_of_scope_sends"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as e:
        print(json.dumps({"ok": False, "refused": str(e)}))
        sys.exit(2)
