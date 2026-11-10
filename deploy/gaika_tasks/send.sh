#!/usr/bin/env bash
# Owner runs this: hands the 2026-11-10 tasks to the three idle GAIKA agents.
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
for pair in gaika-country-ru:ru gaika-country-ro:ro gaika-country-pl:pl; do
  s=${pair%%:*}; f=${pair##*:}
  tmux has-session -t "$s" 2>/dev/null || { echo "skip $s (no session)"; continue; }
  tmux send-keys -t "$s" -l "$(cat "$D/$f.txt")"
  sleep 1
  tmux send-keys -t "$s" Enter
  echo "sent to $s"
done
