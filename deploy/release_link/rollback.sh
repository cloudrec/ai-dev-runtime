#!/usr/bin/env bash
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
rm -f /etc/systemd/system/owner-os-fullcycle-intake.service.d/20-release-link.conf
systemctl daemon-reload
git -C /root/ai-dev-runtime reset --keep f9cde5c
systemctl restart ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker
git -C /opt/seo reset --keep d1aa013
docker tag seo-backend:rollback-pre-release-link seo-backend:latest
( cd /opt/seo && docker compose up -d --no-deps backend )
echo "rolled back (the key file stays, unused)"
