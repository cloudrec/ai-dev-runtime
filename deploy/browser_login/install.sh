#!/usr/bin/env bash
# Owner can log the wake browser back into ChatGPT from his own devices.
#   * noVNC (127.0.0.1:6080) served on the TAILNET ONLY: tailscale serve, never funnel;
#   * a fresh VNC password, 600 file owned by owneros, read by x11vnc via -passwdfile;
#     the password is sent to the owner by Telegram only and is never printed or put
#     in a process argument;
#   * the companion's "logged out" alert carries the link (CHATGPT_SESSION_LOGIN_URL).
#   install.sh --check | --apply ; rollback.sh <backup-dir>
set -euo pipefail
mode=${1:-}; [ "$mode" = --check ] || [ "$mode" = --apply ] || { echo "usage: $0 --check|--apply" >&2; exit 2; }
die() { echo "ABORT: $*" >&2; exit 1; }
PORT=6080
HOST=$(tailscale status --self --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
URL="http://$HOST:$PORT/vnc.html?autoconnect=1&resize=remote"
PWF=/home/owneros/.vncpasswd.txt
XDROP=/etc/systemd/system/owner-os-x11vnc.service.d/50-passwdfile.conf
CDROP=/etc/systemd/system/owner-os-wake-companion.service.d/99-session-login-link.conf
RT=/root/ai-dev-runtime
[ -n "$HOST" ] || die "tailscale has no DNS name"
tailscale status >/dev/null || die "tailscale is not up"
systemctl is-active -q owner-os-x11vnc owner-os-novnc || die "x11vnc/novnc not active"
[ -f /usr/share/novnc/vnc.html ] || die "noVNC web files missing"
tailscale serve status --json | python3 -c "import json,sys; d=json.load(sys.stdin) or {}; sys.exit(1 if '$PORT' in (d.get('TCP') or {}) else 0)" \
  || echo "note: tailnet port $PORT is already served (re-apply)"
tailscale funnel status 2>/dev/null | grep -q ":$PORT" && die "port $PORT is on FUNNEL (public) - refusing"
grep -q 'login_url' "$RT/core/chatgpt_session_watch.py" || die "runtime lacks login_url (deploy the runtime first)"
grep -qE '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$RT/configs/.env" || die "Telegram not configured"
echo "ok: noVNC -> tailnet only at $URL; x11vnc gets -passwdfile; companion gets the link"
[ "$mode" = --check ] && { echo "CHECK OK (no change made)"; exit 0; }

BK="$RT/backups/browser-login-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$BK"; chmod 700 "$BK"
[ -f "$PWF" ] && cp -p "$PWF" "$BK/vncpasswd.txt.prev"
[ -f "$XDROP" ] && cp -p "$XDROP" "$BK/" ; [ -f "$CDROP" ] && cp -p "$CDROP" "$BK/"
echo "$PORT" > "$BK/port"
# 1. password: generated in python, written 600, sent by Telegram, never echoed
umask 077
"$RT/venv/bin/python" - "$PWF" "$URL" <<'PY'
import os, secrets, string, sys, pwd
path, url = sys.argv[1], sys.argv[2]
pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))  # VNC uses 8
u = pwd.getpwnam("owneros")
fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(pw + "\n")
os.chown(path + ".new", u.pw_uid, u.pw_gid)
os.replace(path + ".new", path)
for line in open("/root/ai-dev-runtime/configs/.env", encoding="utf-8"):
    k, _, v = line.strip().partition("=")
    if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ[k] = v.strip().strip('"').strip("'")
sys.path.insert(0, "/root/ai-dev-runtime")
from core.control_plane.delivery import _send_owner_push
ok, _, err = _send_owner_push(
    "Браузер ChatGPT для входа (только с твоих устройств в Tailscale):\n" + url +
    "\nПароль VNC: " + pw + "\nНужен, только если слетел вход в ChatGPT.")
print("telegram:", "sent" if ok else f"FAILED {str(err)[:80]}")
sys.exit(0 if ok else 3)
PY
# 2. x11vnc reads the new file
mkdir -p "$(dirname "$XDROP")"
cat > "$XDROP" <<CONF
[Service]
ExecStart=
ExecStart=/usr/bin/x11vnc -display :99 -passwdfile $PWF -localhost -rfbport 5901 -forever -shared -noxdamage
CONF
# 3. tailnet only
tailscale serve --bg --http="$PORT" "http://127.0.0.1:$PORT" >/dev/null
# 4. alert link
printf '[Service]\nEnvironment="CHATGPT_SESSION_LOGIN_URL=%s"\n' "$URL" > "$CDROP"
systemctl daemon-reload
systemctl restart owner-os-x11vnc owner-os-wake-companion
sleep 3
systemctl is-active -q owner-os-x11vnc owner-os-wake-companion owner-os-chromium || die "a unit is not active - run rollback.sh $BK"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://$HOST:$PORT/vnc.html")
[ "$code" = 200 ] || die "noVNC over tailnet answered $code - run rollback.sh $BK"
tailscale funnel status 2>/dev/null | grep -q ":$PORT" && die "unexpectedly public - run rollback.sh $BK"
echo "DONE: $URL (tailnet only); rollback: $(dirname "$0")/rollback.sh $BK"
