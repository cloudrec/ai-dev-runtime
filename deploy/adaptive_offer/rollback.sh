#!/usr/bin/env bash
set -euo pipefail
RUNTIME=/root/ai-dev-runtime; B=${1:?backup dir}; DROP=/etc/systemd/system/owner-os-wake-companion.service.d
[ -f "$B/live_sha" ] || { echo "STOP: not a backup dir" >&2; exit 1; }
if [ -f "$B/99-tech-lead-hears.conf" ]; then cp -p "$B/99-tech-lead-hears.conf" $DROP/; else rm -f $DROP/99-tech-lead-hears.conf; fi
git -C $RUNTIME reset -q --keep "$(cat "$B/live_sha")"
systemctl daemon-reload; systemctl restart owner-os-wake-companion
echo "ROLLED BACK: live $(git -C $RUNTIME rev-parse --short HEAD)"
