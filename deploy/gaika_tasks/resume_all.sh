#!/usr/bin/env bash
# Owner runs this: all three GAIKA agents died on "API Error: Connection lost"; tell each to continue.
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
MSG="Connection was lost mid-response. Continue exactly where you stopped (check git status/diff first), finish the stage, run the tests, then report via stage_done."
for s in gaika-country-ru gaika-country-ro gaika-country-pl; do
  bash "$D/nudge.sh" "$s" "$MSG"
done
