#!/usr/bin/env bash
# ChatGPT projects run their agents as `fcagent`, in a mount namespace that hides the rest
# of /opt (payment code included), on their own tmux server, from their own worker.
# Owner runs this, in three steps:
#
#   install.sh --check      read-only
#   install.sh --prepare    makes user fcagent and gives it its own claude; then the owner
#                           logs that claude in (see the printed line)
#   install.sh --apply      switches ChatGPT projects to the sandboxed worker; prints rollback
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 022

RUNTIME=/root/ai-dev-runtime
LIVE_SHA=db9319e
TARGET_SHA=13d99da
U=fcagent
H=/home/$U
ROOT=/opt/fc-projects
STATE=/var/lib/owneros-fc
UNIT=owner-os-fullcycle-intake
UNIT_FILE=/etc/systemd/system/$UNIT.service
D_API=/etc/systemd/system/ai-runtime.service.d/98-fullcycle-intake.conf
D_W=/etc/systemd/system/owner-os-fullcycle-worker.service.d
BACKUP=/root/backups/isolation-$(date +%Y%m%d)
HERE=$(cd "$(dirname "$0")" && pwd)
PY=$RUNTIME/venv/bin/python

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

user_ok() {   # exists, not root, no extra groups, home as expected
  id "$U" >/dev/null 2>&1 || return 1
  [ "$(id -u $U)" != 0 ] && [ "$(id -g $U)" != 0 ] || die "$U is root"
  [ "$(id -G $U)" = "$(id -g $U)" ] || die "$U has extra groups: $(id -Gn $U)"
  [ "$(getent passwd $U | cut -d: -f6)" = "$H" ] || die "$U home is not $H"
}

check() {
  local head; head=$(git -C $RUNTIME rev-parse --short=7 HEAD)
  # b7c3521: an earlier --apply fast-forwarded before its preflight failed (inert code)
  case "$head" in "$LIVE_SHA"|b7c3521|"$TARGET_SHA") ;; *) die "live is not at $LIVE_SHA";; esac
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
  for b in unshare setpriv mount; do command -v $b >/dev/null || die "$b missing"; done
  [ -x "$(readlink -f /root/.local/bin/claude)" ] || die "root's claude binary not found"
  [ -d "$ROOT" ] || die "$ROOT missing"
  [ -f "$D_API" ] || die "$D_API missing (intake not deployed?)"
  [ ! -e "$UNIT_FILE" ] || die "$UNIT_FILE exists already"
  for u in ai-runtime owner-os-fullcycle-worker; do systemctl is-active -q $u || die "$u not active"; done
  if user_ok; then echo "user $U: present"; else echo "user $U: not yet (--prepare makes it)"; fi
  echo "check ok: live $head -> $TARGET_SHA"
}

prepare() {
  if ! id "$U" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$H" --shell /bin/bash --user-group "$U"
  fi
  user_ok
  chmod 700 "$H"
  install -d -o $U -g $U -m 755 "$H/.local" "$H/.local/bin"
  install -o $U -g $U -m 755 "$(readlink -f /root/.local/bin/claude)" "$H/.local/bin/claude"
  install -d -o root -g root -m 700 "$STATE"
  echo "prepared: user $U ($(id -u $U)), claude $("$H/.local/bin/claude" --version 2>/dev/null | head -1)"
  echo
  echo "NOW log that claude in, in a real terminal on the server (ssh), not through Claude Code:"
  echo "  su - $U -c '$H/.local/bin/claude'"
  echo "pick the theme, run /login, finish in the browser, then /exit. Then: install.sh --apply"
}

logged_in() { [ -s "$H/.claude/.credentials.json" ] || [ -s "$H/.claude.json" ]; }

apply() {
  user_ok || die "run --prepare first"
  logged_in || die "$U's claude is not logged in: su - $U -c '$H/.local/bin/claude', then /login"
  mkdir -p "$BACKUP"
  cp -a "$D_API" "$BACKUP/" ; cp -a "$D_W"/98-fullcycle-intake.conf "$D_W"/99-fullcycle-telegram.conf "$BACKUP/" 2>/dev/null || true
  trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh" >&2' ERR

  # preflight first, from the target code unpacked aside: as the agent, through the
  # sandbox; it pins the setting for this instance. Live is not touched until it passes.
  PF=$(mktemp -d /root/.isolation-preflight.XXXXXX)
  git -C $RUNTIME archive "$TARGET_SHA" core tools config | tar -x -C "$PF"
  AGENT_RUN_AS=$U AGENT_CONTROL_DB=$STATE/agent_control.db AGENT_SANDBOX_KEEP=$ROOT \
    $PY "$PF/tools/agent_uid_preflight.py" --pin \
      --deny /opt/payment-orchestrator --deny /opt/payorch-wt-tabpay \
      --deny /opt/payorch-wt-mulenrefund --deny /opt/seo/.env \
      --deny /root/ai-dev-runtime/configs/.env --deny /etc/ai-runtime/fullcycle-telegram.env \
    || { rm -rf "$PF"; die "preflight failed: nothing switched yet"; }
  rm -rf "$PF"
  git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"

  # the API hands new project trees to fcagent
  printf '[Service]\nEnvironment=FULLCYCLE_INTAKE_ROOT=%s\nEnvironment=FULLCYCLE_INTAKE_RUN_AS=%s\n' \
    "$ROOT" "$U" > "$D_API"
  # the live worker gives ChatGPT projects up; the sandboxed worker takes them
  rm -f "$D_W/98-fullcycle-intake.conf" "$D_W/99-fullcycle-telegram.conf"
  install -m 644 "$HERE/$UNIT.service" "$UNIT_FILE"
  systemctl daemon-reload
  systemctl restart ai-runtime owner-os-fullcycle-worker
  systemctl enable -q --now $UNIT
  sleep 5
  for u in ai-runtime owner-os-fullcycle-worker $UNIT; do
    systemctl is-active -q $u || die "$u did not start"
  done
  [ -z "$(systemctl show owner-os-fullcycle-worker -p Environment --value | tr ' ' '\n' | grep FULLCYCLE_INTAKE_ROOT)" ] \
    || die "live worker still has the intake root"
  echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD); ChatGPT projects now run as $U, sandboxed"
  echo "Rollback: bash $HERE/rollback.sh"
}

check
case "${1:-}" in
  --check) ;;
  --prepare) prepare ;;
  --apply) apply ;;
  *) die "choose --check, --prepare or --apply" ;;
esac
