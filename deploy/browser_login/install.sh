#!/usr/bin/env bash
# Owner can log the wake browser back into ChatGPT from his own devices.
#   * noVNC (127.0.0.1:6080) served on the TAILNET ONLY: tailscale serve, never funnel;
#   * no VNC password, by the owner's decision; VNC itself stays on localhost;
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
public() { tailscale serve status --json | python3 -c "import json,sys; d=json.load(sys.stdin) or {}; sys.exit(0 if any(k.endswith(':$PORT') and v for k, v in (d.get('AllowFunnel') or {}).items()) else 1)"; }
public && die "port $PORT is on FUNNEL (public) - refusing"
grep -q 'login_url' "$RT/core/chatgpt_session_watch.py" || die "runtime lacks login_url (deploy the runtime first)"
grep -qE '^TELEGRAM_(BOT_TOKEN|CHAT_ID)=' "$RT/configs/.env" || die "Telegram not configured"
echo "ok: noVNC -> tailnet only at $URL; x11vnc without password; companion gets the link"
[ "$mode" = --check ] && { echo "CHECK OK (no change made)"; exit 0; }

BK="$RT/backups/browser-login-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$BK"; chmod 700 "$BK"
[ -f "$PWF" ] && cp -p "$PWF" "$BK/vncpasswd.txt.prev"
[ -f "$XDROP" ] && cp -p "$XDROP" "$BK/" ; [ -f "$CDROP" ] && cp -p "$CDROP" "$BK/"
echo "$PORT" > "$BK/port"
# 2. x11vnc without a password (owner 2026-11-17: tailnet-only is the guard; local
#    users can already drive the browser over CDP, so a VNC password added nothing)
mkdir -p "$(dirname "$XDROP")"
cat > "$XDROP" <<CONF
[Service]
ExecStart=
ExecStart=/usr/bin/x11vnc -display :99 -nopw -localhost -rfbport 5901 -forever -shared -noxdamage
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
public && die "unexpectedly public - run rollback.sh $BK"
echo "DONE: $URL (tailnet only); rollback: $(dirname "$0")/rollback.sh $BK"
