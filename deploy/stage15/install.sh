#!/usr/bin/env bash
# Stage 15 deploy of Owner OS plus the seo MCP. Owner runs this. AGENT_RUN_AS stays off.
#
#   install.sh --check                        read-only: refs, trees, services. No DB.
#   install.sh --tokens                       lists MCP tokens (id, label, enabled) to pick from
#   install.sh --dry-run                      --check, plus migration on copies of the live DBs
#   install.sh --apply --reviewer-token-id N  deploys. N is the live ChatGPT connector's token
#
# Prints no token. Rollback: deploy/stage15/rollback.sh <backup dir>, printed at the end.
set -euo pipefail
umask 077

RUNTIME=/root/ai-dev-runtime
SEO=/opt/seo
LIVE_SHA=955700417adb899e32160e738f942e0a7ba4544f
TARGET_SHA=319012a
SEO_LIVE=7039d6c
SEO_TARGET=5253d7c
CLIENT=chatgpt-tech-lead
TOKENS=/etc/ai-runtime/runtime-clients.json
DROPIN=/etc/systemd/system/ai-runtime.service.d/97-fullcycle-reviewers.conf
UNITS=(ai-runtime owner-os-fullcycle-worker owner-os-wake-companion)
API=http://172.17.0.1:8199
PY=$RUNTIME/venv/bin/python
HERE=$(cd "$(dirname "$0")" && pwd)

die() { echo "STOP: $*" >&2; exit 1; }

mode=""; token_id=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check|--tokens|--dry-run|--apply) mode=$1 ;;
    --reviewer-token-id) token_id=${2:-}; shift ;;
    *) die "unknown argument $1" ;;
  esac
  shift
done
[ -n "$mode" ] || die "choose --check, --tokens, --dry-run or --apply"
[ "$(id -u)" = 0 ] || die "run as root"

check() {
  [ "$(git -C $RUNTIME rev-parse HEAD)" = "$LIVE_SHA" ] || die "live is not at ${LIVE_SHA:0:7}"
  git -C $RUNTIME merge-base --is-ancestor HEAD "$TARGET_SHA" || die "target is not a fast-forward"
  [ -z "$(git -C $RUNTIME status --porcelain --untracked-files=no)" ] || die "live has tracked changes"
  local clash
  clash=$(comm -12 <(git -C $RUNTIME ls-files --others --exclude-standard | sort) \
                   <(git -C $RUNTIME diff --name-only HEAD "$TARGET_SHA" | sort))
  [ -z "$clash" ] || die "untracked files in the way: $clash"
  [ "$(git -C $SEO rev-parse --short=7 HEAD)" = "$SEO_LIVE" ] || die "seo is not at $SEO_LIVE"
  git -C $SEO merge-base --is-ancestor HEAD "$SEO_TARGET" || die "seo target is not a fast-forward"
  clash=$(comm -12 <(git -C $SEO status --porcelain | cut -c4- | sort) \
                   <(git -C $SEO diff --name-only HEAD "$SEO_TARGET" | sort))
  [ -z "$clash" ] || die "seo files in the way: $clash"
  [ -z "$(git -C $SEO status --porcelain -- backend)" ] || die "seo backend has changes an image would pick up"
  for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || die "$u is not active"; done
  [ ! -e "$TOKENS" ] || die "$TOKENS already exists"
  [ ! -e "$DROPIN" ] || die "$DROPIN already exists"
  ! grep -qE '^(FULLCYCLE_REVIEWERS|RUNTIME_CLIENT_TOKENS_FILE|AGENT_RUN_AS)=' $RUNTIME/configs/.env \
    || die "configs/.env sets a variable this deploy owns"
  ! grep -qE '^(RUNTIME_FULLCYCLE_TOKEN|MCP_FULLCYCLE_REVIEWER_TOKEN_IDS)=' $SEO/.env \
    || die "seo .env already has the reviewer settings"
  ! ls "${AGENT_CONTROL_DB:-$RUNTIME/agent_control.db}.run_as" >/dev/null 2>&1 || die "a pin exists on the live DB"
  [ -z "${AGENT_RUN_AS:-}" ] || die "AGENT_RUN_AS is set in this shell"
  echo "check ok: live ${LIVE_SHA:0:7} -> $TARGET_SHA, seo $SEO_LIVE -> $SEO_TARGET"
}

dbcopy() {  # src dst: a consistent copy, the live DB stays open for the services
  "$PY" - "$1" "$2" <<'P'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
P
}

rehearse() {
  local tmp wt
  tmp=$(mktemp -d /tmp/stage15-rehearsal.XXXXXX)
  wt=$tmp/tree
  trap 'git -C '"$RUNTIME"' worktree remove --force '"$wt"' 2>/dev/null || true; rm -rf '"$tmp" EXIT
  git -C $RUNTIME worktree add -q --detach "$wt" "$TARGET_SHA"
  dbcopy $RUNTIME/control_plane.db "$tmp/control_plane.db"
  dbcopy $RUNTIME/agent_control.db "$tmp/agent_control.db"
  ( cd "$wt"
    env -i PATH=/usr/bin:/bin HOME=/root PYTHONDONTWRITEBYTECODE=1 \
      CONTROL_PLANE_DB="$tmp/control_plane.db" AGENT_CONTROL_DB="$tmp/agent_control.db" \
      "$PY" - <<'P'
import os, sqlite3
from core.fullcycle import engine as fe
from core import agent_auth_mode as aam
conn, own = fe.connect(None)
print("pending reviews on the live copy:", len(fe.pending_reviews(conn=conn)))
print("control_plane integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("fc_decision_audit present:", "fc_decision_audit" in tables)
for r in conn.execute("SELECT project_id, COUNT(*) FROM fc_review WHERE status='pending' GROUP BY 1"):
    print("  pending in", r[0], r[1])
a = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
aam.ensure_proof_table(a)
a.commit()
print("agent_control integrity:", a.execute("PRAGMA integrity_check").fetchone()[0])
P
  )
  echo "rehearsal ok, on copies only"
}

tokens() {
  docker exec -i seo-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select id, label, enabled, use_count from mcp_tokens order by id"'
}

verify() {
  "$PY" - <<'P'
import json, urllib.request, urllib.error
env = {}
for line in open("/root/ai-dev-runtime/configs/.env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1)
        env[k] = v.strip("'\"")
def post(path, token):
    r = urllib.request.Request("http://172.17.0.1:8199/api/v1" + path, method="POST", data=b"{}",
                               headers={"Authorization": "Bearer " + token,
                                        "Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r, timeout=15).status
    except urllib.error.HTTPError as e:
        return e.code
code = post("/fullcycle/reviews/0/ack", env["RUNTIME_TOKEN"])
assert code == 403, f"shared token on ack gave {code}"
print("shared token refused on ack: 403")
P
  docker exec seo-backend-1 python - <<'P'
import os, urllib.request, urllib.error
t = os.environ["RUNTIME_FULLCYCLE_TOKEN"]
assert t and os.environ["MCP_FULLCYCLE_REVIEWER_TOKEN_IDS"]
u = os.environ["RUNTIME_URL"].rstrip("/") + "/fullcycle/reviews/0/ack"
if "/api/v1" not in u:
    u = os.environ["RUNTIME_URL"].rstrip("/") + "/api/v1/fullcycle/reviews/0/ack"
r = urllib.request.Request(u, method="POST", data=b"{}", headers={"Authorization": "Bearer " + t,
                                                                  "Content-Type": "application/json"})
try:
    code = urllib.request.urlopen(r, timeout=15).status
except urllib.error.HTTPError as e:
    code = e.code
assert code not in (401, 403), f"client token gave {code}"
print("client token from the MCP passes auth:", code)
P
  for u in "${UNITS[@]}"; do systemctl is-active -q "$u" || die "$u is not active"; done
  echo "tmux sessions: $(tmux ls 2>/dev/null | wc -l) (was $1)"
}

case "$mode" in
  --tokens) tokens; exit 0 ;;
  --check) check; exit 0 ;;
  --dry-run) check; rehearse; exit 0 ;;
esac

[[ "$token_id" =~ ^[0-9]+$ ]] || die "--apply needs --reviewer-token-id N, see --tokens"
check

B=$RUNTIME/backups/stage15-deploy-$(date -u +%Y%m%dT%H%M%SZ)
sessions=$(tmux ls 2>/dev/null | wc -l)
mkdir -p "$B"
trap 'echo; echo "FAILED. Rollback: bash '"$HERE"'/rollback.sh '"$B"'" >&2' ERR
for db in control_plane agent_control runtime_jobs runtime_releases; do
  dbcopy "$RUNTIME/$db.db" "$B/$db.db"
done
cp -p $SEO/.env "$B/seo.env"
docker tag seo-backend:latest seo-backend:rollback-pre-stage15
echo "backup: $B"

# reviewer credentials: inert until the code that reads them runs
"$PY" - "$TOKENS" "$CLIENT" "$SEO/.env" "$token_id" <<'P'
import hashlib, json, os, secrets, sys
path, client, env, token_id = sys.argv[1:5]
raw = secrets.token_urlsafe(32)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump({client: hashlib.sha256(raw.encode()).hexdigest()}, fh)
with open(env, "a") as fh:
    fh.write(f"\n# Stage 15: full-cycle reviews\nRUNTIME_FULLCYCLE_TOKEN={raw}\n"
             f"MCP_FULLCYCLE_REVIEWER_TOKEN_IDS={token_id}\n")
P
cat > "$DROPIN" <<E
[Service]
Environment=FULLCYCLE_REVIEWERS=api:client/$CLIENT
E
chmod 644 "$DROPIN"
systemctl daemon-reload

# reviews answer 403 from here until the MCP is up, and wait as pending: nothing is lost
git -C $RUNTIME merge -q --ff-only "$TARGET_SHA"
systemctl restart "${UNITS[@]}"

git -C $SEO merge -q --ff-only "$SEO_TARGET"
( cd $SEO && docker compose build -q backend && docker compose up -d --no-deps backend )

for _ in $(seq 1 30); do
  code=$(curl -s -m3 -o /dev/null -w '%{http_code}' "$API/health" || true)
  [ "$code" = 200 ] && docker exec seo-backend-1 true 2>/dev/null && break
  sleep 2
done
verify "$sessions"
trap - ERR
echo "DONE: live $(git -C $RUNTIME rev-parse --short HEAD), seo $(git -C $SEO rev-parse --short HEAD)"
echo "Rollback: bash $HERE/rollback.sh $B"
