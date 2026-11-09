#!/usr/bin/env bash
# Undo deploy/isolation/install.sh --apply: ChatGPT projects back on the live worker, as root.
# Leaves user fcagent and its login in place (userdel -r fcagent removes them).
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime
D_W=/etc/systemd/system/owner-os-fullcycle-worker.service.d
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
systemctl disable -q --now owner-os-fullcycle-intake 2>/dev/null
tmux -L owneros-fc kill-server 2>/dev/null
rm -f /etc/systemd/system/owner-os-fullcycle-intake.service
printf '[Service]\nEnvironment=FULLCYCLE_INTAKE_ROOT=/opt/fc-projects\n' \
  > /etc/systemd/system/ai-runtime.service.d/98-fullcycle-intake.conf
mkdir -p "$D_W"
printf '[Service]\nEnvironment=FULLCYCLE_INTAKE_ROOT=/opt/fc-projects\nEnvironment=AGENT_AUTOMATION_TARGETS=owneros-taskboard-taskboard@fullcycle:engine,fc-*@fullcycle:engine\n' \
  > "$D_W/98-fullcycle-intake.conf"
printf '[Service]\nEnvironmentFile=/etc/ai-runtime/fullcycle-telegram.env\n' > "$D_W/99-fullcycle-telegram.conf"
systemctl daemon-reload
git -C $RUNTIME reset --keep db9319e
systemctl restart ai-runtime owner-os-fullcycle-worker
echo "rolled back: live $(git -C $RUNTIME rev-parse --short HEAD); ChatGPT projects on the live worker"
echo "note: projects made while sandboxed belong to fcagent; the root worker refuses none of them"
