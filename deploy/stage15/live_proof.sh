#!/bin/bash
# Owner-run. Starts the existing test agent and gives the full-cycle engine one small task,
# so a real review goes through the live chat after the stage 15 deploy.
set -euo pipefail
LIVE=/root/ai-dev-runtime
ROOT=/opt/owneros-taskboard-test
SESSION=owneros-taskboard-taskboard
PROJECT=owneros-taskboard-test
SPEC=/opt/owneros-autonomy/deploy/stage15/live_proof_spec.json
PY=$LIVE/venv/bin/python

[ "$(git -C "$LIVE" rev-parse --short HEAD)" = 319012a ] || { echo "live is not 319012a"; exit 1; }
if [ -n "$(git -C "$ROOT" status --porcelain -- . ':!.fullcycle')" ]; then
  echo "project tree is dirty"; exit 1
fi
export -n TMUX TMUX_PANE 2>/dev/null || true
if tmux has-session -t "=$SESSION" 2>/dev/null; then
  echo "agent already running: $SESSION"
else
  mapfile -t ARGV < <(cd "$LIVE" && "$PY" -c 'import sys; sys.path.insert(0, "."); from core import agent_auth_mode as a; print("\n".join(a.subscription_argv(["--model", "sonnet"])))')
  env -u TMUX -u TMUX_PANE -u CLAUDECODE tmux new-session -d -s "$SESSION" -c "$ROOT" "${ARGV[@]}"
  echo "agent started: $SESSION"
  python3 -c 'import time; time.sleep(10)'
fi
tmux capture-pane -p -t "=$SESSION:" | grep -v '^$' | tail -6

cd "$LIVE"
CONTROL_PLANE_DB=$LIVE/control_plane.db AGENT_CONTROL_DB=$LIVE/agent_control.db \
  "$PY" tools/fullcycle_register.py --project "$PROJECT" --root "$ROOT" --executor claude \
  --spec "$SPEC" --by owner | "$PY" -c 'import json,sys; r=json.load(sys.stdin); print({k:r.get(k) for k in ("ok","error","spec_version")})'
echo "task registered, worker picks it up within 20 seconds"
