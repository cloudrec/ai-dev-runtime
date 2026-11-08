#!/usr/bin/env bash
# Projects from ChatGPT: the tech lead starts a project with fullcycle_project_start, Owner OS
# makes /opt/fc-projects/<id>, starts agent fc-<id>, runs the tasks, sends each for review.
# Owner runs this.
#
#   install.sh --check    read-only: refs, trees, units
#   install.sh --apply    deploys; prints the rollback line
set -euo pipefail
umask 022

RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
LIVE_SHA=308ac8f
TARGET_SHA=68895a0
SEO_LIVE=5253d7c
SEO_TARGET=0aa58b2
ROOT=/opt/fc-projects
UNITS=(ai-runtime owner-os-fullcycle-worker owner-os-wake-companion)
API=http://172.17.0.1:8199
HERE=$(cd "$(dirname "$0")" && pwd)
D_API=/etc/systemd/system/ai-runtime.service.d/98-fullcycle-intake.conf
D_WORKER=/etc/systemd/system/owner-os-fullcycle-worker.service.d/98-fullcycle-intake.conf
D_WAKE=/etc/systemd/system/owner-os-wake-companion.service.d/98-fullcycle-intake.conf

die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

env_of() {  # the unit's current value of one variable
  systemctl show "$1" -p Environment --value | tr ' ' '\n' | sed -n "s/^$2=//p" | tail -1
}

check() {
  [ "$(git -C $RUNTIME rev-parse --short=7 HEAD)" = "$LIVE_SHA" ] || die "live is not at $LIVE_SHA"
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  clash=$(comm -12 <(git -C $RUNTIME ls-files --others --exclude-standard | sort) \
                   <(git -C $RUNTIME diff --name-only HEAD "$TARGET_SHA" | sort))
  [ -z "$clash" ] || die "untracked files in the way: $clash"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live tree has edits"
  [ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
  git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo target is not a fast-forward"
  # old unrelated edits in /opt/seo (nginx.conf, settings, reports) stay as they are;
  # only an edit to a file this deploy changes stops it
  seo_clash=$(comm -12 <(git -C $SEO diff --name-only HEAD | sort) \
                       <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
  [ -z "$seo_clash" ] || die "seo edits in deployed files: $seo_clash"
  for f in "$D_API" "$D_WORKER" "$D_WAKE"; do [ ! -e "$f" ] || die "$f exists already"; done
  [ ! -e "$ROOT" ] || [ -z "$(ls -A $ROOT)" ] || die "$ROOT exists and is not empty"
  for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || die "$u is not active"; done
  echo "check ok: live $LIVE_SHA -> $TARGET_SHA, seo $SEO_LIVE -> $SEO_TARGET"
}

check
[ "${1:-}" = "--check" ] && exit 0
[ "${1:-}" = "--apply" ] || die "choose --check or --apply"

targets=$(env_of owner-os-fullcycle-worker AGENT_AUTOMATION_TARGETS)
only=$(env_of owner-os-wake-companion WAKE_BRIDGE_ONLY_AGENTS)
trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh" >&2' ERR
docker tag seo-backend:latest seo-backend:rollback-pre-intake

mkdir -p -m 755 $ROOT
mkdir -p "$(dirname "$D_WORKER")"
printf '[Service]\nEnvironment=FULLCYCLE_INTAKE_ROOT=%s\n' "$ROOT" > "$D_API"
printf '[Service]\nEnvironment=FULLCYCLE_INTAKE_ROOT=%s\nEnvironment=AGENT_AUTOMATION_TARGETS=%s\n' \
  "$ROOT" "${targets:+$targets,}fc-*@fullcycle:engine" > "$D_WORKER"
if [ -n "$only" ]; then   # unset means every agent is offered already
  printf '[Service]\nEnvironment=WAKE_BRIDGE_ONLY_AGENTS=%s\n' "$only,fc-*" > "$D_WAKE"
fi
systemctl daemon-reload

git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart "${UNITS[@]}"
git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )

for _ in $(seq 1 30); do
  code=$(curl -s -m3 -o /dev/null -w '%{http_code}' "$API/health" || true)
  [ "$code" = 200 ] && break
  sleep 2
done
for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || die "$u did not come back"; done
code=$(curl -s -m5 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
       -d '{"project_id":"probe","spec":{}}' "$API/api/v1/fullcycle/projects" || true)
echo "start route without a client token: $code (want 401 or 403)"
case "$code" in 401|403) ;; *) die "start route answered $code" ;; esac
for _ in $(seq 1 30); do
  docker exec seo-backend-1 python -c 'from services import mcp_server as m; assert "fullcycle_project_start" in m.TOOLS' 2>/dev/null && break
  sleep 2
done
docker exec seo-backend-1 python -c 'from services import mcp_server as m; assert "fullcycle_project_start" in m.TOOLS' \
  || die "MCP tool not loaded"
echo "MCP tool fullcycle_project_start: loaded"
trap - ERR
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: bash $HERE/rollback.sh"
