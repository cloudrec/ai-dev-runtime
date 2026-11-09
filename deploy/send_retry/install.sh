#!/usr/bin/env bash
# agent_send retries Enter with back-off. Restarts the API and both full-cycle workers. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=9d4867a; TARGET_SHA=ec99bf2
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker
sleep 4
for u in ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker; do systemctl is-active -q $u || die "$u did not start"; done
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD)"
echo "Rollback: git -C $RUNTIME reset --keep $LIVE_SHA && systemctl restart ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker"
