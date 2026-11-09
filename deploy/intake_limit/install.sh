#!/usr/bin/env bash
# Up to 8 ChatGPT projects at once (was 2). Run after deploy/chat_rotation. Owner runs this.
#   install.sh --check | --apply
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime
LIVE_SHA=78dcd19
TARGET_SHA=50e90f9
D_API=/etc/systemd/system/ai-runtime.service.d/98-fullcycle-intake.conf
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA (run deploy/chat_rotation first)"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
grep -q '^Environment=FULLCYCLE_INTAKE_RUN_AS=fcagent' "$D_API" || die "$D_API is not the isolation one"
echo "check ok: live $LIVE_SHA -> $TARGET_SHA, intake max 2 -> 8"
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"
cp -a "$D_API" "$D_API.bak-limit"
grep -v '^Environment=FULLCYCLE_INTAKE_MAX_ACTIVE=' "$D_API.bak-limit" > "$D_API"
echo 'Environment=FULLCYCLE_INTAKE_MAX_ACTIVE=8' >> "$D_API"
systemctl daemon-reload
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart ai-runtime owner-os-fullcycle-intake
sleep 5
for u in ai-runtime owner-os-fullcycle-intake; do systemctl is-active -q $u || die "$u did not start"; done
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD); up to 8 ChatGPT projects at once"
echo "Rollback: mv $D_API.bak-limit $D_API && git -C $RUNTIME reset --keep $LIVE_SHA && systemctl daemon-reload && systemctl restart ai-runtime owner-os-fullcycle-intake"
