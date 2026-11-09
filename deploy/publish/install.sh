#!/usr/bin/env bash
# Push / PR buttons for released projects on the owner page. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; SEO=/opt/seo
LIVE_SHA=11c7a43; TARGET_SHA=85e080a; SEO_LIVE=961629d; SEO_TARGET=6114af8
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || [ "$h" = f5080fa ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
[ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo not a fast-forward"
c=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
[ -z "$c" ] || die "seo edits in deployed files: $c"
trap 'echo "FAILED. Rollback: git -C $RUNTIME reset --keep $h; git -C $SEO reset --keep $SEO_LIVE; docker tag seo-backend:rollback-pre-publish seo-backend:latest; (cd $SEO && docker compose up -d --no-deps backend); systemctl restart ai-runtime" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-publish
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart ai-runtime
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8; systemctl is-active -q ai-runtime || die "ai-runtime did not start"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: git -C $RUNTIME reset --keep $h && git -C $SEO reset --keep $SEO_LIVE && docker tag seo-backend:rollback-pre-publish seo-backend:latest && (cd $SEO && docker compose up -d --no-deps backend) && systemctl restart ai-runtime"
