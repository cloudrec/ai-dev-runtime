#!/usr/bin/env bash
# The sandboxed worker can start its first agent. Restarts that worker only. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=50e90f9; TARGET_SHA=9d4867a
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart owner-os-fullcycle-intake
sleep 3; systemctl is-active -q owner-os-fullcycle-intake || die "worker did not start"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), sandboxed worker restarted"
echo "Rollback: git -C $RUNTIME reset --keep $LIVE_SHA && systemctl restart owner-os-fullcycle-intake"
