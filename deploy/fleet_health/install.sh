#!/bin/bash
# Install/enable the Owner OS fleet health monitor (server-down Telegram alerting).
#
# Safe to re-run. Only touches:
#   - /etc/systemd/system/owner-os-fleet-health.{service,timer}  (copied from this repo)
#   - systemd daemon-reload / enable --now on the TIMER only (never the service itself,
#     which is Type=oneshot and is meant to be started BY the timer)
#
# Does NOT touch payment services, DB, Patroni, etcd, DNS, firewall, WireGuard, or
# restart any production workload. Management-host-local only.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_SRC="$REPO_DIR/deploy/fleet_health"
UNIT_DST=/etc/systemd/system

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (writes to $UNIT_DST)" >&2
  exit 1
fi

if [ ! -f "$REPO_DIR/configs/.env" ] || ! grep -q '^TELEGRAM_BOT_TOKEN=' "$REPO_DIR/configs/.env" \
    || ! grep -q '^TELEGRAM_CHAT_ID=' "$REPO_DIR/configs/.env"; then
  echo "ERROR: $REPO_DIR/configs/.env missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID." >&2
  echo "See deploy/fleet_health/fleet_health.env.example. Refusing to install a" >&2
  echo "monitor that cannot alert." >&2
  exit 1
fi

mkdir -p "$REPO_DIR/state"

install -m 0644 "$UNIT_SRC/owner-os-fleet-health.service" "$UNIT_DST/owner-os-fleet-health.service"
install -m 0644 "$UNIT_SRC/owner-os-fleet-health.timer" "$UNIT_DST/owner-os-fleet-health.timer"

systemctl daemon-reload
systemctl enable --now owner-os-fleet-health.timer

echo "--- timer status ---"
systemctl status owner-os-fleet-health.timer --no-pager -l || true
echo "--- next run ---"
systemctl list-timers owner-os-fleet-health.timer --no-pager || true

echo
echo "To run one check immediately (does not wait for the timer):"
echo "  systemctl start owner-os-fleet-health.service && journalctl -u owner-os-fleet-health.service -n 30 --no-pager"
