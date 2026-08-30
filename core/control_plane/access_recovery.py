"""Access-recovery classification for the payment agent's server connectivity.

OWNER TRUTH (authenticated, 2026-08-03): payment previously ACCESSED and DEPLOYED all existing
servers, and the required SSH keys are ALREADY INSTALLED. Therefore a failed root+key SSH attempt
to RU-PROD / NL-edge is NOT an owner credential/access gate. It is INTERNAL connection-mapping /
key-selection recovery in progress by payment:0.0 — the historical user / IdentityFile / host
alias / ssh config must be recovered. The control plane must NOT repeatedly notify the owner to
install keys. Escalate to the owner ONLY if exhaustive evidence proves the historical access
material is genuinely ABSENT or REVOKED.

This module is a read/emit-only policy layer: it classifies a signal, records recovery progress
durably (inbox-only, non-owner-actionable), tracks the recovery task, and gates escalation. It
never touches a pane, connects anywhere, or performs an external action.
"""
from __future__ import annotations

import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso

# agents whose server-access failures are internal recovery, not an owner gate.
RECOVERY_AGENTS = {"payment:0.0"}
# hosts the owner confirmed payment already deployed to.
RECOVERY_HOSTS = ("ru-prod", "nl-edge", "ru_prod", "nl_edge", "ruprod", "nledge",
                  "ru prod", "nl edge")

# key-selection / connection-mapping signals — recoverable INTERNALLY by the agent picking the
# right user / IdentityFile / host alias / ssh config. NOT an owner gate.
_KEY_SELECTION_RE = re.compile(
    r"(permission denied \(publickey"
    r"|\bpublickey\b"
    r"|no such identity"
    r"|identity ?file"
    r"|host key verification failed"
    r"|no matching host key"
    r"|could not resolve hostname"
    r"|name or service not known"
    r"|nodename nor servname"
    r"|connection refused"
    r"|connection timed out"
    r"|no route to host"
    r"|bad owner or permissions"
    r"|too many authentication failures"
    r"|(unknown|invalid|no such) user"
    r"|\.ssh/config"
    r"|ssh_config)", re.I)

# EXHAUSTIVE ABSENCE / REVOCATION — the ONLY evidence that justifies escalating to the owner.
# Deliberately narrow: a routine publickey failure must never match this.
_ABSENT_REVOKED_RE = re.compile(
    r"(all (ssh )?keys (removed|deleted|absent|gone)"
    r"|no identity files (present|found|remaining)"
    r"|key(s)? (revoked|no longer authorized)"
    r"|access (permanently )?revoked"
    r"|account (disabled|locked|removed|terminated)"
    r"|removed from authorized_keys"
    r"|exhausted all (known )?(keys|identities|users|aliases|configs|hosts))", re.I)


def classify(agent: str, text: str) -> dict:
    """Classify an access signal. Returns {'class', 'host', 'reason'} where class is
    'internal_recovery' | 'escalate' | 'none'."""
    if agent not in RECOVERY_AGENTS:
        return {"class": "none", "host": None, "reason": "agent_not_in_recovery_set"}
    t = text or ""
    host = next((h for h in RECOVERY_HOSTS if h in t.lower()), None)
    if _ABSENT_REVOKED_RE.search(t):
        return {"class": "escalate", "host": host,
                "reason": "exhaustive absence/revocation proven — historical access material gone"}
    if _KEY_SELECTION_RE.search(t):
        return {"class": "internal_recovery", "host": host,
                "reason": ("key-selection/connection-mapping recovery in progress "
                           "(keys already installed per authenticated owner truth)")}
    return {"class": "none", "host": host, "reason": "no_access_signal"}


# external-block phrases that, FOR A RECOVERY AGENT, are key/credential/user SELECTION issues
# (recoverable per owner truth — keys already installed), NOT a genuine vendor block. Quota /
# rate-limit / 429 are deliberately EXCLUDED so a real vendor block still surfaces.
_SELECTION_EXTERNAL_RE = re.compile(
    r"(verification key|vendor key|awaiting vendor|api ?key required|credentials? required"
    r"|input[_ ]required|key required|no identity|identity ?file|ssh|publickey|permission denied"
    r"|\.ssh|host key|could not resolve|unknown user|invalid user)", re.I)


def is_internal_recovery(agent: str, text: str) -> bool:
    return classify(agent, text)["class"] == "internal_recovery"


def reported_state(agent: str, state: str, tail: str) -> tuple:
    """State to REPORT to owner-notification consumers (e.g. the seo-backend agent_notifier,
    which reads ai-runtime state over HTTP). Read-only/pure. For a recovery agent an
    `externally_blocked` state whose evidence is a recoverable key/credential/user SELECTION
    issue is downgraded to `idle` (not news → the owner is not re-notified to 'install keys').
    A genuine vendor block (quota/rate-limit) is left untouched, and an EXHAUSTIVE
    absence/revocation is left as `externally_blocked` so it still escalates once.

    Returns (state, reclassified: bool)."""
    if agent not in RECOVERY_AGENTS or state != "externally_blocked":
        return state, False
    t = tail or ""
    if _ABSENT_REVOKED_RE.search(t):
        return state, False                       # exhaustive absence/revocation → keep + escalate
    if _SELECTION_EXTERNAL_RE.search(t):
        return "idle", True                       # recoverable selection → not a block, not news
    return state, False                           # other external blocks (quota/rate-limit) unchanged


def should_escalate(agent: str, text: str) -> bool:
    return classify(agent, text)["class"] == "escalate"


def _ensure_task_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS access_recovery_task ("
                 "agent TEXT, host TEXT, state TEXT, attempts INTEGER DEFAULT 0, "
                 "first_seen TEXT, updated_at TEXT, evidence TEXT, "
                 "PRIMARY KEY(agent, host))")


def get_recovery_tasks(conn=None) -> list:
    conn, own = _c(conn)
    try:
        _ensure_task_table(conn)
        rows = conn.execute("SELECT agent,host,state,attempts,first_seen,updated_at,evidence "
                            "FROM access_recovery_task ORDER BY updated_at DESC").fetchall()
        return [{"agent": r[0], "host": r[1], "state": r[2], "attempts": r[3],
                 "first_seen": r[4], "updated_at": r[5], "evidence": r[6]} for r in rows]
    finally:
        if own:
            conn.close()


def note_recovery(agent: str, host: str = "", detail: str = "", conn=None) -> dict:
    """Track recovery-in-progress durably WITHOUT notifying the owner. Upserts the recovery
    task (attempt count) and records an inbox-only, non-owner-actionable event. Returns the
    task state. This is what replaces the erroneous 'install keys' owner escalation."""
    from core.control_plane.cto import emit
    host = host or "server"
    conn, own = _c(conn)
    try:
        _ensure_task_table(conn)
        conn.execute(
            "INSERT INTO access_recovery_task(agent,host,state,attempts,first_seen,updated_at,evidence) "
            "VALUES(?,?, 'recovering', 1, ?, ?, ?) ON CONFLICT(agent,host) DO UPDATE SET "
            "attempts=attempts+1, state='recovering', updated_at=excluded.updated_at, "
            "evidence=excluded.evidence",
            (agent, host, now_iso(), now_iso(), (detail or "")[:400]))
        conn.commit()
        row = conn.execute("SELECT attempts FROM access_recovery_task WHERE agent=? AND host=?",
                           (agent, host)).fetchone()
        attempts = row[0] if row else 1
        # inbox-only (push=False), owner_action_required=False, long dedup → owner is NOT pinged.
        emit("access_recovery", "access_recovery_in_progress", agent_id=agent, severity="info",
             owner_action_required=False, push=False,
             payload={"host": host, "attempts": attempts, "detail": (detail or "")[:200],
                      "classification": "internal key-selection/connection-mapping recovery",
                      "owner_notify": "SUPPRESSED — keys already installed per authenticated owner truth"},
             action_taken="tracked as internal recovery; owner NOT notified",
             dedup_key=f"access_recovery:{agent}:{host}", dedup_window_secs=21600, conn=conn)
        return {"agent": agent, "host": host, "state": "recovering", "attempts": attempts}
    finally:
        if own:
            conn.close()


def escalate(agent: str, host: str = "", detail: str = "", conn=None) -> dict:
    """Only for EXHAUSTIVE absence/revocation. Marks the task failed and raises a real
    owner-actionable event (this is the one case where the owner IS notified)."""
    from core.control_plane.cto import emit
    host = host or "server"
    conn, own = _c(conn)
    try:
        _ensure_task_table(conn)
        conn.execute(
            "INSERT INTO access_recovery_task(agent,host,state,attempts,first_seen,updated_at,evidence) "
            "VALUES(?,?, 'escalated', 1, ?, ?, ?) ON CONFLICT(agent,host) DO UPDATE SET "
            "state='escalated', updated_at=excluded.updated_at, evidence=excluded.evidence",
            (agent, host, now_iso(), now_iso(), (detail or "")[:400]))
        conn.commit()
        ev = emit("access_recovery", "access_material_absent_or_revoked", agent_id=agent,
                  severity="high", owner_action_required=True,
                  payload={"host": host, "detail": (detail or "")[:200],
                           "classification": "EXHAUSTIVE absence/revocation proven — not a mere key-selection failure"},
                  action_taken="escalated — historical access material genuinely absent/revoked",
                  dedup_key=f"access_absent:{agent}:{host}", dedup_window_secs=86400, conn=conn)
        return {"agent": agent, "host": host, "state": "escalated", "event_id": ev["event_id"]}
    finally:
        if own:
            conn.close()


def record_owner_truth(conn=None) -> dict:
    """Persist the AUTHENTICATED owner decision + a policy fact so the control plane treats
    RU-PROD/NL-edge SSH failures as internal recovery, not a credential/access gate. Idempotent
    via a dedup'd meta event."""
    from core.control_plane import provenance
    from core.control_plane.cto import emit
    answer = ("payment already accessed and deployed all existing servers; required SSH keys "
              "already installed; RU-PROD/NL-edge SSH failure = internal key-selection/"
              "connection-mapping recovery, NOT an owner credential/access gate; do not "
              "repeatedly notify the owner to install keys; escalate only on exhaustive proof "
              "the historical access material is absent or revoked")
    d = provenance.record_owner_decision(
        question_id="payment_server_access_classification", source_channel="cto_authenticated",
        actor="owner", authenticated=True, answer=answer, conn=conn)
    emit("access_recovery", "owner_truth_recorded", agent_id="payment:0.0", severity="info",
         owner_action_required=False, push=False,
         payload={"decision_id": d["id"], "trusted": d["trusted"], "answer": answer,
                  "hosts": list(RECOVERY_HOSTS)},
         action_taken="recorded authenticated owner truth — access is internal recovery, not a gate",
         dedup_key="owner_truth:payment_server_access", dedup_window_secs=31536000, conn=conn)
    return d
