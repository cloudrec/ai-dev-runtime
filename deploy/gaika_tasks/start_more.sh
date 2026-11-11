#!/usr/bin/env bash
# Owner runs this: starts two more GAIKA cart agents (HU+Baltics, West) from the trunk.
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
REPO=/opt/gaika-extension; BASE=integration/trunk-20261110
start() { # session, worktree, branch, brief file
  local s=$1 w=/opt/gaika-worktrees/$2 b=$3 f=$4
  tmux has-session -t "$s" 2>/dev/null && { echo "$s already runs"; return; }
  [ -d "$w" ] || git -c safe.directory='*' -C "$REPO" worktree add -q -b "$b" "$w" "$BASE"
  [ -e "$w/node_modules" ] || ln -s /opt/gaika-worktrees/ru/node_modules "$w/node_modules"
  tmux new-session -d -s "$s" -c "$w" claude
  sleep 15
  bash "$D/nudge.sh" "$s" "$(tr '\n' ' ' < "$D/common_cart.txt") $(tr '\n' ' ' < "$D/$f")"
}
start gaika-country-hu hu-balt-cart feat/hu-balt-cart-20261111 hu.txt
start gaika-country-west west-cart feat/west-cart-20261111 west.txt
