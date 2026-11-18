#!/usr/bin/env bash
set -euo pipefail
SEO=/opt/seo; FROM=39603fb; DC="docker compose -f $SEO/docker-compose.yml"
docker image inspect seo-backend:rollback-pre-report-text >/dev/null || { echo "no rollback image" >&2; exit 1; }
git -C "$SEO" reset --keep "$FROM"
docker image tag seo-backend:rollback-pre-report-text seo-backend:latest
$DC up -d --no-deps backend
echo "rolled back: seo $(git -C "$SEO" rev-parse --short HEAD)"
