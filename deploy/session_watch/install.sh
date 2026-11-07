#!/bin/bash
# Owner-run. Installs the ChatGPT session alert on the live runtime.
#   install.sh --test-telegram   sends one real test message, changes nothing
#   install.sh --apply           fast-forwards live, restarts the wake companion only
set -euo pipefail
LIVE=/root/ai-dev-runtime
FROM=319012a
TARGET=308ac8f
PY=$LIVE/venv/bin/python

telegram() {
  cd "$LIVE"
  "$PY" - "$1" <<'P'
import os, sys
sys.path.insert(0, ".")
for line in open("configs/.env", encoding="utf-8"):
    k, _, v = line.strip().partition("=")
    if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ[k] = v.strip().strip('"').strip("'")
from core.control_plane.delivery import _send_owner_push
ok, receipt, error = _send_owner_push(sys.argv[1])
print("telegram:", "ok" if ok else f"FAILED: {error}")
sys.exit(0 if ok else 1)
P
}

case "${1:-}" in
  --test-telegram)
    telegram "Owner OS: проверка канала. Если ты это видишь, сигнал дойдёт."
    ;;
  --apply)
    [ "$(git -C "$LIVE" rev-parse --short HEAD)" = "$FROM" ] || { echo "live is not $FROM"; exit 1; }
    git -C "$LIVE" merge --ff-only "$TARGET" >/dev/null
    systemctl restart owner-os-wake-companion
    python3 -c 'import time; time.sleep(5)'
    systemctl is-active owner-os-wake-companion
    echo "DONE: live $(git -C "$LIVE" rev-parse --short HEAD)"
    echo "Rollback: git -C $LIVE reset --keep $FROM && systemctl restart owner-os-wake-companion"
    ;;
  *) echo "usage: install.sh --test-telegram | --apply"; exit 2 ;;
esac
