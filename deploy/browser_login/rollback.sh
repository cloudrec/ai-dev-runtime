#!/usr/bin/env bash
set -euo pipefail
BK=${1:?usage: rollback.sh <backup-dir>}; [ -d "$BK" ] || { echo "no $BK" >&2; exit 1; }
PORT=$(cat "$BK/port")
XDROP=/etc/systemd/system/owner-os-x11vnc.service.d/50-passwdfile.conf
CDROP=/etc/systemd/system/owner-os-wake-companion.service.d/99-session-login-link.conf
tailscale serve --http="$PORT" off >/dev/null 2>&1 || true
if [ -f "$BK/50-passwdfile.conf" ]; then cp -p "$BK/50-passwdfile.conf" "$XDROP"; else rm -f "$XDROP"; fi
if [ -f "$BK/99-session-login-link.conf" ]; then cp -p "$BK/99-session-login-link.conf" "$CDROP"; else rm -f "$CDROP"; fi
if [ -f "$BK/vncpasswd.txt.prev" ]; then cp -p "$BK/vncpasswd.txt.prev" /home/owneros/.vncpasswd.txt; else rm -f /home/owneros/.vncpasswd.txt; fi
systemctl daemon-reload
systemctl restart owner-os-x11vnc owner-os-wake-companion
echo "rolled back: noVNC local only, x11vnc on its original -rfbauth file, no login link"
