#!/usr/bin/env bash
# A full ChatGPT chat is replaced on its own: Owner OS opens a new chat, tells it to read
# techlead_brief, moves the routes there, and sends the owner one Telegram line.
# Owner runs this.
#
#   install.sh --check    read-only
#   install.sh --apply    deploys; prints the rollback line
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
LIVE_SHA=13d99da
TARGET_SHA=78dcd19
SEO_LIVE=a850bb3
SEO_TARGET=5dd3319
UNITS=(ai-runtime owner-os-wake-companion owner-os-fullcycle-intake)
API=http://172.17.0.1:8199
HERE=$(cd "$(dirname "$0")" && pwd)

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

check() {
  [ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
  [ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
  git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo target is not a fast-forward"
  seo_clash=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) \
                       <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
  [ -z "$seo_clash" ] || die "seo edits in deployed files: $seo_clash"
  for u in "${UNITS[@]}" owner-os-fullcycle-worker; do systemctl is-active -q $u || die "$u not active"; done
  echo "check ok: live $LIVE_SHA -> $TARGET_SHA, seo $SEO_LIVE -> $SEO_TARGET"
}

check
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"

trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-chat-rotation
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart "${UNITS[@]}" owner-os-fullcycle-worker
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8
for u in "${UNITS[@]}" owner-os-fullcycle-worker; do systemctl is-active -q $u || die "$u did not start"; done
code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/fullcycle/brief")
case "$code" in 401|403) echo "brief route without a client token: $code";;
  *) die "brief route answered $code without a token";; esac
docker exec seo-backend-1 python -c "
from services import mcp_server as m
n={t['name'] for t in m.tool_schemas()}
assert {'techlead_brief','techlead_note_add'} <= n
print('MCP: techlead_brief, techlead_note_add loaded')"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: bash $HERE/rollback.sh"
