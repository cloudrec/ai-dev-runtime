#!/usr/bin/env python3
"""Owner only: the phone page link.

    owner_page.py url       print the link (makes the key on first use)
    owner_page.py rotate    new key: the old link stops working at once
    owner_page.py dismiss-finished   every project releasable now: not to be released alone
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fullcycle import owner_page  # noqa: E402


def _base_from_unit() -> None:
    if os.getenv("FULLCYCLE_OWNER_PAGE_BASE"):
        return
    try:
        out = subprocess.run(["systemctl", "show", "owner-os-wake-companion", "-p",
                              "Environment", "--value"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for item in out.split():
        k, _, v = item.partition("=")
        if k == "FULLCYCLE_OWNER_PAGE_BASE" and v:
            os.environ[k] = v


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["url"], ["rotate"], ["dismiss-finished"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("STOP: run as root", file=sys.stderr)
        return 2
    _base_from_unit()
    if args == ["dismiss-finished"]:
        for pid in owner_page.dismiss_finished():
            print(f"not to be released on its own: {pid}")
        return 0
    if args == ["rotate"] or not owner_page.secret():
        owner_page.rotate()
    link = owner_page.url()
    if not link:
        print("STOP: FULLCYCLE_OWNER_PAGE_BASE is not set", file=sys.stderr)
        return 1
    print(link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
