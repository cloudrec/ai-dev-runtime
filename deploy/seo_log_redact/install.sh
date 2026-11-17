#!/usr/bin/env bash
# Access-log redaction of URL secrets: fast-forward the deployed seo branch 97cb001 -> 39603fb,
# rebuild the backend image, recreate only the backend container (--no-deps).
#   install.sh --check | --apply ; rollback.sh restores image + commit.
# Touches backend/services/mcp_server.py, runtime_client.py, one test. No secret, migration or push.
set -euo pipefail
SEO=/opt/seo; FROM=97cb001; TARGET=39603fb
DC="docker compose -f $SEO/docker-compose.yml"
RB_TAG="seo-backend:rollback-pre-log-redact"
mode=${1:-}; [ "$mode" = --check ] || [ "$mode" = --apply ] || { echo "usage: $0 --check|--apply" >&2; exit 2; }
die() { echo "ABORT: $*" >&2; exit 1; }
head=$(git -C "$SEO" rev-parse --short HEAD)
[ "$head" = "$FROM" ] || [ "$head" = "$TARGET" ] || die "seo HEAD $head is neither $FROM nor $TARGET"
git -C "$SEO" merge-base --is-ancestor "$FROM" "$TARGET" || die "not a fast-forward"
for f in $(git -C "$SEO" diff --name-only "$FROM" "$TARGET"); do
  [ -z "$(git -C "$SEO" status --porcelain -- "$f")" ] || die "$f has local changes"; done
[ "$(docker inspect -f '{{.State.Health.Status}}' seo-backend-1)" = healthy ] || die "seo-backend-1 not healthy"
echo "ok: seo $head -> $TARGET; files: $(git -C "$SEO" diff --name-only "$FROM" "$TARGET" | tr '\n' ' ')"
[ "$mode" = --check ] && { echo "CHECK OK (no change made)"; exit 0; }
docker image tag seo-backend:latest "$RB_TAG"
git -C "$SEO" merge --ff-only "$TARGET"
$DC build backend
$DC up -d --no-deps backend
ok=0; for _ in $(seq 1 60); do sleep 3; [ "$(docker inspect -f '{{.State.Health.Status}}' seo-backend-1)" = healthy ] && { ok=1; break; }; done
[ "$ok" = 1 ] || die "backend not healthy - run $(dirname "$0")/rollback.sh"
docker exec seo-backend-1 python -c "import logging, main; from services.log_redaction import SecretPathFilter as F; assert any(isinstance(f, F) for f in logging.getLogger('uvicorn.access').filters); print('log redaction installed')"
echo "DONE: seo $(git -C "$SEO" rev-parse --short HEAD); rollback: $(dirname "$0")/rollback.sh"
