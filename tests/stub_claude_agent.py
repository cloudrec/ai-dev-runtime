"""Stand-in for a Claude Code session in a PRIVATE tmux server (tests only).

Started as `exec -a claude python3 stub_claude_agent.py NOTES LOG` so agent_control's
process check sees a Claude pane. Reads what agent_control types (tmux paste + Enter),
treats a quiet gap as the end of one message, then:
  * CTO request  -> runs the exact reply command block it was given (as a Claude would);
  * CTO advice   -> appends the advised line to NOTES (the "command -> result");
and goes back to an idle prompt. Every message is logged to LOG as one JSON line.
"""
import json
import re
import select
import subprocess
import sys

NOTES, LOG = sys.argv[1], sys.argv[2]
REQUEST = "[Owner OS CTO request"
ADVICE = "[Owner OS CTO advice"
DECISION = {"decision": "append the review line", "rationale": "canary checklist complete",
            "owner_needed": False,
            "message_to_agent": "Append the line `step 7 reviewed` to notes.txt."}


def prompt():
    sys.stdout.write("\n❯ \n")
    sys.stdout.flush()


def read_message():
    lines = []
    while True:
        r, _, _ = select.select([sys.stdin], [], [], 0.6 if lines else None)
        if not r:
            return "\n".join(lines)
        line = sys.stdin.readline()
        if not line:
            return "\n".join(lines) if lines else None
        lines.append(line.rstrip("\n"))


def log(entry):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def handle(msg):
    if msg.startswith(REQUEST):
        m = re.search(r"Reply exactly once:\n(.*?) <<'JSON'\n", msg, re.S)
        cmd = m.group(1) + " <<'JSON'\n" + json.dumps(DECISION) + "\nJSON\n"
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
        log({"kind": "request", "reply_rc": res.returncode, "reply": res.stdout.strip(),
             "stderr": res.stderr.strip()[-300:]})
        print(f"replied: {res.stdout.strip()}")
    elif msg.startswith(ADVICE):
        m = re.search(r"`([^`]+)`", msg)
        with open(NOTES, "a") as fh:
            fh.write(m.group(1) + "\n")
        log({"kind": "advice", "applied": m.group(1)})
        print("applied advice")
    else:
        log({"kind": "other", "text": msg[:200]})


def main():
    prompt()
    while True:
        msg = read_message()
        if msg is None:
            return
        if msg.strip():
            handle(msg.strip())
        prompt()


if __name__ == "__main__":
    main()
