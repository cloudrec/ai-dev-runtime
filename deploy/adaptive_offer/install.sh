#!/usr/bin/env bash
# Replace the companion's hand-kept wake allowlist with identity-based non-payment offering.
#   install.sh --check   read-only: live/target SHA, diff, and how every live agent pane
#                        would be treated by the TARGET code (payment / offered / passed over)
#   install.sh --apply   backup, fast-forward live, install the drop-in, restart the companion
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; HERE=$(cd "$(dirname "$0")" && pwd)
TARGET_SHA=${TARGET_SHA:?set TARGET_SHA}
DROPDIR=/etc/systemd/system/owner-os-wake-companion.service.d; UNIT=owner-os-wake-companion
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse HEAD)
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward from ${h:0:7}"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"

check() {
  echo "live ${h:0:7} -> target $(git -C $RUNTIME rev-parse --short "$TARGET_SHA")"
  git -C $RUNTIME diff --stat HEAD "$TARGET_SHA" -- core tools api | tail -1
  T=$(mktemp -d); git -C $RUNTIME archive "$TARGET_SHA" core | tar -x -C "$T"
  ( cd "$T" && WAKE_BRIDGE_OFFER=nonpayment \
    WAKE_BRIDGE_ONLY_AGENTS=owner-os-supervisor-clean,owneros-taskboard-taskboard,fc-* \
    AGENT_CREATION_CONFIG=$RUNTIME/config/agent_creation.yaml \
    "$RUNTIME/venv/bin/python" -I -c '
import sys; sys.path.insert(0, ".")
from core import wake_bridge as wb
panes = wb.pane_cwds()
if panes is None: print("tmux unreadable: only the explicit list would be offered"); sys.exit()
for s in sorted(panes):
    a = f"{s}:0.0"
    if wb.payment_identity(a, "", "", panes[s]): v = "PAYMENT - never offered"
    elif wb.offer_allowed(a, "", "", panes=panes): v = "offered"
    else: v = "passed over (identity)"
    print(f"  {s:28} {v}")' )
  rm -rf "$T"
}
case "${1:-}" in
  --check) check ;;
  --apply)
    check
    B=$RUNTIME/backups/adaptive-offer-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$B"
    echo "$h" > "$B/live_sha"; cp -p $DROPDIR/99-tech-lead-hears.conf "$B/" 2>/dev/null || true
    git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
    install -m 0644 "$HERE/99-tech-lead-hears.conf" $DROPDIR/99-tech-lead-hears.conf
    systemctl daemon-reload; systemctl restart $UNIT; sleep 6
    systemctl is-active -q $UNIT || die "$UNIT did not start (rollback: $HERE/rollback.sh $B)"
    echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD); rollback: $HERE/rollback.sh $B" ;;
  *) die "usage: TARGET_SHA=<sha> $0 --check|--apply" ;;
esac
