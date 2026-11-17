#!/usr/bin/env bash
# Send-queue E2E over real HTTP: seo MCP tools -> disposable runtime (synthetic panes).
#   tools/e2e/send_queue_e2e.sh <scratch dir> <seo backend dir>
# Touches no live service, tmux or DB. Exit 0 only if every expectation holds.
set -euo pipefail
D=${1:?scratch dir}; SEO=${2:?seo backend dir}
HERE=$(cd "$(dirname "$0")" && pwd); RT_PY=/root/ai-dev-runtime/venv/bin/python
SEO_PY=/opt/seo/backend/.venv/bin/python
mkdir -p "$D/toy"; rm -f "$D"/*.db "$D/typed.jsonl" "$D/busy"
export E2E_DIR=$D E2E_PORT=$((20000 + RANDOM % 20000))
export RUNTIME_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
export RUNTIME_URL=http://127.0.0.1:$E2E_PORT/api/v1
fail() { echo "FAIL: $*"; kill $PID 2>/dev/null || true; exit 1; }
start() { env SEND_QUEUE_ENABLED=${1:-} "$RT_PY" "$HERE/send_queue_runtime.py" > "$D/rt.log" 2>&1 &
          PID=$!; for _ in $(seq 50); do curl -s -o /dev/null "$RUNTIME_URL/agents" && return; sleep 0.2; done
          fail "runtime did not start"; }
step() { (cd "$SEO" && "$SEO_PY" "$HERE/send_queue_mcp_client.py" "$1"); }
typed() { local n=0; [ -f "$D/typed.jsonl" ] && n=$(grep -c -- "$1" "$D/typed.jsonl" || true); echo "${n:-0}"; }

echo "== 1 default (queue disabled)"; start ""; touch "$D/busy"
r=$(step queue_send); echo "$r"; echo "$r" | grep -q '"send_queue_disabled"' || fail "disabled default"
r=$(step old_send); echo "$r"; echo "$r" | grep -q '"refused": "working"' || fail "old client"
kill $PID; wait $PID 2>/dev/null || true

echo "== 2 enabled"; start 1
r=$(step queue_send); echo "$r"; echo "$r" | grep -q '"queued": true' || fail "queued"
echo "$r" | grep -q '"delivered": false' || fail "queued must not claim delivered"
r=$(step queue_dup_other_text); echo "$r"; echo "$r" | grep -q 'idempotency_key_reused_with_other_text' || fail "dup mismatch"
r=$(step pay); echo "$r"; echo "$r" | grep -q 'hard_denied_target' || fail "payment"
r=$(step queue_cancel_me); r=$(step cancel); echo "$r"; echo "$r" | grep -q '"cancelled": true' || fail "cancel"
sleep 1.5; [ "$(typed 'queued task')" = 0 ] || fail "typed while busy"

echo "== 3 restart with the message still queued"; kill $PID; wait $PID 2>/dev/null || true
start 1; rm -f "$D/busy"; t0=$SECONDS
for _ in $(seq 120); do r=$(step status); echo "$r" | grep -q '"status": "delivered"' && break; sleep 0.5; done
echo "$r"; echo "delivered after $((SECONDS - t0)) s"
echo "$r" | grep -q '"status": "delivered"' || fail "delivered after restart"
[ "$(typed 'queued task')" = 1 ] || fail "typed exactly once, got $(typed 'queued task')"
[ "$(typed 'never')" = 0 ] || fail "cancelled message typed"
r=$(step status_c); echo "$r" | grep -q '"status": "cancelled"' || fail "cancel status"
sleep 1.5; [ "$(typed 'queued task')" = 1 ] || fail "re-typed later"
kill $PID; wait $PID 2>/dev/null || true
echo "E2E PASS"
