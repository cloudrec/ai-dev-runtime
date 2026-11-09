#!/usr/bin/env bash
# A headless browser for the sandboxed agents (fcagent): Playwright's Chromium in a shared
# read-only dir the sandbox shows, and the agent's claude told where it is and what it may
# do with it (read-only probes, no logins, no getting around blocks). Owner runs this.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HERE=$(cd "$(dirname "$0")/.." && pwd)
TOOLS=/opt/fc-tools
SRC=/root/.cache/ms-playwright
U=fcagent; H=/home/$U
D=/etc/systemd/system/owner-os-fullcycle-intake.service.d/40-agent-browser.conf
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
for b in chromium_headless_shell-1223 ffmpeg-1011; do [ -d "$SRC/$b" ] || die "$SRC/$b missing"; done
[ -f "$H/.claude/settings.json" ] || die "$U has no claude settings (isolation not installed?)"
install -d -o root -g root -m 755 "$TOOLS" "$TOOLS/ms-playwright"
for b in chromium_headless_shell-1223 ffmpeg-1011; do
  [ -d "$TOOLS/ms-playwright/$b" ] || cp -a "$SRC/$b" "$TOOLS/ms-playwright/"
done
chown -R root:root "$TOOLS"; chmod -R u=rwX,go=rX "$TOOLS"
cp -a "$H/.claude/settings.json" "$H/.claude/settings.json.bak-browser"
install -o $U -g $U -m 600 "$HERE/isolation/fcagent-settings.json" "$H/.claude/settings.json"
mkdir -p "$(dirname "$D")"
printf '[Service]\nEnvironment=AGENT_SANDBOX_KEEP=/opt/fc-projects:%s\n' "$TOOLS" > "$D"
systemctl daemon-reload
systemctl restart owner-os-fullcycle-intake
sleep 3; systemctl is-active -q owner-os-fullcycle-intake || die "worker did not start"
# proof: the agent, in its sandbox, opens a page
cd /root/ai-dev-runtime && AGENT_SANDBOX_KEEP=/opt/fc-projects:$TOOLS venv/bin/python - <<'PY'
import pwd, subprocess, sys
sys.path.insert(0, "/root/ai-dev-runtime")
from core import agent_privilege as priv
p = pwd.getpwnam("fcagent")
t = priv.Target("fcagent", p.pw_uid, p.pw_gid, p.pw_dir)
code = ("from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as pw:\n"
        "    b = pw.chromium.launch(); pg = b.new_page()\n"
        "    pg.goto('https://example.com', timeout=30000); print(pg.title()); b.close()\n")
r = subprocess.run(priv.host_argv(t, ["python3", "-c", code]),
                   env=priv.child_env(t, {"PLAYWRIGHT_BROWSERS_PATH": "/opt/fc-tools/ms-playwright"}),
                   cwd="/", capture_output=True, text=True, timeout=110)
print("agent browser:", r.stdout.strip() or r.stderr.strip()[-200:])
PY
echo "DONE: sandboxed agents have a headless browser (new agents; running ones after restart)"
echo "Rollback: rm $D && systemctl daemon-reload && systemctl restart owner-os-fullcycle-intake && mv $H/.claude/settings.json.bak-browser $H/.claude/settings.json"
