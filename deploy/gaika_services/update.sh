#!/usr/bin/env bash
# Owner runs this: move the live GAIKA services code (/opt/gaika) to a trunk SHA (default bab51fec: + Aldi UK hold, myTime.de, Costco).
# Gains: crawl holds (Ahorramás, Linella, Condis, Lidl ES, Polski Koszyk, Collect&Go), non-image reject,
# 512/160 WebP copies for new images, 8 new crawlers. Plan: /opt/gaika-worktrees/trunk/reports/GAIKA_SERVICES_UPDATE_PLAN_2026-11-11.md
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CODE=/opt/gaika; FROM=2b83ee79; TO=${1:-bab51fec}; V=/opt/gaika-worktrees/ru/var
UNITS="gaika-refresh gaika-images gaika-dist-server gaika-log-receiver"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
G="git -c safe.directory=* -C $CODE"
cur=$($G rev-parse --short=8 HEAD)
[ "$cur" = "$FROM" ] || [ "$cur" = "$TO" ] || die "/opt/gaika is at $cur, expected $FROM"
FROM=$cur
[ -z "$($G status --porcelain --untracked-files=no)" ] || die "/opt/gaika has local edits"
$G fetch -q /opt/gaika-worktrees/trunk integration/trunk-20261110
$G cat-file -e "$TO^{commit}" || die "no $TO after fetch"
$G checkout -q --detach "$TO"
systemctl restart $UNITS
sleep 10
for u in $UNITS; do systemctl is-active -q $u || { echo "$u failed, rolling back"; $G checkout -q --detach $FROM; systemctl restart $UNITS; die "rolled back to $FROM: journalctl -u $u"; }; done
if ss -ltn | grep -qE '0\.0\.0\.0:893[01]'; then die "a port is public"; fi
echo "DONE: /opt/gaika at $($G rev-parse --short=8 HEAD), units active"
echo "Rollback: git -C $CODE checkout --detach $FROM && systemctl restart $UNITS"
