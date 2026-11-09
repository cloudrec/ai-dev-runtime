#!/usr/bin/env bash
# Owner's decision 2026-11-09 (option 2): a ROOT agent `fruit-migration` that ChatGPT may drive
# with agent_send, to move the Fruit casino sites by SSH. It runs as root, unsandboxed: it can
# read every key in /root/.ssh. Owner runs this.  Undo: bash stop.sh
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SRC=/opt/fc-projects/fruit-casino-migration
DIR=/opt/fruit-migration
S=fruit-migration
die() { echo "STOP: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
tmux has-session -t "=$S" 2>/dev/null && die "session $S exists already"

# 1. the code, as root's own copy (no hard links into the sandboxed agent's tree)
#    git runs as fcagent inside its own tree and writes a bundle; root clones the bundle,
#    so root git never opens the sandboxed agent's repository
if [ ! -d "$DIR" ]; then
  B=$(mktemp -u /tmp/fruit-XXXXXX.bundle)
  setpriv --reuid="$(id -u fcagent)" --regid="$(id -g fcagent)" --clear-groups \
    env -i PATH=/usr/bin:/bin HOME=/home/fcagent git -C "$SRC" bundle create -q "$B" HEAD main
  git clone -q -b main "$B" "$DIR"
  git -C "$DIR" remote remove origin
  rm -f "$B"
fi
echo "code: $DIR at $(git -C $DIR log -1 --format='%h %s' | cut -c1-80)"

# 2. ChatGPT (caller api) may type into this one session; every other target stays as it was.
#    ai-runtime reads the allowlist from configs/.env (its EnvironmentFile wins over drop-ins):
#    only that one line changes, after a 0600 backup of the file.
ENVF=/root/ai-dev-runtime/configs/.env
[ "$(grep -c '^AGENT_AUTOMATION_TARGETS=' $ENVF)" = 1 ] || die "allowlist line not exactly once in configs/.env"
if ! grep -q "^AGENT_AUTOMATION_TARGETS=.*[=,]$S@api\(,\|$\)" $ENVF; then
  install -m 600 $ENVF "$ENVF.bak-fruit-$(date +%Y%m%d%H%M%S)"
  sed -i "s/^\(AGENT_AUTOMATION_TARGETS=.*\)$/\1,$S@api/" $ENVF
  systemctl restart ai-runtime
fi
sleep 3; systemctl is-active -q ai-runtime || die "ai-runtime did not start"
P=$(systemctl show ai-runtime -p MainPID --value)
eff=$(tr '\0' '\n' < /proc/$P/environ | sed -n 's/^AGENT_AUTOMATION_TARGETS=//p')
case ",$eff," in *",$S@api,"*) echo "allowlist (effective): $eff";; *) die "allowlist not in effect: $eff";; esac

# 3. the agent
tmux new-session -d -s "$S" -c "$DIR" -x 200 -y 50 /root/.local/bin/claude
for i in $(seq 1 30); do
  sleep 2
  if tmux capture-pane -p -t "=$S:" | grep -q "Yes, I trust this folder"; then
    tmux send-keys -t "=$S:" Down; sleep 1; tmux send-keys -t "=$S:" Enter; break
  fi
  tmux capture-pane -p -t "=$S:" | grep -q "auto mode" && break
done
sleep 6
tmux capture-pane -p -t "=$S:" | grep -q "auto mode" || die "agent not at its prompt: tmux attach -t $S"
echo "DONE: agent $S running as root in $DIR. Tell ChatGPT: агент fruit-migration запущен в $DIR, давай ему задания через agent_send."
