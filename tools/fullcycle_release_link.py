#!/usr/bin/env python3
"""Owner only: make a one-time release link for a finished repo project, on demand.

    fullcycle_release_link.py <project id>

Same link the finished-project notice carries (core.fullcycle.release): 24 h, single use,
bound to the project's current commit. Prints it; open it on the phone and press Release.
"""
from __future__ import annotations

import os
import pwd
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent_privilege as priv  # noqa: E402
from core.fullcycle import intake, release  # noqa: E402
from core.fullcycle import spec as specs  # noqa: E402


def head_of(root: str, branch: str) -> str:
    """The branch's commit, read the way the release reads it: by the tree's owner."""
    argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", root,
            "rev-parse", "--verify", f"{branch}^{{commit}}"]
    env = None
    st = os.lstat(root)
    if st.st_uid != 0:
        p = pwd.getpwuid(st.st_uid)
        t = priv.Target(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir)
        argv, env = priv.sandboxed_argv(t, intake.intake_root() or "", argv), priv.child_env(t)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env, cwd="/")
    if r.returncode != 0:
        raise SystemExit(f"STOP: cannot read {branch}: {r.stderr.strip()[:160]}")
    return r.stdout.strip()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("STOP: run as root", file=sys.stderr)
        return 2
    pid = args[0]
    p, src = specs.get_project(pid), intake.source_of(pid)
    if not p or not src:
        print(f"STOP: {pid} is not a project started on an allowed repo", file=sys.stderr)
        return 1
    link = release.make_link(pid, head_of(p["root"], src["branch"]))
    if not link:
        print("STOP: release links are off (no key or no FULLCYCLE_RELEASE_URL_BASE)",
              file=sys.stderr)
        return 1
    print(link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
