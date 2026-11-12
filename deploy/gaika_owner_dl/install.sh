#!/usr/bin/env bash
# Owner order 2026-11-12 ("внедряй"): password-protected download of GAIKA owner test builds
# at https://gaika.tech/owner/ . Files live in /var/www/gaika-owner (not under the public root).
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
SITE=/etc/nginx/sites-available/gaika.tech; DL=/var/www/gaika-owner; PW=/etc/nginx/gaika-owner.htpasswd
SECRET=/root/.gaika-owner-download-password
[ -f "$SITE" ] || die "no $SITE"
mkdir -p "$DL"; chmod 755 "$DL"
if [ ! -f "$SECRET" ]; then umask 077; openssl rand -base64 18 | tr -d '/+=' | cut -c1-16 > "$SECRET"; fi
printf 'owner:%s\n' "$(openssl passwd -apr1 "$(cat "$SECRET")")" > "$PW"; chown root:www-data "$PW"; chmod 640 "$PW"
cp -a "$SITE" "$SITE.bak-owner-dl"
if ! grep -q 'location /owner/' "$SITE"; then
  python3 - "$SITE" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
blk='''    location /owner/ {
        alias /var/www/gaika-owner/;
        auth_basic "GAIKA owner";
        auth_basic_user_file /etc/nginx/gaika-owner.htpasswd;
        autoindex on;
        add_header Cache-Control "no-store";
    }
'''
a='    location / { try_files $uri $uri/ =404; }\n'
assert a in s; s=s.replace(a, a+blk,1); open(p,'w').write(s)
PY
fi
if ! nginx -t 2>/dev/null; then cp -a "$SITE.bak-owner-dl" "$SITE"; die "nginx -t failed, vhost restored"; fi
systemctl reload nginx
echo "DONE: https://gaika.tech/owner/ (user owner, password in $SECRET)"
echo "Rollback: cp $SITE.bak-owner-dl $SITE && systemctl reload nginx && rm -rf $DL $PW"
