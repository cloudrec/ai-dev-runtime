#!/usr/bin/env bash
# loop watch: same canary filter as the bridge. Restarts the companion only.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=caef919; TARGET_SHA=f9cde5c
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || [ "$h" = 1bc8fa3 ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart owner-os-wake-companion
sleep 3; systemctl is-active -q owner-os-wake-companion || die "companion did not start"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD)"
