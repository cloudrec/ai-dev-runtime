#!/usr/bin/env python3
"""Release Controller CLI — the operator command for taking a branch live.

Nothing here happens implicitly: each step is a separate command the operator
runs, and `release` refuses anything that is not an approved candidate whose
head SHA still matches what was approved.

    # 1. create a candidate for ONE branch
    python3 -m cli.release create --branch ai-runtime/109-fix --service ai-runtime.service \
        --health-url http://172.17.0.1:8199/health

    # 2. inspect it (diff, SHAs, state)
    python3 -m cli.release show --id rc-xxxx
    python3 -m cli.release diff --id rc-xxxx

    # 3. run the suite on the candidate
    python3 -m cli.release test --id rc-xxxx

    # 4. approve, naming the exact SHA
    python3 -m cli.release approve --id rc-xxxx --approver alex --sha <head-sha>

    # 5. release: merge -> retest -> restart -> health check (auto-rollback on failure)
    python3 -m cli.release release --id rc-xxxx

    # rollback an already-released candidate
    python3 -m cli.release rollback --id rc-xxxx
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import release_controller as rcx  # noqa: E402

DEFAULT_PROJECT = os.getenv("RUNTIME_PROJECT_PATH", "/root/ai-dev-runtime")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _brief(rc: dict) -> dict:
    return {k: rc.get(k) for k in (
        "id", "branch", "base_branch", "head_sha", "approved_sha", "merge_sha",
        "backup_branch", "service", "health_url", "state", "approved_by", "error")}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cli.release", description="Release Controller")
    p.add_argument("--project", default=DEFAULT_PROJECT, help="repository path")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a release candidate for one branch")
    c.add_argument("--branch", required=True)
    c.add_argument("--base", default="main")
    c.add_argument("--service", default=None, help="the ONE unit to restart, e.g. ai-runtime.service")
    c.add_argument("--health-url", default=None)
    c.add_argument("--job-outcome", default=None,
                   help="outcome of the job that produced the branch; a fallback_plan_only branch is refused")

    for name, help_text in (("show", "show a candidate"), ("diff", "show the candidate diff"),
                            ("verify", "re-check the branch head SHA"),
                            ("test", "run the repository suite on the candidate"),
                            ("release", "merge+verify an approved candidate"),
                            ("rollback", "roll a released candidate back")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--id", required=True)

    a = sub.add_parser("approve", help="explicitly approve a candidate")
    a.add_argument("--id", required=True)
    a.add_argument("--approver", required=True)
    a.add_argument("--sha", required=True, help="exact head SHA being approved")

    r = sub.add_parser("reject", help="reject a candidate")
    r.add_argument("--id", required=True)
    r.add_argument("--reason", required=True)

    sub.add_parser("list", help="list candidates")

    args = p.parse_args(argv)
    rcx.init_db()

    try:
        if args.cmd == "create":
            rc = rcx.create_candidate(args.project, args.branch, args.base, args.service,
                                      args.health_url, args.job_outcome)
            _print(_brief(rc))
            print(f"\nNext: python3 -m cli.release test --id {rc['id']}", file=sys.stderr)
        elif args.cmd == "list":
            _print([_brief(x) for x in rcx.list_releases()])
        elif args.cmd == "show":
            _print(rcx.get(args.id) or {"error": "not found"})
        elif args.cmd == "diff":
            rc = rcx.get(args.id) or {}
            print(rc.get("diff_stat") or "(no diff recorded)")
        elif args.cmd == "verify":
            _print(rcx.verify(args.project, args.id))
        elif args.cmd == "test":
            _print(rcx.run_tests(args.project, args.id, phase="tests_before"))
        elif args.cmd == "approve":
            _print(_brief(rcx.approve(args.project, args.id, args.approver, args.sha)))
        elif args.cmd == "reject":
            _print(_brief(rcx.reject(args.id, args.reason)))
        elif args.cmd == "release":
            rc = rcx.release(args.project, args.id)
            _print(_brief(rc))
            return 0 if rc["state"] == rcx.RELEASED else 1
        elif args.cmd == "rollback":
            _print(_brief(rcx.rollback(args.project, args.id)))
    except rcx.ReleaseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
