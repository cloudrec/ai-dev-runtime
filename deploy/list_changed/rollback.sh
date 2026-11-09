#!/usr/bin/env bash
set -uo pipefail
SEO=/opt/seo
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
git -C $SEO reset --keep ee9e404
docker tag seo-backend:rollback-pre-list-changed seo-backend:latest
( cd $SEO && docker compose up -d --no-deps backend )
echo "rolled back: seo $(git -C $SEO rev-parse --short HEAD)"
