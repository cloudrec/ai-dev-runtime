#!/usr/bin/env bash
# Full-cycle projects on the owner's allowed repos, and a Telegram notice when a project
# finishes. Owner runs this. Allows no repo by itself: after it, the owner adds repos with
#   /root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/fullcycle_repo.py allow <name> <path>
#
#   install.sh --check    read-only
#   install.sh --apply    deploys; prints the rollback line
set -euo pipefail
umask 077

RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
LIVE_SHA=b24b66b
TARGET_SHA=db9319e
SEO_LIVE=0aa58b2
SEO_TARGET=ee9e404
UNITS=(ai-runtime owner-os-fullcycle-worker)
API=http://172.17.0.1:8199
HERE=$(cd "$(dirname "$0")" && pwd)
ENVFILE=$RUNTIME/configs/.env
TG=/etc/ai-runtime/fullcycle-telegram.env
D_WORKER=/etc/systemd/system/owner-os-fullcycle-worker.service.d/99-fullcycle-telegram.conf

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

check() {
  [ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
  clash=$(comm -12 <(git -C $RUNTIME ls-files --others --exclude-standard | sort) \
                   <(git -C $RUNTIME diff --name-only HEAD "$TARGET_SHA" | sort))
  [ -z "$clash" ] || die "untracked files in the way: $clash"
  [ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
  git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo target is not a fast-forward"
  seo_clash=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) \
                       <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
  [ -z "$seo_clash" ] || die "seo edits in deployed files: $seo_clash"
  for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    [ "$(grep -c "^$k=." "$ENVFILE" || true)" = 1 ] || die "$k: not exactly one line in configs/.env"
  done
  for f in "$TG" "$D_WORKER"; do [ ! -e "$f" ] || die "$f exists already"; done
  for u in "${UNITS[@]}" owner-os-wake-companion; do
    systemctl is-active -q "$u" || die "$u is not active"
  done
  echo "check ok: live $LIVE_SHA -> $TARGET_SHA, seo $SEO_LIVE -> $SEO_TARGET"
}

check
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"

trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-repos

# the worker gets the two Telegram values only, never the rest of configs/.env
grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' "$ENVFILE" > "$TG"
chmod 600 "$TG"
mkdir -p "$(dirname "$D_WORKER")"
printf '[Service]\nEnvironmentFile=%s\n' "$TG" > "$D_WORKER"
chmod 644 "$D_WORKER"
systemctl daemon-reload

git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
for u in "${UNITS[@]}"; do systemctl restart "$u"; done
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )

sleep 8
for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || die "$u did not start"; done
code=$(curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/fullcycle/repos")
case "$code" in 401|403) echo "repos route without a client token: $code (want 401 or 403)";;
  *) die "repos route answered $code without a token";; esac
docker exec seo-backend-1 python -c "
from services import mcp_server as m
n={t['name'] for t in m.tool_schemas()}
assert 'fullcycle_repos_list' in n
s=[t for t in m.tool_schemas() if t['name']=='fullcycle_project_start'][0]
assert 'repo' in s['inputSchema']['properties']
print('MCP: fullcycle_repos_list loaded, project_start takes repo')"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Next: refresh the ChatGPT connector; allow a repo with tools/fullcycle_repo.py allow <name> <path>"
echo "Rollback: bash $HERE/rollback.sh"
