#!/usr/bin/env bash
# Undo install.sh --apply: restore configs/.env and the previous live commit, restart.
set -euo pipefail
RUNTIME=/root/ai-dev-runtime; B=${1:?backup dir}
UNITS="ai-runtime owner-os-wake-companion owner-os-fullcycle-worker owner-os-fullcycle-intake"
[ -f "$B/env" ] && [ -f "$B/live_sha" ] || { echo "STOP: not a backup dir" >&2; exit 1; }
cp -p "$B/env" $RUNTIME/configs/.env
rm -f /etc/systemd/system/owner-os-wake-companion.service.d/99-tech-lead-hears.conf
systemctl daemon-reload
git -C $RUNTIME reset -q --keep "$(cat "$B/live_sha")"
systemctl restart $UNITS
echo "ROLLED BACK: live $(git -C $RUNTIME rev-parse --short HEAD)"
