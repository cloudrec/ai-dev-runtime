#!/usr/bin/env bash
# Integration projects (merge_from) + a loop-watch pass line. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; SEO=/opt/seo
LIVE_SHA=81cbd2f; TARGET_SHA=caef919; SEO_LIVE=5dd3319; SEO_TARGET=d1aa013
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
[ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo not a fast-forward"
c=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
[ -z "$c" ] || die "seo edits in deployed files: $c"
trap 'echo "FAILED. Rollback: git -C $RUNTIME reset --keep $LIVE_SHA; git -C $SEO reset --keep $SEO_LIVE; docker tag seo-backend:rollback-pre-integration seo-backend:latest; (cd $SEO && docker compose up -d --no-deps backend); systemctl restart ai-runtime owner-os-wake-companion" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-integration
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart ai-runtime owner-os-wake-companion
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8
for u in ai-runtime owner-os-wake-companion; do systemctl is-active -q $u || die "$u did not start"; done
docker exec seo-backend-1 python -c "
from services import mcp_server as m
s=[t for t in m.tool_schemas() if t['name']=='fullcycle_project_start'][0]
assert 'merge_from' in s['inputSchema']['properties']; print('MCP: project_start takes merge_from')"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: git -C $RUNTIME reset --keep $LIVE_SHA && git -C $SEO reset --keep $SEO_LIVE && docker tag seo-backend:rollback-pre-integration seo-backend:latest && (cd $SEO && docker compose up -d --no-deps backend) && systemctl restart ai-runtime owner-os-wake-companion"
