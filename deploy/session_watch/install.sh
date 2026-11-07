#!/bin/bash
# Owner-run. Installs the ChatGPT session alert on the live runtime.
#   install.sh --test-telegram   sends one real test message, changes nothing
#   install.sh --whoami          shows the bot and which chat it sends to, changes nothing
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

whoami() {
  cd "$LIVE"
  "$PY" - <<'P'
import json, urllib.error, urllib.parse, urllib.request
env = {}
for line in open("configs/.env", encoding="utf-8"):
    k, _, v = line.strip().partition("=")
    if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        env[k] = v.strip().strip('"').strip("'")
def call(method, **params):
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/{method}"
    try:
        with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": json.loads(e.read().decode() or "{}").get("description")}
    except Exception as e:
        return {"ok": False, "description": type(e).__name__}
def name(c):
    return " ".join(str(c[k]) for k in ("title", "first_name", "last_name", "username") if c.get(k))
me = call("getMe")
print("bot:", "@" + me["result"]["username"] if me.get("ok") else me.get("description"))
c = call("getChat", chat_id=env["TELEGRAM_CHAT_ID"])
print("sends to:", f"{c['result']['id']} {c['result']['type']} {name(c['result'])}" if c.get("ok")
      else c.get("description"))
u = call("getUpdates", limit=100, timeout=0)
seen = {}
for x in u.get("result", []):
    m = x.get("message") or x.get("my_chat_member") or {}
    if m.get("chat"):
        seen[m["chat"]["id"]] = m["chat"]
print("wrote to the bot recently:" if seen else f"nobody wrote to the bot recently ({u.get('description', 'no updates')})")
for i, ch in seen.items():
    print(" ", i, ch.get("type"), name(ch))
P
}

case "${1:-}" in
  --whoami)
    whoami
    ;;
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
  *) echo "usage: install.sh --test-telegram | --whoami | --apply"; exit 2 ;;
esac
