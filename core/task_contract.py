"""Agent Fabric — machine-readable Task Contract + fail-closed state machine.

A task handed to any agent (tmux/Claude pane or runtime worker) is governed by
one durable contract: what to do, what may not be touched, what "done" must
prove, and which powers (push/deploy) the task carries. The state machine is
fail-closed: only the transitions named here exist, VERIFIED_DONE is reachable
ONLY from VERIFYING and ONLY with recorded evidence, and every transition is
appended to an immutable history — a claim of completion without verification
evidence is structurally impossible to record as verified.

States (spec, task OWNER-192): WORKING, BLOCKED, OWNER_DECISION, AGENT_DONE,
VERIFYING, VERIFICATION_FAILED, VERIFIED_DONE — plus CREATED as the entry
state a contract holds before an agent picks the task up.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# ── contract shape ──────────────────────────────────────────────────────────
REQUIRED_FIELDS = ("goal", "acceptance_criteria")
CONTRACT_FIELDS = {
    "goal": str,                    # what must be achieved
    "scope": list,                  # paths/areas the agent may touch
    "do_not_touch": list,           # paths/areas that are off limits
    "acceptance_criteria": list,    # each independently checkable
    "tests_required": bool,
    "live_check_required": bool,
    "push_allowed": bool,           # default False — a contract without the
    "deploy_allowed": bool,         # power simply does not carry it
    "owner_decisions": list,        # decisions only the owner may take
    "expected_report": str,         # where/what the final report must be
}
_DEFAULTS = {"scope": [], "do_not_touch": [], "tests_required": True,
             "live_check_required": False, "push_allowed": False,
             "deploy_allowed": False, "owner_decisions": [], "expected_report": ""}


class ContractError(ValueError):
    pass


def validate_contract(data: dict) -> dict:
    """Normalize + validate a contract dict. Unknown keys are refused (a typo'd
    power like `pushallowed` must fail loudly, not silently grant nothing)."""
    if not isinstance(data, dict):
        raise ContractError("contract must be an object")
    unknown = set(data) - set(CONTRACT_FIELDS)
    if unknown:
        raise ContractError(f"unknown contract field(s): {sorted(unknown)}")
    for f in REQUIRED_FIELDS:
        if not data.get(f):
            raise ContractError(f"required contract field missing/empty: {f}")
    out = {**_DEFAULTS, **data}
    for k, typ in CONTRACT_FIELDS.items():
        if k in out and not isinstance(out[k], typ):
            raise ContractError(f"field {k} must be {typ.__name__}")
    if not all(isinstance(c, str) and c.strip() for c in out["acceptance_criteria"]):
        raise ContractError("acceptance_criteria must be non-empty strings")
    return out


# ── state machine ───────────────────────────────────────────────────────────
CREATED = "CREATED"
WORKING = "WORKING"
BLOCKED = "BLOCKED"
OWNER_DECISION = "OWNER_DECISION"
AGENT_DONE = "AGENT_DONE"
VERIFYING = "VERIFYING"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
VERIFIED_DONE = "VERIFIED_DONE"
CANCELLED = "CANCELLED"

STATES = frozenset({CREATED, WORKING, BLOCKED, OWNER_DECISION, AGENT_DONE,
                    VERIFYING, VERIFICATION_FAILED, VERIFIED_DONE, CANCELLED})
TERMINAL = frozenset({VERIFIED_DONE, CANCELLED})

TRANSITIONS = {
    CREATED: {WORKING, BLOCKED, OWNER_DECISION, CANCELLED},
    WORKING: {BLOCKED, OWNER_DECISION, AGENT_DONE, CANCELLED},
    BLOCKED: {WORKING, OWNER_DECISION, CANCELLED},
    OWNER_DECISION: {WORKING, BLOCKED, CANCELLED},
    # AGENT_DONE is a CLAIM, not a result: the only way forward is verification.
    AGENT_DONE: {VERIFYING, CANCELLED},
    VERIFYING: {VERIFIED_DONE, VERIFICATION_FAILED},
    VERIFICATION_FAILED: {WORKING, BLOCKED, OWNER_DECISION, CANCELLED},
    VERIFIED_DONE: set(),
    CANCELLED: set(),
}

# Transitions that MUST carry evidence to be recorded at all.
_EVIDENCE_REQUIRED = {VERIFIED_DONE, VERIFICATION_FAILED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fabric_contract (
    id TEXT PRIMARY KEY,
    task_id INTEGER, agent_ref TEXT, project TEXT,
    contract TEXT, state TEXT,
    created_at TEXT, created_ts REAL, updated_at TEXT, updated_ts REAL
);
CREATE TABLE IF NOT EXISTS fabric_contract_transition (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT, from_state TEXT, to_state TEXT,
    by TEXT, evidence TEXT, at TEXT, ts REAL
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def create(contract: dict, *, task_id: Optional[int] = None, agent_ref: str = "",
           project: str = "", by: str = "owner", conn=None,
           now: Optional[float] = None) -> dict:
    """Create a contract in CREATED. The contract body is validated fail-closed."""
    now = now if now is not None else now_ts()
    body = validate_contract(contract)
    cid = uuid.uuid4().hex[:16]
    conn, own = _conn(conn)
    try:
        conn.execute(
            "INSERT INTO fabric_contract (id, task_id, agent_ref, project, contract,"
            " state, created_at, created_ts, updated_at, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, task_id, agent_ref, project, json.dumps(body), CREATED,
             now_iso(), now, now_iso(), now))
        conn.execute(
            "INSERT INTO fabric_contract_transition (contract_id, from_state,"
            " to_state, by, evidence, at, ts) VALUES (?,?,?,?,?,?,?)",
            (cid, "", CREATED, by, "", now_iso(), now))
        conn.commit()
        return get(cid, conn=conn)
    finally:
        if own:
            conn.close()


def get(contract_id: str, conn=None) -> Optional[dict]:
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT * FROM fabric_contract WHERE id=?",
                         (contract_id,)).fetchone()
        if not r:
            return None
        cols = [d[0] for d in conn.execute(
            "SELECT * FROM fabric_contract LIMIT 0").description]
        d = dict(zip(cols, r))
        d["contract"] = json.loads(d["contract"]) if d["contract"] else {}
        return d
    finally:
        if own:
            conn.close()


def history(contract_id: str, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        rows = conn.execute(
            "SELECT from_state, to_state, by, evidence, at FROM"
            " fabric_contract_transition WHERE contract_id=? ORDER BY id",
            (contract_id,)).fetchall()
        return [{"from": r[0], "to": r[1], "by": r[2],
                 "evidence": json.loads(r[3]) if r[3] else {}, "at": r[4]}
                for r in rows]
    finally:
        if own:
            conn.close()


def transition(contract_id: str, to_state: str, *, by: str,
               evidence: Optional[dict] = None, conn=None,
               now: Optional[float] = None) -> dict:
    """Fail-closed transition. Returns the updated contract or raises
    ContractError with the exact refusal reason."""
    now = now if now is not None else now_ts()
    if to_state not in STATES:
        raise ContractError(f"unknown state: {to_state}")
    conn, own = _conn(conn)
    try:
        cur = get(contract_id, conn=conn)
        if not cur:
            raise ContractError(f"no such contract: {contract_id}")
        frm = cur["state"]
        if to_state not in TRANSITIONS[frm]:
            raise ContractError(f"illegal transition {frm} -> {to_state}")
        if to_state in _EVIDENCE_REQUIRED and not evidence:
            raise ContractError(
                f"{to_state} requires evidence; a bare claim is not recordable")
        if to_state == VERIFIED_DONE:
            body = cur["contract"]
            if body.get("tests_required") and not (evidence or {}).get("tests"):
                raise ContractError(
                    "contract requires tests; evidence carries no tests result")
            if body.get("live_check_required") and not (evidence or {}).get("live_check"):
                raise ContractError(
                    "contract requires a live check; evidence carries none")
        conn.execute(
            "UPDATE fabric_contract SET state=?, updated_at=?, updated_ts=?"
            " WHERE id=?", (to_state, now_iso(), now, contract_id))
        conn.execute(
            "INSERT INTO fabric_contract_transition (contract_id, from_state,"
            " to_state, by, evidence, at, ts) VALUES (?,?,?,?,?,?,?)",
            (contract_id, frm, to_state, by,
             json.dumps(evidence or {}), now_iso(), now))
        conn.commit()
        return get(contract_id, conn=conn)
    finally:
        if own:
            conn.close()


def list_contracts(*, state: Optional[str] = None, task_id: Optional[int] = None,
                   limit: int = 100, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        q, args = "SELECT id FROM fabric_contract", []
        clauses = []
        if state:
            clauses.append("state=?")
            args.append(state)
        if task_id is not None:
            clauses.append("task_id=?")
            args.append(task_id)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_ts DESC LIMIT ?"
        args.append(limit)
        return [get(r[0], conn=conn) for r in conn.execute(q, args).fetchall()]
    finally:
        if own:
            conn.close()
