"""Owner OS Operating Constitution — the enforcement mechanism.

`docs/OWNER_OS_OPERATING_CONSTITUTION.md` is the law, `config/owner_os_policy.yaml` is
the machine-readable form of it, and this module is what actually stops an action. The
point of it is stated in one line: **no rule here depends on an agent having read the
instructions.** An agent that never opened the constitution is subject to exactly the
same gates as one that quoted it, because the gates live on the execution path.

Two chokepoints:

  * `preflight(...)` — evaluated BEFORE a mutating action. Returns HARD_BLOCK (never
    proceeds without an owner override), REQUIRE_OWNER (needs a recorded approval),
    REQUIRE_EVIDENCE (needs a proven rollback path first) or ALLOW.
  * `completion_gate(...)` — evaluated BEFORE anything is called DONE. A claim without
    the structured evidence its risk class demands is not a completion; it is a report
    of unverified work, and it comes back as `blocked`/`unverified`, never green.

Everything it decides is written to `policy_decision` — allowed and blocked alike — so
"was this evaluated?" is answerable from data rather than from trust. An emergency
override is owner-scoped, expiring, single-purpose and audited: it can permit a blocked
action, it cannot hide that it did.

Fail-closed everywhere: an unparseable policy file, an unknown action, a missing
evidence field, an expired override or an unreadable state are all reasons to STOP,
never reasons to continue optimistically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

CONFIG_PATH = os.getenv("OWNER_OS_POLICY_CONFIG",
                        "/root/ai-dev-runtime/config/owner_os_policy.yaml")

# ── vocabulary ─────────────────────────────────────────────────────────────
READ_ONLY = "READ_ONLY"
MUTATING = "MUTATING"
HIGH_RISK = "HIGH_RISK"
IRREVERSIBLE = "IRREVERSIBLE"
RISK_ORDER = (READ_ONLY, MUTATING, HIGH_RISK, IRREVERSIBLE)

ALLOW = "ALLOW"
REQUIRE_EVIDENCE = "REQUIRE_EVIDENCE"
REQUIRE_OWNER = "REQUIRE_OWNER"
HARD_BLOCK = "HARD_BLOCK"
DECISION_ORDER = (ALLOW, REQUIRE_EVIDENCE, REQUIRE_OWNER, HARD_BLOCK)

PHASE_PREFLIGHT = "preflight"
PHASE_COMPLETION = "completion"

# Terminal claim states that are NOT a success — a gate failure lands on one of these
# rather than downgrading into a green.
CLAIM_BLOCKED = "blocked"
CLAIM_UNVERIFIED = "unverified"


class PolicyError(RuntimeError):
    """Raised only when the policy itself cannot be read — never to signal a denial."""


# ── policy loading (fail-closed) ───────────────────────────────────────────
_cache: Dict[str, Any] = {"mtime": None, "policy": None, "path": None}


def load_policy(path: Optional[str] = None, *, force: bool = False) -> dict:
    """Load + cache the machine-readable policy. A missing or malformed file is a hard
    error: running with no policy would silently mean running with no rules."""
    p = path or CONFIG_PATH
    try:
        mtime = os.path.getmtime(p)
    except OSError as e:
        raise PolicyError(f"policy file unreadable: {p} ({e})") from e
    if not force and _cache["policy"] is not None and _cache["mtime"] == mtime and _cache["path"] == p:
        return _cache["policy"]
    try:
        import yaml
        with open(p, "r", encoding="utf-8") as fh:
            pol = yaml.safe_load(fh) or {}
    except Exception as e:  # noqa: BLE001
        raise PolicyError(f"policy file unparseable: {p} ({e})") from e
    for key in ("risk_classes", "hard_block", "require_owner"):
        if key not in pol:
            raise PolicyError(f"policy missing required section: {key}")
    _cache.update({"mtime": mtime, "policy": pol, "path": p})
    return pol


def _rx(pattern: str):
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


# ── R6: redaction — applied to everything this module stores or returns ────
def redact(text: Any, policy: Optional[dict] = None) -> Any:
    """Replace secret-shaped substrings. Applied to every reason, evidence blob and
    audit row, so a secret cannot reach a report even by accident."""
    if text is None:
        return text
    if isinstance(text, dict):
        return {k: redact(v, policy) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return [redact(v, policy) for v in text]
    if not isinstance(text, str):
        return text
    pol = policy or _safe_policy()
    red = (pol.get("redaction") or {})
    repl = red.get("replacement", "[REDACTED]")
    out = text
    for pat in red.get("patterns", []):
        rx = _rx(pat)
        if rx:
            out = rx.sub(repl, out)
    return out


def _safe_policy() -> dict:
    """Policy for redaction only — never used to make a decision, so a read failure
    here degrades to 'redact nothing extra' rather than to 'allow everything'."""
    try:
        return load_policy()
    except PolicyError:
        return {}


# ── classification ─────────────────────────────────────────────────────────
def _action_text(action: Any) -> str:
    if isinstance(action, dict):
        return " ".join(str(v) for v in action.values() if v)
    return str(action or "")


def classify(action: Any, *, declared: Optional[str] = None, policy: Optional[dict] = None) -> dict:
    """Risk-classify an action. Returns {risk_class, matched_rules, gate_rule}.

    Deny-by-default: text that matches no read-only pattern is MUTATING at minimum. A
    caller may DECLARE a higher class but never a lower one — self-declaration can only
    tighten the gate.
    """
    pol = policy or load_policy()
    text = _action_text(action)
    matched: List[str] = []
    risk = None
    gate_rule = None

    for entry in pol.get("hard_block", []):
        for pat in entry.get("patterns", []):
            rx = _rx(pat)
            if rx and rx.search(text):
                matched.append(entry["id"])
                return {"risk_class": IRREVERSIBLE, "matched_rules": matched,
                        "gate_rule": entry["id"], "hard_block": True,
                        "why": entry.get("why", "")}

    for entry in pol.get("require_owner", []):
        for pat in entry.get("patterns", []):
            rx = _rx(pat)
            if rx and rx.search(text):
                matched.append(entry["id"])
                cand = entry.get("risk", HIGH_RISK)
                if risk is None or RISK_ORDER.index(cand) > RISK_ORDER.index(risk):
                    risk, gate_rule = cand, entry["id"]
    if risk:
        return {"risk_class": risk, "matched_rules": matched, "gate_rule": gate_rule,
                "hard_block": False, "why": "owner-gated category"}

    for pat in (pol.get("mutating") or {}).get("patterns", []):
        rx = _rx(pat)
        if rx and rx.search(text):
            risk = MUTATING
            break
    if risk is None:
        for pat in (pol.get("read_only") or {}).get("patterns", []):
            rx = _rx(pat)
            if rx and rx.search(text):
                risk = READ_ONLY
                break
    # nothing recognised → treat as mutating (deny-by-default), never read-only
    risk = risk or MUTATING
    if declared and declared in RISK_ORDER and RISK_ORDER.index(declared) > RISK_ORDER.index(risk):
        risk = declared          # self-declaration may only raise the class
    return {"risk_class": risk, "matched_rules": matched, "gate_rule": None,
            "hard_block": False, "why": ""}


# ── evidence validation (structured, not prose) ────────────────────────────
def _evidence_gaps(risk: str, evidence: Optional[dict], *, phase: str,
                   policy: Optional[dict] = None) -> List[str]:
    """Which required evidence fields are missing or unusable for this risk class."""
    pol = policy or load_policy()
    spec = (pol.get("risk_classes") or {}).get(risk) or {}
    ev = evidence or {}
    gaps: List[str] = []

    required = list(spec.get("completion_evidence", [])) if phase == PHASE_COMPLETION else []
    if phase == PHASE_PREFLIGHT and spec.get("backup_required"):
        required = ["rollback"]

    schema = pol.get("evidence_schema") or {}
    for field in required:
        val = ev.get(field)
        if val in (None, "", [], {}):
            gaps.append(field)
            continue
        req_keys = (schema.get(field) or {}).get("required", [])
        if req_keys and not isinstance(val, dict):
            gaps.append(f"{field}(structured fields required: {','.join(req_keys)})")
            continue
        for k in req_keys:
            if isinstance(val, dict) and val.get(k) in (None, ""):
                gaps.append(f"{field}.{k}")
        # a rollback that names no restorable reference is not a rollback
        if field == "rollback" and isinstance(val, dict):
            if val.get("kind") == "none" or not val.get("ref"):
                gaps.append("rollback.ref(no restorable reference)")
        # R4: 'tests ran' is not 'tests passed'
        if field == "tests" and isinstance(val, dict) and val.get("ok") is not True:
            gaps.append("tests.ok(false)")
        # R3: a live surface must be proven active, not merely commanded
        if field == "live" and isinstance(val, dict) and val.get("active") is not True:
            gaps.append("live.active(false)")
    return gaps


# ── owner override (expiring, audited, never hidden) ───────────────────────
def grant_override(*, actor: str, scope: str, reason: str, ttl_secs: int,
                   rules: Optional[List[str]] = None, task_id: str = "",
                   conn=None) -> dict:
    """Record an owner-scoped emergency override. Bounded TTL, mandatory reason, and a
    durable row — an override is always visible in the audit and in reports."""
    pol = load_policy()
    ocfg = pol.get("override") or {}
    max_ttl = int(ocfg.get("max_ttl_secs", 3600))
    min_reason = int(ocfg.get("require_reason_chars", 12))
    allowed = ocfg.get("allowed_actors") or ["owner"]
    if actor not in allowed:
        raise PolicyError(f"override actor not permitted: {actor!r} (allowed: {allowed})")
    if len(reason.strip()) < min_reason:
        raise PolicyError(f"override reason too short (min {min_reason} chars)")
    ttl = max(1, min(int(ttl_secs), max_ttl))
    oid = f"ovr_{uuid.uuid4().hex[:12]}"
    exp_ts = now_ts() + ttl
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO policy_override(id,created_at,actor,scope,rules,reason,task_id,"
            "expires_at,expires_ts,uses) VALUES(?,?,?,?,?,?,?,?,?,0)",
            (oid, now_iso(), actor, scope, json.dumps(rules or []), redact(reason.strip()),
             task_id, _iso_at(exp_ts), exp_ts))
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"id": oid, "expires_ts": exp_ts, "scope": scope, "ttl_secs": ttl}


def _iso_at(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def active_override(scope: str, *, rule: str = "", conn=None) -> Optional[dict]:
    """The live override covering this scope/rule, or None. Expiry is evaluated on read,
    so an override cannot outlive its TTL by never being cleaned up."""
    conn, own = _c(conn)
    try:
        rows = conn.execute(
            "SELECT id,actor,scope,rules,reason,expires_at,expires_ts,uses,revoked_at "
            "FROM policy_override WHERE scope=? AND revoked_at IS NULL ORDER BY rowid DESC",
            (scope,)).fetchall()
        now = now_ts()
        for r in rows:
            if (r[6] or 0) <= now:
                continue                       # expired → not active, no cleanup needed
            rules = json.loads(r[3] or "[]")
            if rules and rule and rule not in rules:
                continue                       # scoped to other rules
            return {"id": r[0], "actor": r[1], "scope": r[2], "rules": rules,
                    "reason": r[4], "expires_at": r[5], "expires_ts": r[6], "uses": r[7]}
        return None
    finally:
        if own:
            conn.close()


def revoke_override(override_id: str, conn=None) -> bool:
    conn, own = _c(conn)
    try:
        cur = conn.execute("UPDATE policy_override SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                           (now_iso(), override_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def list_overrides(*, include_expired: bool = True, conn=None) -> List[dict]:
    """Every override ever granted. Reports read this — an override cannot be omitted."""
    conn, own = _c(conn)
    try:
        rows = conn.execute(
            "SELECT id,created_at,actor,scope,rules,reason,task_id,expires_at,expires_ts,"
            "uses,revoked_at FROM policy_override ORDER BY rowid DESC").fetchall()
        now = now_ts()
        out = []
        for r in rows:
            expired = (r[8] or 0) <= now
            if expired and not include_expired:
                continue
            out.append({"id": r[0], "created_at": r[1], "actor": r[2], "scope": r[3],
                        "rules": json.loads(r[4] or "[]"), "reason": r[5], "task_id": r[6],
                        "expires_at": r[7], "expires_ts": r[8], "uses": r[9],
                        "revoked_at": r[10], "expired": expired,
                        "active": (not expired) and r[10] is None})
        return out
    finally:
        if own:
            conn.close()


def _consume_override(override_id: str, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute("UPDATE policy_override SET uses=uses+1 WHERE id=?", (override_id,))
        conn.commit()
    finally:
        if own:
            conn.close()


# ── R7: one live claim per (project, idempotency key) ──────────────────────
def idem_key(*, project: str, action: Any, task_id: str = "") -> str:
    """Identity of the WORK, not of the worker.

    The key deliberately excludes `task_id`: two tasks asking for the same action in the
    same project are a duplicate, which is the case this guard exists to stop. A retry by
    the SAME task is recognised separately, by comparing the task recorded on the claim.
    """
    raw = f"{project}|{_action_text(action).strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _duplicate_claim(key: str, *, task_id: str, conn=None) -> Optional[dict]:
    pol = load_policy()
    window = int((pol.get("concurrency") or {}).get("duplicate_window_secs", 86400))
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT idem_key,task_id,actor,project,state,created_at,created_ts "
                         "FROM policy_claim WHERE idem_key=? AND state='active'", (key,)).fetchone()
        if not r:
            return None
        if (now_ts() - (r[6] or 0)) > window:
            return None                        # outside the window → no longer a duplicate
        if task_id and r[1] == task_id:
            return None                        # the same task retrying is not a duplicate
        return {"idem_key": r[0], "task_id": r[1], "actor": r[2], "project": r[3],
                "state": r[4], "created_at": r[5]}
    finally:
        if own:
            conn.close()


def _claim(key: str, *, task_id: str, actor: str, project: str, action: Any,
           risk: str, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO policy_claim(idem_key,task_id,actor,project,action,risk_class,"
            "state,created_at,created_ts) VALUES(?,?,?,?,?,?,'active',?,?) "
            "ON CONFLICT(idem_key) DO UPDATE SET task_id=excluded.task_id,state='active'",
            (key, task_id, actor, project, redact(_action_text(action))[:400], risk,
             now_iso(), now_ts()))
        conn.commit()
    finally:
        if own:
            conn.close()


def release_claim(key: str, conn=None) -> None:
    """Release a claim when its task reaches a terminal state, so the next legitimate
    run of the same action is not mistaken for a duplicate."""
    conn, own = _c(conn)
    try:
        conn.execute("UPDATE policy_claim SET state='released', released_at=? WHERE idem_key=?",
                     (now_iso(), key))
        conn.commit()
    finally:
        if own:
            conn.close()


# ── audit ──────────────────────────────────────────────────────────────────
def _record(phase: str, *, actor: str, project: str, task_id: str, action: Any,
            risk: str, decision: str, rules: List[str], missing: List[str],
            evidence: Optional[dict], override_id: str, key: str, reason: str,
            conn=None) -> int:
    conn, own = _c(conn)
    try:
        cur = conn.execute(
            "INSERT INTO policy_decision(ts,phase,actor,project,task_id,action,risk_class,"
            "decision,rules,missing_evidence,evidence,override_id,idem_key,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), phase, actor, project, task_id,
             redact(_action_text(action))[:1000], risk, decision, json.dumps(rules),
             json.dumps(missing), json.dumps(redact(evidence or {}))[:4000],
             override_id, key, redact(reason)[:500]))
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def decisions(*, task_id: str = "", limit: int = 50, conn=None) -> List[dict]:
    conn, own = _c(conn)
    try:
        sql = ("SELECT id,ts,phase,actor,project,task_id,action,risk_class,decision,rules,"
               "missing_evidence,override_id,reason FROM policy_decision")
        args: tuple = ()
        if task_id:
            sql += " WHERE task_id=?"
            args = (task_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        args += (int(limit),)
        rows = conn.execute(sql, args).fetchall()
        cols = ("id", "ts", "phase", "actor", "project", "task_id", "action", "risk_class",
                "decision", "rules", "missing_evidence", "override_id", "reason")
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["rules"] = json.loads(d["rules"] or "[]")
            d["missing_evidence"] = json.loads(d["missing_evidence"] or "[]")
            out.append(d)
        return out
    finally:
        if own:
            conn.close()


# ── the two chokepoints ────────────────────────────────────────────────────
def preflight(*, action: Any, actor: str = "agent", project: str = "", task_id: str = "",
              scope: Optional[List[str]] = None, evidence: Optional[dict] = None,
              owner_approved: bool = False, declared_risk: Optional[str] = None,
              override_scope: str = "", conn=None) -> dict:
    """Evaluate an action BEFORE it runs. Never executes anything.

    Returns a decision dict: {allowed, decision, risk_class, violated_rules,
    missing_evidence, required_gate, reason, override, audit_id, idem_key}.
    """
    pol = load_policy()
    cls = classify(action, declared=declared_risk, policy=pol)
    risk = cls["risk_class"]
    rules = list(cls["matched_rules"])
    key = idem_key(project=project, action=action, task_id=task_id)
    ovr_scope = override_scope or (project or "global")
    decision = ALLOW
    missing: List[str] = []
    required_gate = ""
    reason = ""
    override = None

    # R2: scope containment — a declared scope that does not cover the target is a stop,
    # not a warning. Silent scope growth is how "one fix" becomes three projects.
    target = _target_path(action)
    if scope and target and not any(target.startswith(s.rstrip("/")) for s in scope):
        decision, reason = HARD_BLOCK, f"target {target} is outside the task scope {scope}"
        rules.append("R2.1-scope")

    if decision != HARD_BLOCK and cls.get("hard_block"):
        decision = HARD_BLOCK
        required_gate = cls["gate_rule"]
        reason = f"{cls['gate_rule']}: {cls.get('why') or 'prohibited action'}"

    # R7: the duplicate guard runs before anything is permitted to proceed.
    if decision != HARD_BLOCK:
        dup = _duplicate_claim(key, task_id=task_id, conn=conn)
        if dup:
            decision = HARD_BLOCK
            rules.append("R7.2-duplicate")
            reason = (f"duplicate action already claimed by task {dup['task_id']} "
                      f"({dup['created_at']}) — not started twice")

    if decision != HARD_BLOCK and cls["gate_rule"]:
        required_gate = cls["gate_rule"]
        decision = REQUIRE_OWNER if not owner_approved else ALLOW
        reason = (f"{cls['gate_rule']} requires owner approval"
                  if not owner_approved else f"{cls['gate_rule']} approved by owner")

    # R1: a mutating action needs a proven rollback path BEFORE it starts.
    if decision in (ALLOW, REQUIRE_EVIDENCE):
        gaps = _evidence_gaps(risk, evidence, phase=PHASE_PREFLIGHT, policy=pol)
        if gaps:
            missing = gaps
            decision = REQUIRE_EVIDENCE
            rules.append("R1.1-rollback")
            reason = f"no verified rollback path: missing {', '.join(gaps)}"

    # An override can lift a block — and is recorded doing so.
    if decision in (HARD_BLOCK, REQUIRE_OWNER, REQUIRE_EVIDENCE):
        ovr = active_override(ovr_scope, rule=required_gate or (rules[0] if rules else ""),
                              conn=conn)
        if ovr:
            override = ovr
            _consume_override(ovr["id"], conn=conn)
            reason = f"OVERRIDDEN by {ovr['id']} ({ovr['actor']}, expires {ovr['expires_at']}): {reason}"
            decision = ALLOW

    if decision == ALLOW and risk != READ_ONLY:
        _claim(key, task_id=task_id, actor=actor, project=project, action=action,
               risk=risk, conn=conn)

    audit_id = _record(PHASE_PREFLIGHT, actor=actor, project=project, task_id=task_id,
                       action=action, risk=risk, decision=decision, rules=rules,
                       missing=missing, evidence=evidence,
                       override_id=(override or {}).get("id", ""), key=key,
                       reason=reason, conn=conn)
    return {"allowed": decision == ALLOW, "decision": decision, "risk_class": risk,
            "violated_rules": rules, "missing_evidence": missing,
            "required_gate": required_gate, "reason": redact(reason),
            "override": override, "audit_id": audit_id, "idem_key": key,
            "phase": PHASE_PREFLIGHT}


def completion_gate(*, action: Any, evidence: Optional[dict] = None, actor: str = "agent",
                    project: str = "", task_id: str = "", claimed_status: str = "completed",
                    declared_risk: Optional[str] = None, health_ok: Optional[bool] = None,
                    override_scope: str = "", conn=None) -> dict:
    """Evaluate a DONE claim BEFORE it is recorded. Returns the status that may actually
    be written (`status`), which is `blocked`/`unverified` when the evidence does not
    support the claim. A failed health check is never a completion.
    """
    pol = load_policy()
    cls = classify(action, declared=declared_risk, policy=pol)
    risk = cls["risk_class"]
    rules: List[str] = []
    key = idem_key(project=project, action=action, task_id=task_id)
    gaps = _evidence_gaps(risk, evidence, phase=PHASE_COMPLETION, policy=pol)
    decision, reason, status = ALLOW, "", claimed_status
    override = None

    if health_ok is False:
        # R3.4 / R8.1: a failed post-change health check means rollback-required, not green.
        rules.append("R3.4-health")
        gaps = gaps or []
        gaps.append("live.health(failed)")

    if gaps:
        rules.append("R4.1-evidence")
        decision = REQUIRE_EVIDENCE
        status = CLAIM_UNVERIFIED if claimed_status == "completed" else claimed_status
        reason = (f"DONE refused for risk {risk}: missing evidence {', '.join(gaps)} "
                  f"(BUILD ≠ TESTED ≠ DEPLOYED ≠ VERIFIED)")

    if decision != ALLOW:
        ovr = active_override(override_scope or (project or "global"),
                              rule="R4.1-evidence", conn=conn)
        if ovr:
            override = ovr
            _consume_override(ovr["id"], conn=conn)
            reason = f"OVERRIDDEN by {ovr['id']} ({ovr['actor']}): {reason}"
            decision, status = ALLOW, claimed_status

    if decision == ALLOW:
        release_claim(key, conn=conn)

    audit_id = _record(PHASE_COMPLETION, actor=actor, project=project, task_id=task_id,
                       action=action, risk=risk, decision=decision, rules=rules,
                       missing=gaps, evidence=evidence,
                       override_id=(override or {}).get("id", ""), key=key,
                       reason=reason, conn=conn)
    return {"allowed": decision == ALLOW, "decision": decision, "risk_class": risk,
            "violated_rules": rules, "missing_evidence": gaps, "status": status,
            "reason": redact(reason), "override": override, "audit_id": audit_id,
            "phase": PHASE_COMPLETION}


_PATH_RE = re.compile(r"(/[\w.\-/]{3,})")


def _target_path(action: Any) -> str:
    """Best-effort absolute target path referenced by an action (for scope containment)."""
    if isinstance(action, dict):
        for k in ("path", "target", "file", "project_path"):
            if action.get(k):
                return str(action[k])
    m = _PATH_RE.search(_action_text(action))
    return m.group(1) if m else ""


def explain(action: Any, **kw) -> dict:
    """Dry evaluation for CLI/MCP: what would happen, and why — writes no claim.

    Deliberately mirrors `preflight` minus side effects, so an operator can ask the
    policy a question without creating a duplicate claim or consuming an override.
    """
    pol = load_policy()
    cls = classify(action, declared=kw.get("declared_risk"), policy=pol)
    risk = cls["risk_class"]
    gaps = _evidence_gaps(risk, kw.get("evidence"), phase=PHASE_PREFLIGHT, policy=pol)
    if cls.get("hard_block"):
        decision = HARD_BLOCK
    elif cls["gate_rule"]:
        decision = ALLOW if kw.get("owner_approved") else REQUIRE_OWNER
    elif gaps:
        decision = REQUIRE_EVIDENCE
    else:
        decision = ALLOW
    return {"decision": decision, "risk_class": risk,
            "violated_rules": cls["matched_rules"], "missing_evidence": gaps,
            "required_gate": cls["gate_rule"] or "",
            "completion_evidence_required":
                (pol.get("risk_classes") or {}).get(risk, {}).get("completion_evidence", [])}
