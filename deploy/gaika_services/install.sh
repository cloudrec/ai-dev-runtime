#!/usr/bin/env bash
# GAIKA server side as permanent services (owner said yes 2026-11-10):
#   gaika-refresh      catalog refresh daemon (robots/access-check/polite pace are in the code)
#   gaika-images       image fetcher
#   gaika-dist-server  catalog snapshots, now 127.0.0.1 only (was 0.0.0.0:8931)
#   gaika-log-receiver desktop log bundles, now 127.0.0.1 only (was 0.0.0.0:8930)
# Code runs from its own detached checkout /opt/gaika (country/ru @ 161ea051), so an
# agent editing /opt/gaika-worktrees/ru never changes running code. Data stays in
# /opt/gaika-worktrees/ru/var (GAIKA_VAR_DIR).
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPO=/opt/gaika-extension; CODE=/opt/gaika; SHA=161ea051; V=/opt/gaika-worktrees/ru/var
UNITS="gaika-refresh gaika-images gaika-dist-server gaika-log-receiver"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ -d "$V/catalog" ] || die "no $V/catalog"
if [ ! -d "$CODE" ]; then
  git -c safe.directory='*' -C "$REPO" worktree add -q --detach "$CODE" "$SHA"
  ln -s /opt/gaika-worktrees/ru/node_modules "$CODE/node_modules"
fi
mkdir -p "$V/log" "$V/diagnostics"

unit() { # name, description, command, extra lines
  cat > "/etc/systemd/system/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CODE
Environment=GAIKA_VAR_DIR=$V
ExecStart=$3
Restart=always
RestartSec=30
$4

[Install]
WantedBy=multi-user.target
EOF
}
unit gaika-refresh "GAIKA catalog refresh scheduler" \
  "/usr/bin/node tools/catalog-crawl/scheduler.mjs --tick 300 --global-concurrency 3" \
  "TimeoutStopSec=1800
Nice=10
IOSchedulingClass=idle
StandardOutput=append:$V/log/refresh-daemon.out
StandardError=append:$V/log/refresh-daemon.out"
unit gaika-images "GAIKA product image fetcher" \
  "/usr/bin/node tools/image-fetch/scheduler.mjs --chunk 300 --rest 600 --backend local" \
  "TimeoutStopSec=300
Nice=15
IOSchedulingClass=idle
StandardOutput=append:$V/log/images-daemon.out
StandardError=append:$V/log/images-daemon.out"
unit gaika-dist-server "GAIKA dist server (localhost only)" \
  "/usr/bin/node tools/dist-server.mjs --port 8931 --host 127.0.0.1 --root $V/dist" \
  "MemoryMax=256M
NoNewPrivileges=yes
StandardOutput=append:$V/dist-server.out
StandardError=append:$V/dist-server.out"
unit gaika-log-receiver "GAIKA diagnostics receiver (localhost only)" \
  "/usr/bin/node tools/log-receiver.mjs --port 8930 --host 127.0.0.1" \
  "MemoryMax=256M
NoNewPrivileges=yes
StandardOutput=append:$V/diagnostics/receiver.out
StandardError=append:$V/diagnostics/receiver.out"

# the hand-started public listeners (since 10-21) go away first
pkill -f 'tools/log-receiver.mjs --port 8930 --host 0.0.0.0' || true
pkill -f 'tools/dist-server.mjs --port 8931 --host 0.0.0.0' || true
sleep 2
systemctl daemon-reload
systemctl enable --now $UNITS
sleep 8
for u in $UNITS; do systemctl is-active -q $u || die "$u did not start: journalctl -u $u"; done
if ss -ltn | grep -qE '0\.0\.0\.0:893[01]'; then die "a port is still public"; fi
echo "DONE: $UNITS active; 8930/8931 on 127.0.0.1 only"
echo "Rollback: systemctl disable --now $UNITS && rm /etc/systemd/system/gaika-{refresh,images,dist-server,log-receiver}.service && systemctl daemon-reload"
