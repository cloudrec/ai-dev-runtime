#!/usr/bin/env python3
"""Dry-run of the agent_control automation policy against REAL recent traffic (read-only).

Replays every delivered/acted event in the agent_control audit logs (current + rotated,
.gz included) through the exact policy functions of THIS checkout, under a proposed
AGENT_AUTOMATION_TARGETS set only inside this process. Nothing is sent, written or changed.

  venv/bin/python tools/automation_policy_dryrun.py --targets ''
  venv/bin/python tools/automation_policy_dryrun.py \
      --targets 'owner-os-supervisor-clean@api,bitwhite-backend-v3@native_supervisor'

Human-typed owner input never passes agent_control (it is typed straight into the pane), so
it cannot appear here and is never affected by the policy.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ACTS = ("agent_send", "agent_answer", "control_keys", "agent_stop", "agent_resume")


CONTROLLER_SESSION = "owner-os-supervisor-clean"


def family(key: str, actor: str, target: str = "") -> str:
    """Who sent it, from actor, target and idempotency key prefix (attribution only)."""
    k = key or ""
    m = re.match(r"^([a-z]+)[:\-]", k)
    p = m.group(1) if m else ""
    if (actor or "").startswith("api:"):
        return "owner_os_api_to_controller" if target.split(":")[0] == CONTROLLER_SESSION \
            else "api_mcp_to_agent"
    return {"nativesup": "native_supervisor", "goal": "native_goals", "cto": "cto_bridge",
            "ctoreq": "cto_bridge_request", "phase": "phase_advance",
            "owner": "owner_stop_via_api"}.get(p, p or "unattributed")


def events(paths):
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        try:
            fh = op(p, "rt", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                act = e.get("action") or ""
                if act not in ACTS:
                    continue
                if act in ("agent_send", "agent_answer", "control_keys") and \
                        not e.get("delivered"):
                    continue
                if act == "agent_stop" and not e.get("stopped"):
                    continue
                yield e


def judge(e: dict, ac) -> str:
    act, tgt = e.get("action"), str(e.get("target") or "")
    sess = tgt.split(":")[0]
    if act == "agent_resume":
        return "unaffected (agent creation is outside this policy)"
    if act == "agent_stop":
        return "BLOCKED hard_denied_target (needs owner proof)" \
            if sess.lower() in ac.AUTOMATION_HARD_DENY else "unaffected (confirm-gated stop)"
    why = ac.automation_refusal(sess, tgt, e.get("actor"))
    return f"BLOCKED {why}" if why else "allowed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--targets", required=True, help="proposed AGENT_AUTOMATION_TARGETS")
    ap.add_argument("--log-glob", default="/var/log/ai-runtime/agent_control.jsonl*")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    os.environ["AGENT_AUTOMATION_TARGETS"] = a.targets
    os.environ.pop("AGENT_AUTOMATION_POLICY", None)              # judge the enforced policy
    from core import agent_control as ac
    def rotation(p):                 # x.jsonl.3.gz (oldest) ... x.jsonl.1, x.jsonl (newest)
        m = re.search(r"\.jsonl\.(\d+)", p)
        return -int(m.group(1)) if m else 0
    paths = sorted(glob.glob(a.log_glob), key=rotation)
    rows = collections.Counter()
    first = last = None
    for e in events(paths):
        ts = e.get("ts") or ""
        first, last = first or ts, ts
        actor = str(e.get("actor") or "")
        rows[(family(str(e.get("idempotency_key") or ""), actor, str(e.get("target") or "")),
              ac.caller_class(actor), e.get("action"), str(e.get("target") or ""),
              judge(e, ac))] += 1
    out = {"targets": a.targets, "window": [first, last],
           "rows": [{"sender": s, "caller": c, "action": act, "target": t, "verdict": v,
                     "count": n} for (s, c, act, t, v), n in rows.most_common()],
           "blocked": sum(n for k, n in rows.items() if k[4].startswith("BLOCKED")),
           "total": sum(rows.values())}
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"targets={a.targets!r} window={first} -> {last} "
              f"blocked {out['blocked']}/{out['total']}")
        for r in out["rows"]:
            print(f"{r['count']:5}  {r['sender']:28} {r['caller']:18} {r['action']:12} "
                  f"{r['target']:32} {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
