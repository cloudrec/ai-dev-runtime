#!/usr/bin/env bash
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
rm -f /etc/systemd/system/owner-os-wake-companion.service.d/30-owner-page.conf
systemctl daemon-reload
git -C /root/ai-dev-runtime reset --keep 99f2034
systemctl restart ai-runtime owner-os-wake-companion owner-os-fullcycle-intake owner-os-fullcycle-worker
git -C /opt/seo reset --keep 59e5b1a
docker tag seo-backend:rollback-pre-owner-page seo-backend:latest
( cd /opt/seo && docker compose up -d --no-deps backend )
echo "rolled back (page key file left in place, unused)"
