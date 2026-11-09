#!/usr/bin/env bash
# A pane the intake worker stops is not a crash: fast-forward live to the fix and
# restart the full-cycle worker only. Owner runs this.
#
#   fix_unwatch.sh --check    read-only
#   fix_unwatch.sh --apply    deploys; prints the rollback line
set -euo pipefail
umask 022

RUNTIME=/root/ai-dev-runtime
LIVE_SHA=68895a0
TARGET_SHA=b24b66b
UNIT=owner-os-fullcycle-worker

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

check() {
  [ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
  [ -z "$(git -C $RUNTIME diff --name-only HEAD "$TARGET_SHA" -- api)" ] || die "target changes the API"
  systemctl is-active -q $UNIT || die "$UNIT is not active"
  echo "check ok: live $LIVE_SHA -> $TARGET_SHA, restarts $UNIT only"
}

check
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"

ROLLBACK="git -C $RUNTIME reset --keep $LIVE_SHA && systemctl restart $UNIT"
trap 'echo; echo "FAILED. Rollback: '"$ROLLBACK"'" >&2' ERR
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart $UNIT
sleep 3
systemctl is-active -q $UNIT || die "$UNIT did not start"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), $UNIT restarted"
echo "Rollback: $ROLLBACK"
