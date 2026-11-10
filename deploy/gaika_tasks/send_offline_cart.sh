#!/usr/bin/env bash
# Owner runs this: tells the RO and PL GAIKA agents to build cart code offline (no live cart requests).
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
for s in gaika-country-ro gaika-country-pl; do
  bash "$D/nudge.sh" "$s" "$(tr '\n' ' ' < "$D/offline_cart.txt")"
done
