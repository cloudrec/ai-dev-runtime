#!/usr/bin/env bash
# The release-link tool reads its settings from the worker unit. Code only, no restart.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=cbc219a; TARGET_SHA=99f2034
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || [ "$h" = 5a5bde0 ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD)"
