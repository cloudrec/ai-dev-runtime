#!/usr/bin/env python3
"""Owner-only proof for ONE explicit agent command (agent_control owner proof, v1).

Run by the owner as root on the host — never by an agent, ChatGPT/MCP or a wake path, which
must not be able to read the key. Prints the proof JSON only; the key is never printed.

  tools/owner_proof.py init-key                     # once: 32 random bytes, root 0600
  tools/owner_proof.py sign --action agent_send --target vtb:0.0 --text-file msg.txt
  printf 'status?' | tools/owner_proof.py sign --action agent_send --target vtb:0.0
  tools/owner_proof.py sign --action agent_stop --target T --text-file /dev/null
  tools/owner_proof.py sign-api-launch --session S --dir /abs/worktree [--model opus --risk high]
  tools/owner_proof.py reanchor                     # restore API ledger triggers
  tools/owner_proof.py reanchor --accept-chain      # the anchor cannot vouch for the chain

Reading reanchor: `api_chain_needs_owner_review` with anchored_rows_before above rows means
rows were deleted, so investigate before --accept-chain. Before rotating the key, run a plain
reanchor and keep its rows: afterwards the old anchor's count can no longer be verified.
owner_proof_used_* in restored_triggers means the spent table may have been rebuilt, so a
general proof could have been replayed inside its expiry.

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
    api = sub.add_parser("sign-api-launch", help="authorise ONE API-credit launch (spends money)")
    api.add_argument("--session", required=True)
    api.add_argument("--dir", required=True)
    api.add_argument("--model", default="sonnet")
    api.add_argument("--risk", choices=("normal", "high"), default=None)
    api.add_argument("--conversation", default=None)
    re_a = sub.add_parser("reanchor", help="re-sign the API chain head; restore its triggers")
    re_a.add_argument("--accept-chain", action="store_true",
                      help="the anchor cannot vouch for the chain: accept it as complete")
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
    if a.cmd == "reanchor":
        from core import agent_auth_mode as aam
        r = aam.reanchor(accept_chain=a.accept_chain)
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    if a.cmd == "sign-api-launch":
        from core import agent_auth_mode as aam
        launcher = str(aam.load_config().get("launcher") or aam.DEFAULT_LAUNCHER)
        print(json.dumps(ac.sign_owner_proof(
            key, aam.API_PROOF_ACTION, a.session,
            aam.api_launch_text(a.session, a.dir, a.model, a.risk, a.conversation, launcher))))
        return 0
    if a.text_file:
        with open(a.text_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = (stdin or sys.stdin).read()
    print(json.dumps(ac.sign_owner_proof(key, a.action, a.target, text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
