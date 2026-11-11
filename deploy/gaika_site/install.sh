#!/usr/bin/env bash
# Owner runs this: gaika.tech + gaika.store serve the GAIKA site instead of falling through to 3dthings.
# Site file: /opt/clients-help-landing/GAIKA_city_superapp_site_RU_UK_EN_EMBEDDED.html (2026-09-28), copied as index.html.
# Cert: self-signed origin cert, same pattern as 3dthings.store (Cloudflare proxies in front).
set -euo pipefail
SRC=/opt/clients-help-landing/GAIKA_city_superapp_site_RU_UK_EN_EMBEDDED.html
WEB=/var/www/gaika; CONF=/etc/nginx/sites-available/gaika.tech
CRT=/etc/ssl/certs/gaika.crt; KEY=/etc/ssl/private/gaika.key
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
[ -f "$SRC" ] || die "no $SRC"
[ -e "$CONF" ] && die "$CONF exists already, not overwriting"
mkdir -p "$WEB"; cp "$SRC" "$WEB/index.html"; chown -R root:www-data "$WEB"; chmod 755 "$WEB"; chmod 644 "$WEB/index.html"
if [ ! -f "$CRT" ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj "/O=GAIKA/CN=gaika.tech" \
    -addext "subjectAltName=DNS:gaika.tech,DNS:www.gaika.tech,DNS:gaika.store,DNS:www.gaika.store" \
    -keyout "$KEY" -out "$CRT" 2>/dev/null
  chmod 600 "$KEY"
fi
cat > "$CONF" <<NGX
server {
    listen 80;
    listen [::]:80;
    server_name gaika.tech www.gaika.tech gaika.store www.gaika.store;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name gaika.tech www.gaika.tech gaika.store www.gaika.store;
    ssl_certificate     $CRT;
    ssl_certificate_key $KEY;
    ssl_protocols       TLSv1.2 TLSv1.3;
    root $WEB;
    index index.html;
    location / { try_files \$uri \$uri/ =404; }
    access_log /var/log/nginx/gaika.access.log;
    error_log  /var/log/nginx/gaika.error.log;
}
NGX
ln -s "$CONF" /etc/nginx/sites-enabled/gaika.tech
if ! nginx -t 2>&1; then rm -f /etc/nginx/sites-enabled/gaika.tech "$CONF"; die "nginx -t failed, removed the new site, nothing reloaded"; fi
systemctl reload nginx
sleep 2
for h in gaika.tech gaika.store; do
  echo "$h origin: $(curl -sk -o /dev/null -w '%{http_code}' --resolve $h:443:127.0.0.1 https://$h/)"
done
echo "DONE. Rollback: rm /etc/nginx/sites-enabled/gaika.tech $CONF && systemctl reload nginx"
