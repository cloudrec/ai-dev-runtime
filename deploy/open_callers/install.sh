#!/usr/bin/env bash
# Owner 2026-11-17 "убери запреты для чат гпт": the GPT tech lead (API/MCP caller class
# `api`) may send to / answer every agent, not only the allowlist. Payment work stays
# closed for every caller by name and directory (payorch, blagopay, payment, cloudpayments,
# a standalone "pay" word). Also ships the reminder fix (reminders of a pending review
# were retired as "already decided").
#   install.sh --check   read-only: what would change, how each live session is treated
#   install.sh --apply   backup, fast-forward live, set the flag, restart 4 units
# Rollback: deploy/open_callers/rollback.sh <backup dir printed by --apply>
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; LIVE_SHA=be3cbd3; TARGET_SHA=${TARGET_SHA:?set TARGET_SHA}
ENVF=$RUNTIME/configs/.env; FLAG=AGENT_AUTOMATION_OPEN_CALLERS
UNITS="ai-runtime owner-os-wake-companion owner-os-fullcycle-worker owner-os-fullcycle-intake"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
mode=${1:-}
h=$(git -C $RUNTIME rev-parse --short=7 HEAD)
[ "$h" = "$LIVE_SHA" ] || [ "$h" = "${TARGET_SHA:0:7}" ] || die "live is at $h, expected $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"

check() {
  echo "live $h -> target $(git -C $RUNTIME rev-parse --short "$TARGET_SHA")"
  git -C $RUNTIME diff --stat HEAD "$TARGET_SHA" -- core tools api | tail -1
  echo "$FLAG now: $(grep -E "^$FLAG=" $ENVF || echo unset)"
  echo "sessions as the tech lead would see them after apply:"
  cd "$(mktemp -d)"
  git -C $RUNTIME show "$TARGET_SHA:core/agent_creation_policy.py" > acp.py
  for s in $(env -u TMUX tmux list-sessions -F '#{session_name}' 2>/dev/null); do
    cwd=$(env -u TMUX tmux display -p -t "=$s:" '#{pane_current_path}' 2>/dev/null)
    v=$("$RUNTIME/venv/bin/python" -I -c "import sys; sys.path.insert(0,'.'); import acp
print('DENIED (payment)' if acp.hard_denied(sys.argv[2], sys.argv[1], {'hard_deny_dirs':['/opt/payment-orchestrator']}) else 'open')" "$s" "$cwd")
    printf '  %-28s %-40s %s\n' "$s" "$cwd" "$v"
  done
}

case "$mode" in
  --check) check ;;
  --apply)
    check
    B=$RUNTIME/backups/open-callers-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$B"
    cp -p $ENVF "$B/env"; echo "$h" > "$B/live_sha"
    git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
    grep -qE "^$FLAG=" $ENVF && sed -i "s/^$FLAG=.*/$FLAG=api/" $ENVF || echo "$FLAG=api" >> $ENVF
    systemctl restart $UNITS
    sleep 8
    for u in $UNITS; do systemctl is-active -q $u || die "$u did not start (rollback: $(dirname "$0")/rollback.sh $B)"; done
    echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), $FLAG=api"
    echo "Rollback: $(dirname "$0")/rollback.sh $B" ;;
  *) die "usage: TARGET_SHA=<sha> $0 --check|--apply" ;;
esac
