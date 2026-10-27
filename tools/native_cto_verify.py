#!/usr/bin/env python3
"""Read-only canary verification for the native Owner OS GPT CTO bridge.

Inspects the RUNNING ai-runtime service, not this shell: its environment is read from
/proc/<MainPID>/environ (root only). Secret values are never printed, logged or returned —
only whether OPENAI_API_KEY is present.

  venv/bin/python tools/native_cto_verify.py --expect disabled         # no provider config
  venv/bin/python tools/native_cto_verify.py --expect missing_secret   # config, no key
  venv/bin/python tools/native_cto_verify.py --expect ready --key-check # after the key

--key-check makes ONE GET {base}/models/{model} request (no tokens, no spend) to prove the
key is accepted and the model is visible to it. Without it no network call is made.
--since-rowid N fails if any agent_control delivery after row N made BY THIS STACK (keys
`goal:` from native_goals, `cto:` / `ctoreq:` from cto_bridge) went to a target outside
NATIVE_OWNER_OS_AGENTS; other senders (e.g. `nativesup:`) are only counted. The deliveries
table is pruned by retention, so N older than its oldest row only proves the retained window.
Delivery: `agent_control` (server-side, canary-scoped) and `native_controller` both pass;
advice still pending past its TTL in agent_control mode fails (delivery loop stuck);
failed/expired advice is listed under ledger.delivery_failures.
Watchdog: unread bus events while the controller cursor has not moved for STALL_SECS fail the
check (loop dead or wedged). Exit 0 = pass.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SERVICE = "ai-runtime.service"
STALL_SECS = 600
SECRET_KEYS = ("OPENAI_API_KEY",)
CONFIG_KEYS = ("NATIVE_OWNER_OS_ENABLED", "NATIVE_GOALS_ENABLED", "NATIVE_OWNER_OS_AGENTS",
               "OWNER_OS_CTO_PROVIDER", "OWNER_OS_CTO_MODEL", "OWNER_OS_CTO_BASE_URL",
               "OWNER_OS_CTO_DELIVERY_MODE", "OWNER_OS_CTO_DENY_AGENTS",
               "OWNER_OS_CTO_CONTROLLER")


def service_pid(run=subprocess.run) -> int:
    out = run(["systemctl", "show", SERVICE, "-p", "MainPID", "--value"],
              capture_output=True, text=True, timeout=10).stdout.strip()
    return int(out or 0)


def read_environ(pid: int) -> dict:
    with open(f"/proc/{pid}/environ", "rb") as f:
        raw = f.read().split(b"\0")
    env = {}
    for item in raw:
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep:
            env[k] = v
    return env


def config_report(env: dict) -> dict:
    """Same gate logic the service runs (cto_bridge.provider_from_env), on its env."""
    from core import cto_bridge as cb
    with mock.patch.dict(os.environ, env, clear=True):
        provider, gate = cb.provider_from_env()
        mode = cb.delivery_mode()
    rep = {k: env.get(k, "") for k in CONFIG_KEYS}
    rep.update({k + "_present": bool(env.get(k, "").strip()) for k in SECRET_KEYS})
    rep.update({"gate": gate, "delivery_mode": mode, "provider_ready": provider is not None})
    return rep, provider


def key_check(provider, opener=urllib.request.urlopen) -> dict:
    """GET /models/{model}: authenticates the key and model access without generating."""
    req = urllib.request.Request(f"{provider.base}/models/{provider.model}", method="GET",
                                 headers={"Authorization": f"Bearer {provider._key}"})
    try:
        with opener(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {"ok": data.get("id") == provider.model, "status": 200}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code}
    except Exception as e:  # noqa: BLE001 — never echo the request (it holds the key)
        return {"ok": False, "status": None, "error": type(e).__name__}


def ledger_report(conn, scope: set) -> dict:
    """Pure reads (main() opens the DB read-only): never creates or migrates a table."""
    from core import cto_bridge as cb
    from core import native_owner_os as no

    def q(sql, args=()):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            # table/column not created yet (older schema, feature unused) = nothing there
            if "no such table" in str(e) or "no such column" in str(e):
                return []
            raise
    cur = q("SELECT last_event_id,updated_at FROM cto_cursor WHERE consumer=?",
            (no.CONSUMER,))
    head = (q("SELECT COALESCE(MAX(id),0) FROM event") or [(0,)])[0][0]
    agents = {r[0] for r in q("SELECT DISTINCT agent FROM cto_decision")}
    outbox_to = {r[0] for r in q("SELECT DISTINCT to_agent FROM native_outbox")}
    return {
        "cursor": cur[0][0] if cur else None, "bus_head": head,
        "cursor_age_secs": _age(cur[0][1]) if cur else None,
        "decisions": dict(q("SELECT status,COUNT(*) FROM cto_decision GROUP BY status")),
        "outbox": dict(q("SELECT state,COUNT(*) FROM native_outbox GROUP BY state")),
        "conversations": (q("SELECT COUNT(*) FROM cto_conversation") or [(0,)])[0][0],
        "out_of_scope_decisions": sorted(a for a in agents if scope and not _in(a, scope)),
        "stale_pending": [r[0] for r in q(
            "SELECT msg_key,created_at FROM native_outbox WHERE state='pending'")
            if (_age(r[1]) is None or _age(r[1]) > cb.ADVICE_TTL_SECS + STALL_SECS)],
        "awaiting_controller_stale": [r[0] for r in q(
            "SELECT event_key,requested_at FROM cto_decision WHERE "
            "status='awaiting_controller'")
            if (_age(r[1]) is None or _age(r[1]) > cb.CONTROLLER_TIMEOUT_SECS + STALL_SECS)],
        "delivery_failures": [{"msg_key": r[0], "state": r[1], "receipt": r[2]} for r in q(
            "SELECT msg_key,state,receipt FROM native_outbox WHERE state IN "
            "('failed','expired') ORDER BY created_at DESC LIMIT 10")],
        "denied_outbox_pending": sorted(
            a for a in outbox_to if cb.agent_denied(a) and
            q("SELECT 1 FROM native_outbox WHERE to_agent=? AND state='pending'", (a,))),
    }


def _age(iso, now=None):
    try:
        t = datetime.datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return round((now - t).total_seconds())


def _in(agent: str, scope: set) -> bool:
    a = (agent or "").strip()
    return a in scope or a.split(":", 1)[0] in scope


OWN_KEY_PREFIXES = ("goal:", "cto:", "ctoreq:")
REQUEST_PREFIX = "ctoreq:"


def deliveries_since(path: str, rowid: int) -> dict:
    """{"own": [worker targets (goal:/cto:)], "requests": [controller targets (ctoreq:)],
    "other": {key prefix: count}} for deliveries after `rowid`."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT target,idempotency_key FROM deliveries WHERE rowid>? "
                            "ORDER BY rowid", (rowid,)).fetchall()
    finally:
        conn.close()
    own, requests, other = [], [], {}
    for target, key in rows:
        if (key or "").startswith(REQUEST_PREFIX):
            requests.append(target)
        elif (key or "").startswith(OWN_KEY_PREFIXES):
            own.append(target)
        else:
            p = (key or "").split(":", 1)[0] if ":" in (key or "") else "(unprefixed)"
            other[p] = other.get(p, 0) + 1
    return {"own": own, "requests": requests, "other": other}


def verdict(cfg: dict, ledger: dict, *, expect: str, key: dict = None,
            targets: list = None, request_targets: list = None) -> list:
    """List of failed checks (empty = pass)."""
    fails = []
    for k in ("NATIVE_OWNER_OS_ENABLED", "NATIVE_GOALS_ENABLED"):
        if cfg.get(k) != "1":
            fails.append(f"{k}!=1")
    if not cfg.get("NATIVE_OWNER_OS_AGENTS"):
        fails.append("NATIVE_OWNER_OS_AGENTS empty (not a canary)")
    scope = {s.strip() for s in cfg.get("NATIVE_OWNER_OS_AGENTS", "").split(",") if s.strip()}
    from core import cto_bridge as cb
    denied_in_scope = sorted(a for a in scope if cb.agent_denied(a))
    if denied_in_scope:
        fails.append(f"hard-denied agents in NATIVE_OWNER_OS_AGENTS {denied_in_scope}")
    if cfg.get("delivery_mode") == "agent_control" and ledger.get("stale_pending"):
        fails.append(f"advice pending past TTL (delivery loop stuck) "
                     f"{ledger['stale_pending']}")
    want = {"disabled": "cto_provider_disabled", "missing_secret": "missing_secret:OPENAI_API_KEY",
            "ready": ""}[expect]
    if cfg["gate"] != want:
        fails.append(f"gate={cfg['gate']!r} expected {want!r}")
    if expect == "ready" and key is not None and not key.get("ok"):
        fails.append(f"key_check failed status={key.get('status')}")
    if ledger.get("awaiting_controller_stale"):
        fails.append(f"controller requests unanswered past timeout (expiry not running) "
                     f"{ledger['awaiting_controller_stale']}")
    if ledger["cursor"] is None:
        fails.append("controller cursor missing (loop not running?)")
    elif ledger["cursor"] < ledger["bus_head"] and (ledger.get("cursor_age_secs") is None or
                                                    ledger["cursor_age_secs"] > STALL_SECS):
        fails.append(f"controller stalled: cursor {ledger['cursor']} < head "
                     f"{ledger['bus_head']}, unmoved {ledger.get('cursor_age_secs')}s")
    if ledger["out_of_scope_decisions"]:
        fails.append(f"out-of-scope decisions {ledger['out_of_scope_decisions']}")
    if ledger["denied_outbox_pending"]:
        fails.append(f"denied agents with pending advice {ledger['denied_outbox_pending']}")
    if expect != "ready" and set(ledger["decisions"]) - {"waiting_provider"}:
        fails.append(f"decisions other than waiting_provider {ledger['decisions']}")
    ctl = (cfg.get("OWNER_OS_CTO_CONTROLLER") or "").strip()
    ctl_session = ctl.split(":")[0]
    if ctl and any(a.split(":")[0] == ctl_session for a in scope):
        fails.append(f"controller {ctl} is also a worker in NATIVE_OWNER_OS_AGENTS")
    bad = sorted({t for t in (targets or []) if not _in(t, scope)})
    if bad:
        fails.append(f"deliveries outside scope {bad}")
    if ctl and any(t.split(":")[0] == ctl_session for t in (targets or [])):
        fails.append("worker advice/goal typed into the controller")
    stray = sorted({t for t in (request_targets or []) if t.split(":")[0] != ctl_session})
    if stray:
        fails.append(f"CTO requests typed outside the controller {stray}")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect", choices=("disabled", "missing_secret", "ready"), required=True)
    ap.add_argument("--key-check", action="store_true")
    ap.add_argument("--since-rowid", type=int)
    a = ap.parse_args(argv)
    pid = service_pid()
    if not pid:
        print(json.dumps({"ok": False, "fails": [f"{SERVICE} not running"]}))
        return 1
    env = read_environ(pid)
    cfg, provider = config_report(env)
    key = key_check(provider) if (a.key_check and provider is not None
                                  and provider.name == "openai") else None
    cwd = os.readlink(f"/proc/{pid}/cwd")
    db = os.path.join(cwd, env.get("CONTROL_PLANE_DB", "control_plane.db"))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        scope = {s.strip() for s in cfg["NATIVE_OWNER_OS_AGENTS"].split(",") if s.strip()}
        ledger = ledger_report(conn, scope)
    finally:
        conn.close()
    targets = None
    if a.since_rowid is not None:
        targets = deliveries_since(env.get("AGENT_CONTROL_DB",
                                           os.path.join(cwd, "agent_control.db")),
                                   a.since_rowid)
    fails = verdict(cfg, ledger, expect=a.expect, key=key,
                    targets=targets["own"] if targets else None,
                    request_targets=targets["requests"] if targets else None)
    print(json.dumps({"ok": not fails, "pid": pid, "config": cfg, "key_check": key,
                      "ledger": ledger, "deliveries_since": targets, "fails": fails},
                     indent=2, default=str))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
