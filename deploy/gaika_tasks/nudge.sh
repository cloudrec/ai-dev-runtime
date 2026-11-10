#!/usr/bin/env bash
# Owner runs: bash nudge.sh <tmux session> "<text>"  — types one message into an idle agent.
set -euo pipefail
s=$1; shift; t=${*:-"continue the task from where it stopped"}
tmux has-session -t "$s" || exit 1
tmux send-keys -t "$s" -l "$t"; sleep 1; tmux send-keys -t "$s" Enter; echo "sent to $s"
