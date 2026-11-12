#!/usr/bin/env bash
# Owner runs this: puts the GAIKA landing page (7 languages, static, no external scripts)
# live on gaika.tech / gaika.store. The previous site is kept as a dated backup.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
SRC=/opt/gaika-worktrees/ro-cart/deploy/site; DST=/var/www/gaika
[ -f "$SRC/index.html" ] || die "no $SRC/index.html"
if grep -rqE '<script[^>]+src="https?:' "$SRC"; then die "external script found in $SRC"; fi
B="$DST.bak-$(date +%Y%m%d-%H%M)"
cp -a "$DST" "$B"
rsync -a --delete "$SRC/" "$DST/"
chmod -R a+rX "$DST"
for p in / /ru/ /uk/ /pl/ /site.css; do
  c=$(curl -s -o /dev/null -w '%{http_code}' --resolve gaika.tech:443:127.0.0.1 -k "https://gaika.tech$p")
  [ "$c" = 200 ] || { rsync -a --delete "$B/" "$DST/"; die "$p answered $c, old site restored"; }
done
echo "DONE: landing page live on gaika.tech (en at /, ru /ru/, uk /uk/, pl /pl/, de /de/, fr /fr/, ro /ro/)"
echo "Rollback: rsync -a --delete $B/ $DST/"
