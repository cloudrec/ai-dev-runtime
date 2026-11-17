#!/usr/bin/env bash
# Replace the companion's hand-kept wake allowlist with identity-based non-payment offering.
#   install.sh --check   read-only: commits and files live->target, and how every live
#                        pane (session, cwd) would be treated by the TARGET code
#   install.sh --apply   backup, fast-forward live, WAKE_BRIDGE_IDENTITY=1 in configs/.env,
#                        drop-in for the companion, restart the 4 units, verify env
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; HERE=$(cd "$(dirname "$0")" && pwd); ENVF=$RUNTIME/configs/.env
TARGET_SHA=${TARGET_SHA:?set TARGET_SHA}
DROPDIR=/etc/systemd/system/owner-os-wake-companion.service.d
UNITS="ai-runtime owner-os-wake-companion owner-os-fullcycle-worker owner-os-fullcycle-intake"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse HEAD)
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward from ${h:0:7}"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

check() {
  echo "live ${h:0:7} -> target $(git -C $RUNTIME rev-parse --short "$TARGET_SHA")"
  echo "commits:"; git -C $RUNTIME log --oneline "HEAD..$TARGET_SHA" | sed 's/^/  /'
  echo "files:"; git -C $RUNTIME diff --name-only HEAD "$TARGET_SHA" | sed 's/^/  /'
  git -C $RUNTIME archive "$TARGET_SHA" core | tar -x -C "$T"
  ( cd "$T" && WAKE_BRIDGE_OFFER=nonpayment \
    WAKE_BRIDGE_ONLY_AGENTS=owner-os-supervisor-clean,owneros-taskboard-taskboard \
    AGENT_CREATION_CONFIG=$RUNTIME/config/agent_creation.yaml \
    "$RUNTIME/venv/bin/python" -I -c '
import sys; sys.path.insert(0, ".")
from core import wake_bridge as wb
panes = wb.pane_cwds()
if panes is None: print("tmux unreadable: only the explicit list would be offered"); sys.exit()
print("panes (target code, nonpayment):")
for s in sorted(panes):
    a, cwds = f"{s}:0.0", panes[s]
    if wb.payment_identity(a, "", "", cwds): v = "PAYMENT - never offered"
    elif wb.offer_allowed(a, "", "", panes=panes): v = "offered"
    else: v = "passed over (identity)"
    print(f"  {s:28} {v:26} {chr(32).join(cwds)}")' )
}
case "${1:-}" in
  --check) check ;;
  --apply)
    check
    B=$RUNTIME/backups/adaptive-offer-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$B"
    echo "$h" > "$B/live_sha"; cp -p $ENVF "$B/env"
    cp -p $DROPDIR/99-tech-lead-hears.conf "$B/" 2>/dev/null || true
    trap 'rm -rf "$T"; echo "FAILED mid-apply. Rollback: $HERE/rollback.sh $B" >&2' ERR
    git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
    grep -qE '^WAKE_BRIDGE_IDENTITY=' $ENVF && sed -i 's/^WAKE_BRIDGE_IDENTITY=.*/WAKE_BRIDGE_IDENTITY=1/' $ENVF \
      || echo 'WAKE_BRIDGE_IDENTITY=1' >> $ENVF
    install -m 0644 "$HERE/99-tech-lead-hears.conf" $DROPDIR/99-tech-lead-hears.conf
    systemctl daemon-reload; systemctl restart $UNITS; sleep 8
    for u in $UNITS; do systemctl is-active -q $u || die "$u did not start (rollback: $HERE/rollback.sh $B)"; done
    pid=$(systemctl show owner-os-wake-companion -p MainPID --value)
    tr '\0' '\n' < /proc/$pid/environ | grep -qx 'WAKE_BRIDGE_OFFER=nonpayment' || die "companion env lacks WAKE_BRIDGE_OFFER (rollback: $HERE/rollback.sh $B)"
    tr '\0' '\n' < /proc/$pid/environ | grep -qx 'WAKE_BRIDGE_IDENTITY=1' || die "companion env lacks WAKE_BRIDGE_IDENTITY (rollback: $HERE/rollback.sh $B)"
    echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD); rollback: $HERE/rollback.sh $B" ;;
  *) die "usage: TARGET_SHA=<sha> $0 --check|--apply" ;;
esac
