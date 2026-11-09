#!/usr/bin/env bash
# loop watch: no false "undelivered" alarm for latched old wakes. Restarts the companion only.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=c9a82a5; TARGET_SHA=81cbd2f
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || [ "$h" = c8606ee ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart owner-os-wake-companion
sleep 3; systemctl is-active -q owner-os-wake-companion || die "companion did not start"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD)"
