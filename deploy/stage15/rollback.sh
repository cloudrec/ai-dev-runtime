#!/usr/bin/env bash
# Puts Owner OS and the seo MCP back. Takes the backup dir install.sh printed. Prints no token.
# DBs are left alone: the deploy only adds tables, and restoring would lose events since.
set -euo pipefail
B=${1:?usage: rollback.sh <backup dir>}
[ -f "$B/seo.env" ] || { echo "no backup at $B" >&2; exit 1; }
RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
git -C $RUNTIME reset --keep 955700417adb899e32160e738f942e0a7ba4544f
rm -f /etc/systemd/system/ai-runtime.service.d/97-fullcycle-reviewers.conf \
      /etc/ai-runtime/runtime-clients.json
systemctl daemon-reload
systemctl restart ai-runtime owner-os-fullcycle-worker owner-os-wake-companion
git -C $SEO reset --keep 7039d6c
cp -p "$B/seo.env" $SEO/.env
docker tag seo-backend:rollback-pre-stage15 seo-backend:latest
( cd $SEO && docker compose up -d --no-deps backend )
echo "rolled back: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
