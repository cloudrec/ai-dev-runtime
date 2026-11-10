#!/usr/bin/env bash
# Slots: an intake agent and a start slot only while a project has work in hand.
# Technical stops of sandboxed projects (leftovers in the copy, an agent's permission
# dialog) go to the ChatGPT tech lead instead of the owner. Push/network/privilege
# dialogs stay with the owner. Restarts the API, both full-cycle workers and the companion.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=85e080a; TARGET_SHA=dc7f6c7
UNITS="ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker owner-os-wake-companion"
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
