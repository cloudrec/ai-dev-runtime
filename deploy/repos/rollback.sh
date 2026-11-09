#!/usr/bin/env bash
# Undo deploy/repos/install.sh. Projects already copied under /opt/fc-projects stay.
set -uo pipefail
RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
rm -f /etc/systemd/system/owner-os-fullcycle-worker.service.d/99-fullcycle-telegram.conf
rm -f /etc/ai-runtime/fullcycle-telegram.env
systemctl daemon-reload
git -C $RUNTIME reset --keep b24b66b
systemctl restart ai-runtime owner-os-fullcycle-worker
git -C $SEO reset --keep 0aa58b2
docker tag seo-backend:rollback-pre-repos seo-backend:latest
( cd $SEO && docker compose up -d --no-deps backend )
echo "rolled back: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
