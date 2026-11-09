#!/usr/bin/env bash
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
git -C $RUNTIME reset --keep 13d99da
systemctl restart ai-runtime owner-os-wake-companion owner-os-fullcycle-intake owner-os-fullcycle-worker
git -C $SEO reset --keep a850bb3
docker tag seo-backend:rollback-pre-chat-rotation seo-backend:latest
( cd $SEO && docker compose up -d --no-deps backend )
echo "rolled back: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
