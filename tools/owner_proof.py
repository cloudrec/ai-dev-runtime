#!/usr/bin/env python3
"""Owner-only proof for ONE explicit agent command (agent_control owner proof, v1).

Run by the owner as root on the host — never by an agent, ChatGPT/MCP or a wake path, which
must not be able to read the key. Prints the proof JSON only; the key is never printed.

  tools/owner_proof.py init-key                     # once: 32 random bytes, root 0600
  tools/owner_proof.py sign --action agent_send --target vtb:0.0 --text-file msg.txt
  printf 'status?' | tools/owner_proof.py sign --action agent_send --target vtb:0.0
  tools/owner_proof.py sign --action agent_stop --target T --text-file /dev/null

Send the command with header `X-Owner-Proof: <printed JSON>` (or `owner_proof=` in
process). The proof binds action, target, the exact text (sha256), a timestamp (valid
OWNER_PROOF_TTL_SECS) and a single-use nonce: it cannot be reused, retargeted or edited.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv=None, stdin=None) -> int:
    from core import agent_control as ac
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-key")
    s = sub.add_parser("sign")
    s.add_argument("--action", required=True,
                   choices=("agent_send", "agent_answer", "control_keys", "agent_stop"))
    s.add_argument("--target", required=True)
    s.add_argument("--text-file")
    a = ap.parse_args(argv)
    path = ac._owner_key_path()
    if a.cmd == "init-key":
        if os.path.exists(path):
            print(json.dumps({"ok": False, "reason": f"{path} exists; not overwritten"}))
            return 1
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32) + "\n")
        print(json.dumps({"ok": True, "key_file": path}))
        return 0
    key = ac._owner_key()
    if key is None:
        print(json.dumps({"ok": False, "reason": "owner key missing or not root-only 0600"}))
        return 1
    if a.text_file:
        with open(a.text_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = (stdin or sys.stdin).read()
    print(json.dumps(ac.sign_owner_proof(key, a.action, a.target, text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
