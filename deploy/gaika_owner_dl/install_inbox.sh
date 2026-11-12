#!/usr/bin/env bash
# Owner runs this: upload path for owner-test capture zips (GAIKA-SEND.bat -> PUT /owner/inbox/).
# A small service on 127.0.0.1:8932 behind the same nginx password as /owner/.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
D=$(dirname "$(readlink -f "$0")"); SITE=/etc/nginx/sites-available/gaika.tech
install -m 755 "$D/inbox.py" /usr/local/bin/gaika-owner-inbox
cat > /etc/systemd/system/gaika-owner-inbox.service <<U
[Unit]
Description=GAIKA owner capture inbox (localhost only, behind nginx auth)
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/gaika-owner-inbox
Restart=always
MemoryMax=256M
NoNewPrivileges=yes
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload; systemctl enable --now gaika-owner-inbox
cp -a "$SITE" "$SITE.bak-owner-inbox"
if ! grep -q 'location /owner/inbox/' "$SITE"; then
python3 - "$SITE" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
blk='''    location /owner/inbox/ {
        auth_basic "GAIKA owner";
        auth_basic_user_file /etc/nginx/gaika-owner.htpasswd;
        limit_except PUT { deny all; }
        client_max_body_size 50m;
        proxy_pass http://127.0.0.1:8932;
    }
'''
a='    location /owner/ {\n'
assert a in s; s=s.replace(a, blk+a,1); open(p,'w').write(s)
PY
fi
nginx -t 2>/dev/null || { cp -a "$SITE.bak-owner-inbox" "$SITE"; die "nginx -t failed, restored"; }
systemctl reload nginx; sleep 1; systemctl is-active -q gaika-owner-inbox || die "inbox not active"
echo "DONE: PUT https://gaika.tech/owner/inbox/<gaika-owner-capture-*.zip> -> /var/lib/gaika-owner-inbox"
echo "Rollback: cp $SITE.bak-owner-inbox $SITE && systemctl reload nginx && systemctl disable --now gaika-owner-inbox"
