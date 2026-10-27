#!/usr/bin/env python3
"""Read-only acceptance check for one delegation grant.

  venv/bin/python tools/grant_verify.py <grant id>

Prints the grant (status, binding, bound actor, scope) and, from the agent_control audit
log, every send/answer/create it authorized, plus actions by the same actor under another
authority. Exit 1 if any such action had an authority other than the allowlist or an
owner proof.
"""
from __future__ import annotations

import gzip
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def audit_events(paths):
    for p in paths:
        op = gzip.open if p.endswith(".gz") else open
        try:
            with op(p, "rt", errors="replace") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def main(argv=None) -> int:
    from core import agent_control as ac
    from core import automation_grants as ag
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    g = ag.get(argv[0])
    if g is None:
        print(json.dumps({"ok": False, "reason": "unknown grant"}))
        return 1
    spec, actor = g["spec"], g["spec"].get("bound_to")
    paths = sorted(glob.glob(ac._audit_path() + "*"), reverse=True)
    covered, other = [], []
    for e in audit_events(paths):
        if e.get("action") not in ("agent_send", "agent_answer", "agent_resume"):
            continue
        if not (e.get("delivered") or e.get("resumed")):
            continue
        row = {k: e.get(k) for k in ("ts", "action", "target", "actor", "authorized_by")}
        if e.get("authorized_by") == f"grant:{g['id']}":
            covered.append(row)
        elif e.get("actor") == actor:
            other.append(row)       # same actor, other authority (e.g. the allowlist)
    unexpected = [r for r in other if r["authorized_by"] not in ("allowlist", "owner_proof")]
    out = {"grant": g["id"], "effective": ag.effective_status(g), "binding": spec.get("binding"),
           "bound_to": actor, "summary": ag.summary(spec), "approved_via": g.get("approved_via"),
           "covered_actions": covered, "same_actor_other_authority": other,
           "unexpected": unexpected}
    print(json.dumps(out, indent=2, default=str))
    return 0 if not unexpected else 1


if __name__ == "__main__":
    sys.exit(main())
