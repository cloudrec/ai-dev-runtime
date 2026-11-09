#!/usr/bin/env bash
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
tmux kill-session -t =fruit-migration 2>/dev/null
sed -i 's/,fruit-migration@api//' /root/ai-dev-runtime/configs/.env
systemctl restart ai-runtime
echo "stopped fruit-migration, allowlist back; /opt/fruit-migration left in place"
