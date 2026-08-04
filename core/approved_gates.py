"""Pre-approved gate registry — the ONLY way Owner OS may answer a dialog.

Everything in this system has, until now, refused every dialog outright. That is still
the default. This module adds a narrow exception: the owner may pre-record EXACT
confirmations, and only those are answered automatically.

Hard rules, all enforced here rather than left to the caller:

  * an entry binds to ONE target and ONE command shape — matched by sha256 of the
    normalised command, or by an ANCHORED regex the owner wrote deliberately;
  * an entry carries a SCOPE (what class of work it belongs to) and an EXPIRY; an
    expired or scope-less entry never matches;
  * the answer text is recorded by the owner, never synthesised;
  * anything not matched — unknown wording, unknown target, expired, ambiguous, or
    matching MORE than one entry — is REFUSED. Deny by default, always;
  * a dialog whose text carries a prohibited marker (payment execution, promotion /
    failover, real orders, credentials, arbitrary destructive verbs) is refused even
    if an entry would otherwise match. The denylist wins over the allowlist.

The registry is data (`config/approved_gates.yaml`); this module is the mechanism.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Optional

CONFIG_PATH = os.getenv("APPROVED_GATES_CONFIG",
                        "/root/ai-dev-runtime/config/approved_gates.yaml")

# Markers that can NEVER be auto-answered, whatever the registry says. These are the
# owner's standing prohibitions expressed as a last-resort veto.
_PROHIBITED_RE = re.compile(
    r"(real\s+payment|payment\s+traffic|charge|payout|refund|settle|"
    r"provider\s+account|promotion|promote\b|failover|cutover|switch\s+traffic|"
    r"live\s+order|place\s+order|market\s+order|withdraw|transfer\s+funds|api[_\s-]?key|"
    r"secret|passphrase|password|private\s+key|credential|token\b|"
    r"drop\s+database|rm\s+-rf|force[\s-]?push|push\s+.*--force|--force\b|reset\s+--hard|delete\s+(all|everything))",
    re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def command_hash(command: str) -> str:
    return hashlib.sha256(_norm(command).encode("utf-8")).hexdigest()


def load_registry(path: Optional[str] = None) -> list:
    """Entries as a list of dicts. A malformed file yields NO entries (fail-closed)."""
    p = path or CONFIG_PATH
    try:
        import yaml
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        entries = data.get("gates") or []
        return [e for e in entries if isinstance(e, dict)]
    except Exception:  # noqa: BLE001
        return []


def _expired(entry: dict, now: float) -> bool:
    exp = entry.get("expires_at")
    if not exp:
        return True                      # no expiry recorded → never usable
    try:
        from datetime import datetime, timezone
        if isinstance(exp, (int, float)):
            return float(exp) < now
        s = str(exp).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() < now
    except Exception:  # noqa: BLE001
        return True


def _entry_matches(entry: dict, target: str, command: str) -> bool:
    if (entry.get("target") or "") != target:
        return False
    want_hash = entry.get("command_sha256")
    if want_hash:
        return command_hash(command) == str(want_hash).strip().lower()
    pattern = entry.get("command_pattern")
    if pattern:
        # Anchored by construction: the owner's pattern must describe the WHOLE command.
        try:
            return bool(re.fullmatch(pattern, _norm(command), re.I))
        except re.error:
            return False
    return False


def match(target: str, command: str, *, scope_allowed=None, now: Optional[float] = None,
          registry: Optional[list] = None) -> dict:
    """Decide whether this exact dialog command may be auto-answered.

    Returns {"allowed": bool, "reason": str, "answer": str|None, "entry_id": str|None}.
    `reason` is always populated so the audit trail explains every refusal.
    """
    now = now if now is not None else time.time()
    cmd = (command or "").strip()
    if not target or not cmd:
        return {"allowed": False, "reason": "no_command_text", "answer": None,
                "entry_id": None}
    if _PROHIBITED_RE.search(cmd):
        return {"allowed": False, "reason": "prohibited_marker_in_command",
                "answer": None, "entry_id": None}

    entries = registry if registry is not None else load_registry()
    hits = []
    for e in entries:
        if not _entry_matches(e, target, cmd):
            continue
        if _expired(e, now):
            hits.append((e, "expired"))
            continue
        if not e.get("scope"):
            hits.append((e, "no_scope"))
            continue
        if scope_allowed is not None and e.get("scope") not in scope_allowed:
            hits.append((e, "scope_not_allowed"))
            continue
        if not str(e.get("answer") or "").strip():
            hits.append((e, "no_recorded_answer"))
            continue
        hits.append((e, "ok"))

    usable = [(e, why) for e, why in hits if why == "ok"]
    if len(usable) > 1:
        return {"allowed": False, "reason": "ambiguous_multiple_entries", "answer": None,
                "entry_id": None}
    if usable:
        e = usable[0][0]
        return {"allowed": True, "reason": f"approved:{e.get('scope')}",
                "answer": str(e["answer"]).strip(), "entry_id": str(e.get("id") or "")}
    if hits:
        return {"allowed": False, "reason": hits[0][1], "answer": None,
                "entry_id": str(hits[0][0].get("id") or "")}
    return {"allowed": False, "reason": "no_matching_approval", "answer": None,
            "entry_id": None}
