#!/usr/bin/env python3
"""Register a project with the full-cycle engine and store its spec.

    fullcycle_register.py --project ID --root DIR --executor claude --spec spec.json --by WHO

Idempotent: an existing project with the same root/executor is kept; an identical spec
is not re-versioned; a different root or executor under the same id is refused. The
root must pass the same gate as every dispatch (gates.check_project_root): no broad,
live-runtime, system or payment directory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--executor", default="claude")
    ap.add_argument("--spec", required=True, help="spec JSON file")
    ap.add_argument("--name", default="")
    ap.add_argument("--by", required=True, help="who authored the spec (recorded)")
    args = ap.parse_args()
    from core.fullcycle import engine as fe
    from core.fullcycle import gates
    from core.fullcycle import spec as specs

    refused = gates.check_project_root(args.root)
    if refused:
        print(json.dumps({"ok": False, "error": refused}))
        return 1
    with open(args.spec) as f:
        body = json.load(f)
    try:
        p = specs.create_project(args.project, name=args.name or args.project,
                                 root=os.path.realpath(args.root), executor=args.executor)
        s = specs.put_spec(args.project, body, by=args.by)
    except specs.SpecError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, "project": p, "spec_version": s["version"], "sha": s["sha"],
                      "status": fe.project_status(args.project)}, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
