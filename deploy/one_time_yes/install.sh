#!/usr/bin/env bash
# An agent's permission dialog is answered only with that dialog's one-time "yes", never
# an option that keeps allowing (Claude's new file-read dialog has "1. Yes, and keep
# allowing any reads" first). File reads inside /opt/fc-projects go to the ChatGPT tech
# lead; other reads and tool requests stay with the owner. An owner gate opened for a
# dialog that the lead may take now moves to the lead. Restarts both full-cycle workers.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=dc7f6c7; TARGET_SHA=be3cbd3
UNITS="owner-os-fullcycle-intake owner-os-fullcycle-worker"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD)
[ "$h" = "$LIVE_SHA" ] || [ "$h" = "${TARGET_SHA:0:7}" ] || die "live is at $h, expected $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart $UNITS
sleep 5
for u in $UNITS; do systemctl is-active -q $u || die "$u did not start"; done
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD)"
echo "Rollback: git -C $RUNTIME reset --keep $LIVE_SHA && systemctl restart $UNITS"
