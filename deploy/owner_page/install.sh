#!/usr/bin/env bash
# Short release links, the "what needs you" Telegram list and the owner's phone page.
# The page link is sent to the owner's Telegram, never printed. Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUNTIME=/root/ai-dev-runtime; SEO=/opt/seo; PY=$RUNTIME/venv/bin/python
LIVE_SHA=99f2034; TARGET_SHA=11c7a43; SEO_LIVE=59e5b1a; SEO_TARGET=961629d
BASE=https://seoseo.cheap/api/owner/fc-page
D=/etc/systemd/system/owner-os-wake-companion.service.d/30-owner-page.conf
UNITS="ai-runtime owner-os-wake-companion owner-os-fullcycle-intake owner-os-fullcycle-worker"
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
h=$(git -C $RUNTIME rev-parse --short=7 HEAD); [ "$h" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "not a fast-forward"
[ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
[ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo not a fast-forward"
c=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
[ -z "$c" ] || die "seo edits in deployed files: $c"
trap 'echo "FAILED. Rollback: bash $(dirname "$0")/rollback.sh" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-owner-page
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
mkdir -p "$(dirname "$D")"
printf '[Service]\nEnvironment=FULLCYCLE_OWNER_PAGE_BASE=%s\n' "$BASE" > "$D"
systemctl daemon-reload
# the page key, and: projects finished before today are not offered for release one by one
FULLCYCLE_OWNER_PAGE_BASE=$BASE FULLCYCLE_INTAKE_ROOT=/opt/fc-projects \
  $PY $RUNTIME/tools/owner_page.py url > /dev/null
FULLCYCLE_INTAKE_ROOT=/opt/fc-projects $PY $RUNTIME/tools/owner_page.py dismiss-finished
systemctl restart $UNITS
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )
sleep 8
for u in $UNITS; do systemctl is-active -q $u || die "$u did not start"; done
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/$(head -c 43 /dev/zero | tr '\0' K)")
[ "$code" = 404 ] || die "owner page answered $code for a wrong key (want 404)"
echo "owner page: a wrong key is refused (404)"
# the link goes to Telegram only
( set -a; . /etc/ai-runtime/fullcycle-telegram.env; set +a
  FULLCYCLE_OWNER_PAGE_BASE=$BASE $PY - <<'PY'
import sys
sys.path.insert(0, "/root/ai-dev-runtime")
from core.fullcycle import owner_page
from core.control_plane.delivery import _send_owner_push
ok, _r, err = _send_owner_push("📱 Owner OS, твоя страница (сохрани, никому не пересылай): "
                               + owner_page.url())
print("page link sent to Telegram" if ok else f"Telegram send failed: {err}")
PY
)
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: bash $(dirname "$0")/rollback.sh"
