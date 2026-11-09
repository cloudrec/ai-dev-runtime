#!/usr/bin/env bash
# The sandboxed worker's reviews and questions wake ChatGPT again (WAKE_BRIDGE_ENABLED=1 was
# missing from its unit). Restarts that worker only; agents keep running. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
U=owner-os-fullcycle-intake
D=/etc/systemd/system/$U.service.d/10-wake.conf
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
mkdir -p "$(dirname "$D")"
printf '[Service]\nEnvironment=WAKE_BRIDGE_ENABLED=1\n' > "$D"
systemctl daemon-reload
systemctl restart $U
sleep 3
P=$(systemctl show $U -p MainPID --value)
tr '\0' '\n' < /proc/$P/environ | grep -qx 'WAKE_BRIDGE_ENABLED=1' || { echo "STOP: not in effect" >&2; exit 1; }
echo "DONE: $U wakes ChatGPT again. Rollback: rm $D && systemctl daemon-reload && systemctl restart $U"
