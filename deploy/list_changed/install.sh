#!/usr/bin/env bash
# MCP tools.listChanged: after a deploy, ChatGPT is told the tool catalog changed, so no
# manual connector refresh should be needed. seo backend only. Owner runs this.
#
#   install.sh --check    read-only
#   install.sh --apply    deploys; prints the rollback line
set -euo pipefail

SEO=/opt/seo
SEO_LIVE=ee9e404
SEO_TARGET=a850bb3
HERE=$(cd "$(dirname "$0")" && pwd)

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

check() {
  [ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
  git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo target is not a fast-forward"
  seo_clash=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) \
                       <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
  [ -z "$seo_clash" ] || die "seo edits in deployed files: $seo_clash"
  docker inspect -f '{{.State.Running}}' seo-backend-1 | grep -q true || die "seo-backend-1 not running"
  echo "check ok: seo $SEO_LIVE -> $SEO_TARGET"
}

check
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"

trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-list-changed
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8
docker exec seo-backend-1 python -c "
import asyncio
from types import SimpleNamespace
from services import mcp_server as m
r = asyncio.run(m.dispatch_rpc(None, SimpleNamespace(id=0, scopes=[]), 'initialize', {}))
assert r['capabilities']['tools']['listChanged'] is True
print('MCP initialize: tools.listChanged = true')"
echo "DONE: seo $(git -C $SEO rev-parse --short HEAD)"
echo "Proof: docker logs seo-backend-1 2>&1 | grep '\[MCP\] tools/list'  (a line without your refresh = ChatGPT re-listed by itself)"
echo "Rollback: bash $HERE/rollback.sh"
