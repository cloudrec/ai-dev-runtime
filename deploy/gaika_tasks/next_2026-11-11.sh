#!/usr/bin/env bash
# Owner runs this: next stage for each GAIKA agent (tech lead, 2026-11-11).
set -euo pipefail
D=$(dirname "$(readlink -f "$0")")
RU="TECH LEAD: good trunk report. Do NOT start anonymous cart tests: owner decision 2026-11-10, no live cart requests from this server (robots disallows cart paths for bots); carts are proven by the owner in his own Chrome. Next stage on integration/trunk-20261110: (1) merge feat/ro-cart-20261110 (/opt/gaika-worktrees/ro-cart) and feat/de-fr-cart-20261110 (/opt/gaika-worktrees/de-fr-cart) into the trunk, full suite green; (2) image-fetch/image-store: reject a response that is not an image (8 HTML/XML pages were stored as .jpg under var/images/kifli etc.) and write 512px q78 + 160px q75 WebP variants next to each new original under var/images-web/{512,160}/<retailer>/<shard>/<hash>.webp (same layout already filled on the server), with tests; (3) a script that builds an OWNER TEST build per country: a ready unpacked extension folder + zip with only that country's tested shops' cart gates ON, so the owner just loads it, never edits code; update the OWNER-TEST sheets to point to it. Then stage_done."
RO="TECH LEAD: RO report accepted. Go on: Czechia, Rohlik first (reuse Knuspr cart read), then Globus, same offline rules + OWNER-TEST sheets. Then stage_done."
PL="TECH LEAD: DE/FR/NL report accepted. Go on: fix the DE/FR matcher misses (eggs, butter, milk) with fixture tests, then stage_done."
bash "$D/nudge.sh" gaika-country-ru "$RU"
bash "$D/nudge.sh" gaika-country-ro "$RO"
bash "$D/nudge.sh" gaika-country-pl "$PL"
