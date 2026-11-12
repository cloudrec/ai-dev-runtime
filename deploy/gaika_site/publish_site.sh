#!/usr/bin/env bash
# Owner runs this: publish gaika.tech / gaika.store from deploy/gaika_site/www
# (ChatGPT redesign + live market list + tester section + all target-market languages).
# Static files only; refuses external references or network code. Keeps a dated backup;
# /owner/ (test builds, inbox) is not touched.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
SRC=$(dirname "$(readlink -f "$0")")/www; DST=/var/www/gaika
[ -f "$SRC/index.html" ] && [ -f "$SRC/assets/i18n.js" ] && [ -f "$SRC/assets/markets.js" ] || die "incomplete $SRC"
if grep -rqE '(src|href)="https?://' "$SRC"/*.html; then die "external reference in the pages"; fi
if grep -qE 'fetch\(|XMLHttpRequest|sendBeacon|WebSocket|eval\(' "$SRC"/assets/*.js; then die "network/eval code in scripts"; fi
B="$DST.bak-$(date +%Y%m%d-%H%M)"
cp -a "$DST" "$B"
rsync -a --delete "$SRC/" "$DST/"
chmod -R a+rX "$DST"
for p in / /retailers.html /assets/styles.css /assets/app.js /assets/i18n.js /assets/markets.js; do
  c=$(curl -s -o /dev/null -w '%{http_code}' --resolve gaika.tech:443:127.0.0.1 -k "https://gaika.tech$p")
  [ "$c" = 200 ] || { rsync -a --delete "$B/" "$DST/"; die "$p answered $c, old site restored"; }
done
echo "DONE: gaika.tech updated from $SRC"
echo "Rollback: rsync -a --delete $B/ $DST/"
