#!/usr/bin/env bash
# Seed paths + release-from-the-phone links. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; SEO=/opt/seo
LIVE_SHA=f9cde5c; TARGET_SHA=743077b; SEO_LIVE=d1aa013; SEO_TARGET=59e5b1a
KEY=/etc/ai-runtime/release-link.key
D=/etc/systemd/system/owner-os-fullcycle-intake.service.d/20-release-link.conf
BASE=https://seoseo.cheap/api/owner/fc-release
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || [ "$h" = 1bc8fa3 ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
[ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo not a fast-forward"
c=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
[ -z "$c" ] || die "seo edits in deployed files: $c"
trap 'echo "FAILED. Rollback: bash $(dirname "$0")/rollback.sh" >&2' ERR
# the signing key: root-only, made once, never printed
if [ ! -e "$KEY" ]; then (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$KEY"); fi
chmod 600 "$KEY"
mkdir -p "$(dirname "$D")"
printf '[Service]\nEnvironment=FULLCYCLE_RELEASE_URL_BASE=%s\n' "$BASE" > "$D"
systemctl daemon-reload
docker tag seo-backend:latest seo-backend:rollback-pre-release-link
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8
for u in ai-runtime owner-os-fullcycle-intake owner-os-fullcycle-worker; do systemctl is-active -q $u || die "$u did not start"; done
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
[ "$code" = 400 ] || die "release page answered $code for a bad link (want 400)"
echo "release page: a bad link is refused (400)"
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: bash $(dirname "$0")/rollback.sh"
