#!/usr/bin/env python3
"""Owner-only host CLI for Owner OS delegation grants (run as root over SSH, not via an agent).

  tools/owner_grant.py set-passphrase         # once: the owner approval passphrase (scrypt)
  tools/owner_grant.py list
  tools/owner_grant.py approve <grant id>     # shows the SERVER copy, asks the passphrase
  tools/owner_grant.py revoke <grant id>
  tools/owner_grant.py telegram-poll-once     # only if the owner Telegram channel is configured

`approve` needs the OWNER PASSPHRASE (verified inside automation_grants.approve against a
root-only scrypt hash). A terminal is required too, but the passphrase is what an agent —
even one that can drive a pseudo-terminal or call the Python API — does not have. Five
wrong attempts in 15 minutes lock approvals. Never tell the passphrase to ChatGPT or an agent.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv=None, stdin=None, stdout=None, isatty=None, getpass=None) -> int:
    from core import automation_grants as ag
    if getpass is None:
        import getpass as _gp
        getpass = _gp.getpass
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    isatty = isatty if isatty is not None else (sys.stdin.isatty() and sys.stdout.isatty())
    say = lambda *a: print(*a, file=stdout)  # noqa: E731
    if not argv:
        say(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "list":
        for g in ag.list_grants():
            say(f"{g['id']} {g['effective']:8} by={g['requested_by']} :: "
                f"{ag.summary(g['spec'])}")
        return 0
    if cmd == "revoke" and len(argv) == 2:
        ok = ag.revoke(argv[1], by="owner:cli")
        say("revoked" if ok else "not revocable")
        return 0 if ok else 1
    if cmd == "set-passphrase":
        if os.geteuid() != 0 or not isatty:
            say("refused: set-passphrase needs root on an interactive terminal")
            return 1
        p1 = getpass("New owner approval passphrase (>= 12 chars): ")
        if p1 != getpass("Repeat: "):
            say("refused: passphrases differ")
            return 1
        try:
            ag.set_passphrase(p1)
        except ag.GrantError as e:
            say(f"refused: {e}")
            return 1
        say(f"stored (scrypt) in {ag.passphrase_file()}")
        return 0
    if cmd == "telegram-poll-once":
        from core import owner_telegram as ot
        say(json.dumps(ot.poll_once()))
        return 0
    if cmd == "approve" and len(argv) == 2:
        if os.geteuid() != 0 or not isatty:
            say("refused: approve needs root on an interactive terminal")
            return 1
        g = ag.get(argv[1])
        if g is None or ag.effective_status(g) != "pending":
            say("refused: no such pending grant")
            return 1
        say(f"Grant {g['id']} requested by {g['requested_by']}:")
        say("  " + ag.summary(g["spec"]))
        say(f"  content {g['spec_hash'][:12]}")
        pw = getpass("Owner passphrase to approve (empty declines): ")
        if not pw:
            say("declined")
            return 1
        try:
            ag.approve(g["id"], via="owner_cli", expected_hash=g["spec_hash"], passphrase=pw)
        except ag.GrantError as e:
            say(f"refused: {e}")
            return 1
        say(f"approved; expires in {g['spec']['ttl_secs'] // 60} min")
        return 0
    say(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
