#!/usr/bin/env python3
"""Point the Owner OS wake bridge at a different ChatGPT conversation.

This is the ONE supported way to answer "the owner opened a new chat, where do I change
the link?". The answer must never again be a server-wide grep: the target is a single row
(`wake_target`, id=1) in the control plane database, `core.wake_bridge` is the only module
that writes it, and this script is the only operator-facing entry point that does so with a
backup and a verification attached.

What it does, in order, refusing to continue the moment a step fails:

  1. Validate the URL through `wake_bridge.valid_conversation` — the SAME predicate the
     bridge itself uses at wake time. A URL this script accepts can never be one the bridge
     later rejects as `active_chat_invalid`, because there is only one rule, not two.
  2. Show the current target, so a rebind is always a visible before/after rather than a
     silent overwrite.
  3. Back up the two pointer tables to a timestamped .sql file. Point backup, not a copy of
     the whole database: the database is 25MB of live control-plane state that other workers
     are writing to, and snapshotting all of it to change one row would be the larger risk.
  4. Call `wake_bridge.bind_chat` — the official API, which writes the row and the audit
     record in one transaction.
  5. Re-read `active_chat()` and assert it is the requested conversation. PASS is printed
     only on that evidence; anything else is FAIL with exit code 1.

Nothing here restarts a service, and nothing needs to. `owner-os-wake-companion` resolves
the target through `pending_wake()` on every tick, so a rebind takes effect on the next tick
(<= 20s) with the service untouched. See docs/OWNER_OS_CHAT_REBIND.md.

Usage:
    tools/rebind_chat.py https://chatgpt.com/c/<uuid> [--note "why"] [--by owner]
    tools/rebind_chat.py --show
    tools/rebind_chat.py <url> --dry-run     # validate + show, change nothing
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/ai-dev-runtime")

from core import wake_bridge as wb  # noqa: E402
from core import wake_routes  # noqa: E402
from core.control_plane.store import db_path  # noqa: E402

# The pointers and their audit trails. Deliberately NOT the whole database.
_BACKUP_TABLES = ("wake_target", "wake_bind_audit", "wake_route", "wake_route_audit")
_BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".ai-runtime-backups", "wake_target")


def backup_pointer(dest_dir: str = "", *, stamp: str = "") -> str:
    """Dump the pointer tables to a timestamped .sql file and return its path.

    `iterdump` is filtered rather than `.backup()`d because the goal is a file an operator
    can read and hand-replay: the old URL has to be recoverable by eye, not only by restore.

    The directory is resolved at CALL time, never captured in a default argument — otherwise
    the destination is frozen at import and a test that redirects it silently writes into
    the real backup tree instead.
    """
    dest_dir = dest_dir or _BACKUP_DIR
    os.makedirs(dest_dir, exist_ok=True)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(dest_dir, f"wake_target_{stamp}.sql")
    conn = sqlite3.connect(db_path())
    try:
        lines = [ln for ln in conn.iterdump() if any(t in ln for t in _BACKUP_TABLES)]
    finally:
        conn.close()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"-- wake_target point backup taken {datetime.now(timezone.utc).isoformat()}\n")
        fh.write(f"-- source: {db_path()}\n")
        fh.write("\n".join(lines) + "\n")
    return path


def _fmt_target(t: dict) -> str:
    if not t.get("bound"):
        return f"(none — {t.get('reason')})"
    return (f"{t['conversation']}\n    bound_at={t.get('bound_at')} "
            f"by={t.get('bound_by')} note={t.get('note') or '-'}")


def rebind(url: str, *, by: str = "owner", note: str = "", dry_run: bool = False,
           do_backup: bool = True, out=None) -> int:
    """Returns a process exit code: 0 on a verified rebind, 1 on any failure."""
    def say(msg: str = "") -> None:
        # Resolved per call, not captured as a default: a default would bind the stream at
        # import time and write past anything that redirects stdout afterwards.
        print(msg, file=out or sys.stdout)

    url = (url or "").strip()
    if not wb.valid_conversation(url):
        say(f"FAIL: not a conversation URL: {url[:120]}")
        say("       expected: https://chatgpt.com/c/<id>")
        return 1

    current = wb.active_chat()
    say(f"current target: {_fmt_target(current)}")
    say(f"new target    : {url}")

    if current.get("bound") and current["conversation"].rstrip("/") == url.rstrip("/"):
        say("PASS: already bound to this conversation — nothing to change.")
        return 0

    if dry_run:
        say("dry-run: URL is valid; nothing written.")
        return 0

    if do_backup:
        say(f"backup        : {backup_pointer()}")

    res = wb.bind_chat(url, by=by, note=note)
    if not res.get("ok"):
        say(f"FAIL: bind_chat refused: {res.get('reason')}")
        return 1
    say(f"bind          : {res['action']} (previous: {res.get('previous') or '-'})")

    # Verification is a fresh read, not a reuse of bind_chat's own return value — the point
    # is to prove what the bridge will READ on its next wake, not what the writer intended.
    after = wb.active_chat()
    if not after.get("bound"):
        say(f"FAIL: after rebind the target reads unbound: {after.get('reason')}")
        return 1
    if after["conversation"].rstrip("/") != url.rstrip("/"):
        say(f"FAIL: active target is {after['conversation']}, expected {url}")
        return 1

    say(f"verified      : {after['conversation']}")
    say("PASS: wake bridge is now bound to this conversation.")
    say("      owner-os-wake-companion picks it up on its next tick (<=20s); no restart.")
    return 0


def rebind_route(key: str, url: str, *, by: str = "owner", note: str = "",
                 dry_run: bool = False, do_backup: bool = True, out=None) -> int:
    """Bind one PROJECT route. Same shape as `rebind`: validate, show, backup, write via
    the canonical API, verify by a fresh resolve. Exit 0 only on that evidence."""
    def say(msg: str = "") -> None:
        print(msg, file=out or sys.stdout)

    k = wake_routes.normalize_key(key)
    if not k:
        say(f"FAIL: not a route key: {key[:64]!r} (expected e.g. mess, payment-orchestrator)")
        return 1
    url = (url or "").strip()
    if not wake_routes.valid_conversation(url):
        say(f"FAIL: not a conversation URL: {url[:120]}")
        say("       expected: https://chatgpt.com/c/<id>")
        return 1
    cur = wake_routes.get_route(k)
    say(f"route         : {k}")
    say(f"current target: {cur['conversation'] if cur else '(none)'}")
    say(f"new target    : {url}")
    if dry_run:
        say("dry-run: key and URL are valid; nothing written.")
        return 0
    if do_backup:
        say(f"backup        : {backup_pointer()}")
    res = wake_routes.bind_route(k, url, by=by, note=note)
    if not res.get("ok"):
        say(f"FAIL: bind_route refused: {res.get('reason')}")
        return 1
    say(f"bind          : {res['action']} (previous: {res.get('previous') or '-'})")
    after = wake_routes.resolve(project_id=k)
    if not (after.get("bound") and after["conversation"].rstrip("/") == url.rstrip("/")
            and after["route_key"] == k):
        say(f"FAIL: resolve({k}) returned {after}")
        return 1
    say(f"verified      : {after['conversation']} (route_reason={after['route_reason']})")
    say(f"PASS: events for project '{k}' now wake this conversation.")
    say("      owner-os-wake-companion picks it up on its next tick (<=20s); no restart.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="rebind_chat.py",
        description="Rebind Owner OS wake routing: the owner-os control chat by default, "
                    "or one project route with --route.")
    p.add_argument("url", nargs="?", help="https://chatgpt.com/c/<id>")
    p.add_argument("--route", default="", metavar="KEY",
                   help="bind this PROJECT route (event project_id, e.g. mess, gaika-drop, "
                        "payment-orchestrator) instead of the owner-os control chat")
    p.add_argument("--routes", action="store_true", help="print the route registry and exit")
    p.add_argument("--show", action="store_true", help="print the current target and exit")
    p.add_argument("--history", action="store_true", help="print the rebind audit and exit")
    p.add_argument("--by", default="owner", help="who requested it (audit field)")
    p.add_argument("--note", default="", help="why (audit field, never chat content)")
    p.add_argument("--dry-run", action="store_true", help="validate and show, change nothing")
    p.add_argument("--no-backup", action="store_true", help="skip the point backup")
    a = p.parse_args(argv)

    if a.routes:
        rows = wake_routes.list_routes()
        if not rows:
            print("(no routes bound — everything falls back to the owner-os target)")
        for r in rows:
            print(f"{r['route_key']:24} {r['conversation']}  bound {r['bound_at'][:19]} "
                  f"by {r['bound_by']}")
        print(f"fallback route key: {wake_routes.FALLBACK_ROUTE}")
        return 0
    if a.show:
        print(f"current target: {_fmt_target(wb.active_chat())}")
        return 0
    if a.history:
        for h in wb.bind_history():
            print(f"{h['at'][:19]}  {h['action']:6}  {h['conversation']}  "
                  f"(was: {h['previous'] or '-'})")
        for h in wake_routes.route_history():
            print(f"{h['at'][:19]}  {h['action']:6}  [{h['route_key']}] "
                  f"{h['conversation'] or '-'}  (was: {h['previous'] or '-'})")
        return 0
    if not a.url:
        p.error("a conversation URL is required (or use --show / --history / --routes)")
    if a.route and wake_routes.normalize_key(a.route) != wake_routes.FALLBACK_ROUTE:
        return rebind_route(a.route, a.url, by=a.by, note=a.note, dry_run=a.dry_run,
                            do_backup=not a.no_backup)
    return rebind(a.url, by=a.by, note=a.note, dry_run=a.dry_run,
                  do_backup=not a.no_backup)


if __name__ == "__main__":
    raise SystemExit(main())
