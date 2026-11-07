#!/usr/bin/env bash
# Undoes deploy/intake/install.sh. Projects already made in /opt/fc-projects are kept.
set -euo pipefail
RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^fc-' || true); do
  tmux kill-session -t "=$s"
done
rm -f /etc/systemd/system/ai-runtime.service.d/98-fullcycle-intake.conf \
      /etc/systemd/system/owner-os-fullcycle-worker.service.d/98-fullcycle-intake.conf \
      /etc/systemd/system/owner-os-wake-companion.service.d/98-fullcycle-intake.conf
systemctl daemon-reload
git -C $RUNTIME reset --keep 308ac8f
systemctl restart ai-runtime owner-os-fullcycle-worker owner-os-wake-companion
git -C $SEO reset --keep 5253d7c
docker tag seo-backend:rollback-pre-intake seo-backend:latest
( cd $SEO && docker compose up -d --no-deps backend )
echo "rolled back: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
