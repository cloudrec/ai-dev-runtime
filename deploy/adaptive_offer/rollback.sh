#!/usr/bin/env bash
# Undo install.sh --apply: previous drop-in, configs/.env and live commit; restart the same units.
set -euo pipefail
RUNTIME=/root/ai-dev-runtime; B=${1:?backup dir}; DROP=/etc/systemd/system/owner-os-wake-companion.service.d
UNITS="ai-runtime owner-os-wake-companion owner-os-fullcycle-worker owner-os-fullcycle-intake"
[ -f "$B/live_sha" ] && [ -f "$B/env" ] || { echo "STOP: not a backup dir" >&2; exit 1; }
if [ -f "$B/99-tech-lead-hears.conf" ]; then cp -p "$B/99-tech-lead-hears.conf" $DROP/; else rm -f $DROP/99-tech-lead-hears.conf; fi
cp -p "$B/env" $RUNTIME/configs/.env
git -C $RUNTIME reset -q --keep "$(cat "$B/live_sha")"
# put back live restrictions that were not in git at apply time (e.g. the vtb deny)
if [ -f "$B/live/LIST" ]; then while read -r f; do cp -p "$B/live/$(basename "$f")" "$RUNTIME/$f"; done < "$B/live/LIST"; fi
grep -q '/opt/vtb_old' $RUNTIME/config/agent_creation.yaml || { echo "STOP: vtb deny missing after rollback" >&2; exit 1; }
systemctl daemon-reload; systemctl restart $UNITS
echo "ROLLED BACK: live $(git -C $RUNTIME rev-parse --short HEAD)"
