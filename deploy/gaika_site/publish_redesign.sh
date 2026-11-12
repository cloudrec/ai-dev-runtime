#!/usr/bin/env bash
# Owner runs this (owner order 2026-11-12): replace gaika.tech / gaika.store with the
# ChatGPT redesign (gaika-redesign-v1.zip). Static HTML/CSS/JS, no external requests
# (checked by the tech lead before this script was written). README.md and test_site.py
# from the zip are not published. The current site is kept as a dated backup; /owner/
# (test builds, inbox) is not touched.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
ZIP_IN=/opt/gaika/gaika-redesign-v1.zip
KEEP=/opt/owneros-autonomy/deploy/gaika_site/gaika-redesign-v1.zip
DST=/var/www/gaika
# the zip sits inside the services' code checkout; keep it next to this script instead
if [ -f "$ZIP_IN" ]; then mv "$ZIP_IN" "$KEEP"; fi
[ -f "$KEEP" ] || die "no $KEEP"
echo "$(sha256sum "$KEEP" | cut -c1-16)  $KEEP"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
unzip -q "$KEEP" -d "$T"
S="$T/gaika-redesign-v1"
[ -f "$S/index.html" ] && [ -f "$S/retailers.html" ] && [ -d "$S/assets" ] || die "unexpected zip layout"
if grep -rqE '(src|href)="https?://' "$S"/*.html; then die "external reference in the pages"; fi
if grep -qE 'fetch\(|XMLHttpRequest|sendBeacon|WebSocket|eval\(' "$S"/assets/*.js; then die "network/eval code in scripts"; fi
B="$DST.bak-$(date +%Y%m%d-%H%M)"
cp -a "$DST" "$B"
mkdir -p "$T/site" && cp -a "$S/index.html" "$S/retailers.html" "$S/assets" "$T/site/"
rsync -a --delete "$T/site/" "$DST/"
chmod -R a+rX "$DST"
for p in / /retailers.html /assets/styles.css /assets/app.js /assets/i18n.js; do
  c=$(curl -s -o /dev/null -w '%{http_code}' --resolve gaika.tech:443:127.0.0.1 -k "https://gaika.tech$p")
  [ "$c" = 200 ] || { rsync -a --delete "$B/" "$DST/"; die "$p answered $c, old site restored"; }
done
echo "DONE: redesign live on gaika.tech (languages via ?lang=ru|en|uk|pl|de|fr)"
echo "Rollback: rsync -a --delete $B/ $DST/"
