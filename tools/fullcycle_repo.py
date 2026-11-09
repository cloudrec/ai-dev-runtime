#!/usr/bin/env python3
"""Owner only: which repositories the ChatGPT tech lead may start full-cycle projects on.

    fullcycle_repo.py list
    fullcycle_repo.py allow <name> <path>
    fullcycle_repo.py remove <name>

A project never runs in the repository itself: Owner OS copies its committed HEAD into
/opt/fc-projects/<project id>, branch fullcycle/<project id>, with no remote. Payment
directories, the live Owner OS tree and symlinked paths are refused whatever is asked.
Run as root on the server; the list is /etc/ai-runtime/fullcycle-repos.json.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fullcycle import repos  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("allow")
    a.add_argument("name")
    a.add_argument("path")
    r = sub.add_parser("remove")
    r.add_argument("name")
    args = ap.parse_args(argv)
    if args.cmd != "list" and os.geteuid() != 0:
        print("STOP: run as root", file=sys.stderr)
        return 2
    try:
        if args.cmd == "allow":
            got = repos.allow(args.name, args.path)
            print(f"allowed: {got['repo']} -> {got['path']}")
        elif args.cmd == "remove":
            print("removed" if repos.remove(args.name) else "not in the list")
        else:
            entries = repos.load()
            if not entries:
                print("no repos allowed")
            for name, e in sorted(entries.items()):
                why = repos.check_source(e["path"])
                print(f"{name}\t{e['path']}\t{'ok' if why is None else 'refused: ' + why}")
    except repos.RepoError as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
