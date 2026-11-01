#!/usr/bin/env python3
"""Read-only: what base would a runtime job get in each project, and is it credible?

    base_ref_report.py /path/to/project [...]

For every project path prints the legacy guess (resolve_base_branch with the API's
"master" placeholder), its SHA, the checked-out HEAD, and the staleness verdict that
RUNTIME_STRICT_BASE_REF would apply. Used to validate a project before giving it a
`base_ref` in config/project_base_refs.yaml or turning strict mode on.

Never fetches, never writes: only rev-parse / rev-list / merge-base, with
GIT_OPTIONAL_LOCKS=0 (core.git_write._run).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from core import git_write as gw  # noqa: E402


def report(project_path: str) -> dict:
    out = {"project": project_path}
    if not os.path.isdir(os.path.join(project_path, ".git")) and \
            not os.path.isfile(os.path.join(project_path, ".git")):
        return {**out, "error": "not a git repository"}
    try:
        out["head_branch"] = gw.current_branch(project_path)
        out["head_sha"] = gw.rev_sha(project_path, "HEAD")
        out["configured_base_ref"] = gw.configured_base_ref(project_path)
        guess = gw.resolve_base_branch(project_path, "master")
        out["legacy_guess"] = guess
        out["legacy_guess_sha"] = gw.rev_sha(project_path, guess)
        behind = gw._run(project_path, ["rev-list", "--count",
                                        f"{out['legacy_guess_sha']}..{out['head_sha']}"]).strip()
        out["guess_behind_head"] = int(behind or 0)
        out["strict_verdict"] = gw._staleness(project_path, guess) or "ok"
    except gw.GitWriteError as e:
        out["error"] = str(e)[:300]
    return out


def main(argv=None) -> int:
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    for p in paths:
        print(json.dumps(report(os.path.realpath(p))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
