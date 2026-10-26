#!/usr/bin/env bash
# Owner-run (root) install/removal of the native Owner OS GPT CTO provider for the canary.
#
#   tools/native_cto_key.sh preconfig  # provider config only, no key -> verify missing_secret
#   tools/native_cto_key.sh install    # config + key (prompted, hidden) -> verify ready
#   tools/native_cto_key.sh remove     # remove key AND config -> verify provider disabled
#
# The key is read from the terminal with echo off (or from stdin when piped), validated,
# and written atomically to /etc/ai-runtime/cto-openai.env (root 0600). It is never passed
# as an argument, echoed or logged. Each mode restarts ai-runtime.service only, then runs
# tools/native_cto_verify.py; a failed `install` verification rolls the key back (fail
# closed) and leaves the provider config in place, i.e. the service returns to
# missing_secret / waiting_provider.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# NATIVE_CTO_* overrides exist only so tests can run this script in a sandbox.
RUNTIME="${NATIVE_CTO_RUNTIME:-/root/ai-dev-runtime}"
PY="${NATIVE_CTO_PY:-$RUNTIME/venv/bin/python}"
DROPIN_DIR="${NATIVE_CTO_DROPIN_DIR:-/etc/systemd/system/ai-runtime.service.d}"
DROPIN="$DROPIN_DIR/97-native-cto-provider.conf"
KEY_DIR="${NATIVE_CTO_KEY_DIR:-/etc/ai-runtime}"
KEY_FILE="$KEY_DIR/cto-openai.env"
SERVICE=ai-runtime.service
HEALTH="${NATIVE_CTO_HEALTH:-http://172.17.0.1:8199/health}"

die() { echo "native_cto_key: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ -f "$DROPIN_DIR/96-native-owner-os-canary.conf" ] || die "96 canary drop-in missing; refusing"

hwm() { sqlite3 "file:$RUNTIME/agent_control.db?mode=ro" "SELECT COALESCE(MAX(rowid),0) FROM deliveries"; }

restart() {
  systemctl daemon-reload
  systemctl restart "$SERVICE"
  for _ in $(seq 1 30); do
    curl -s -m 3 -o /dev/null -w '%{http_code}' "$HEALTH" | grep -q 200 && return 0
    sleep 1
  done
  die "$SERVICE not healthy after restart"
}

verify() { "$PY" "$HERE/tools/native_cto_verify.py" "$@"; }

install_config() {
  install -m 0644 "$HERE/deploy/97-native-cto-provider.conf" "$DROPIN"
}

case "${1:-}" in
  preconfig)
    install_config
    since=$(hwm); restart
    verify --expect missing_secret --since-rowid "$since"
    ;;
  install)
    if [ -t 0 ]; then read -rsp "OPENAI_API_KEY: " key; echo; else read -r key; fi
    [[ "$key" =~ ^[A-Za-z0-9_-]{20,}$ ]] || { unset key; die "key format rejected (not written)"; }
    install_config
    install -d -m 0700 "$KEY_DIR"
    tmp=$(mktemp "$KEY_DIR/.cto-openai.XXXXXX")
    printf 'OPENAI_API_KEY=%s\n' "$key" > "$tmp"   # printf is a builtin: not in any argv
    unset key
    chmod 0600 "$tmp"; mv -f "$tmp" "$KEY_FILE"
    since=$(hwm); restart
    if ! verify --expect ready --key-check --since-rowid "$since"; then
      echo "native_cto_key: verification FAILED -> removing key, back to missing_secret" >&2
      rm -f "$KEY_FILE"; restart
      verify --expect missing_secret --since-rowid "$since" || true
      exit 1
    fi
    ;;
  remove)
    rm -f "$KEY_FILE" "$DROPIN"
    since=$(hwm); restart
    verify --expect disabled --since-rowid "$since"
    ;;
  *) die "usage: $0 preconfig|install|remove" ;;
esac
