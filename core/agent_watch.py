"""Agent watch — real tmux agent states become owner notifications.

On 2026-08-14 two agents sat blocked in plain sight: gaika-ext-audit waiting for
migration instructions, gaika-ip-seal at a literal `Do you want to proceed? 1 Yes / 3 No`
permission prompt — and the owner learned it by prodding panes manually, because the
orchestrator's state estimator had both panes at `unknown` and nothing else reads pane
text. This module is the missing reader: poll the live tmux inventory, classify each
agent's TAIL, and emit a CTO event on every meaningful transition. The wake bridge then
routes it to the project's chat like any other event.

Classes and what they mean to the owner:

  owner_prompt — a permission/decision menu is on screen RIGHT NOW  -> actionable wake
  blocker      — work is paused waiting for instructions/input      -> actionable wake
  completed    — a working agent came to rest after substantive work -> completion wake
  crashed      — the Claude process is gone from a known agent pane  -> critical wake
  working/idle — no notification; being busy is not news

Anti-spam is the core discipline, persisted so a restart cannot replay it:

  * fingerprint = (target, class, digest of the normalized tail region). An unchanged
    waiting prompt is never re-sent, poll after poll, restart after restart.
  * re-arm happens when the agent RESUMES or the prompt CHANGES — plus one deliberate
    reminder per REMINDER_SECS for an unresolved owner-needed item.
  * digests strip volatile digits (spinners, token counters) so cosmetic churn is not
    treated as a new state.

Additive: the existing orchestrator/waiting_transitions path is untouched; dedup at the
CTO layer (dedup_key) keeps the two from double-announcing the same fact.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# One deliberate reminder for an unresolved owner-needed item. 0 disables reminders.
REMINDER_SECS = int(os.getenv("AGENT_WATCH_REMINDER_SECS", "3600"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_watch_state (
    target TEXT PRIMARY KEY,
    cls TEXT, digest TEXT, at TEXT, ts REAL,
    notified_cls TEXT, notified_digest TEXT, notified_at TEXT, notified_ts REAL,
    emissions INTEGER DEFAULT 0
)
"""

# ── classification ──────────────────────────────────────────────────────────
# A permission/decision menu: the Claude CLI's numbered prompt, or an explicit question
# aimed at a human. Checked FIRST — an imperfect upstream state must not hide a literal
# "Do you want to proceed?" sitting on screen.
_OWNER_PROMPT_RE = re.compile(
    r"(do you want to proceed|do you want to|would you like to proceed"
    r"|\(y/n\)|\by/n\b|yes/no"
    r"|❯?\s*1\.\s*yes[\s\S]{0,200}?\bno\b"
    r"|разрешить\?|продолжить\?|подтверд)", re.IGNORECASE)
# Work explicitly at rest, waiting for someone: paused / awaiting instructions / blocked.
_BLOCKER_RE = re.compile(
    r"(waiting for (instructions|input|migration|owner|your|approval|confirmation)"
    r"|awaiting (instructions|input|approval|confirmation)"
    r"|development paused|work paused|paused\b.{0,80}waiting"
    r"|жд[уё]\s|ожида(ю|ет|ние)|нужн[ыа].{0,40}(инструкц|решени)"
    r"|need (further |more )?(instructions|guidance|input))", re.IGNORECASE)
# Active execution evidence. ONLY the interrupt affordance counts: it exists exactly
# while something is running. Spinner glyphs and "Brewed for 22s" survive on screen after
# the turn FINISHES, and matching them classified a paused agent as working — which is
# precisely how gaika-ext-audit's "Waiting for migration instructions" went unannounced.
_WORKING_RE = re.compile(
    r"(esc to interrupt|ctrl\+c to interrupt|running…)", re.IGNORECASE)
# Process-level failure text in the tail (beyond the process simply being gone).
_CRASH_RE = re.compile(
    r"(traceback \(most recent call last\)|segmentation fault|killed\b|core dumped"
    r"|claude.{0,20}(exited|crashed))", re.IGNORECASE)

_VOLATILE = re.compile(r"\d+")
_WS = re.compile(r"\s+")

# tmux/Claude-CLI chrome: box borders, status bars, input-box furniture, hints. These are
# DISPLAY, not agent output — they must appear in no digest, no excerpt, no evidence.
_UI_LINE_RE = re.compile(
    r"^\s*(?:[─│╭╮╰╯═┌┐└┘┤├]+.*|❯.*|\?\s+for shortcuts.*|⏵⏵.*|\[caveman\].*"
    r"|.*shift\+tab to cycle.*|.*new task\?\s*/clear.*|.*esc to interrupt.*"
    r"|✻.*|✽.*|·.*)\s*$", re.IGNORECASE)
# Evidence that work is NOT finished, whatever old checkmarks are on screen: running
# shells, open/in-progress task counts, explicit continuation talk.
_CONTINUATION_RE = re.compile(
    r"(still running|shells? (?:are )?running|background (?:shell|task|process)"
    r"|in progress|\bopen\b|todo|continu(?:e|ing)|next step|остал(?:ось|ись)"
    r"|продолж|в процессе"
    # An unchecked box in the CLI's todo widget is an open task by definition — the
    # jobhunter false completion (event 4086) showed exactly this shape at rest.
    r"|[◻☐]|\[ \])", re.IGNORECASE)
# Inventory states that mean ACTIVE — text may never override these into waiting/done.
_ACTIVE_STATES = frozenset({"working", "shell_running"})
# Inventory states in which a decision menu is credible.
_PROMPT_STATES = frozenset({"waiting_owner", "waiting_input", "idle", "unknown", ""})


def _meaningful_lines(tail: str) -> list:
    """The CURRENT response region: strip tmux/CLI chrome, keep real output lines."""
    out = []
    for ln in (tail or "").splitlines():
        s = ln.strip()
        if not s or _UI_LINE_RE.match(s):
            continue
        out.append(s)
    return out


def _bottom_region(tail: str, lines: int = 10) -> str:
    """The last meaningful lines nearest the prompt — classification and fingerprints
    look ONLY here, never at arbitrary scrollback history."""
    return " ".join(_meaningful_lines(tail)[-lines:])


def digest_of(text: str) -> str:
    """Identity of the bottom region: lowercased, whitespace collapsed, digits stripped
    so a ticking counter or spinner frame does not mint a new fingerprint every poll."""
    norm = _WS.sub(" ", _VOLATILE.sub("#", (text or "").lower())).strip()[-800:]
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def excerpt_of(tail: str, limit: int = 300) -> str:
    """`concise last meaningful line(s)` — chrome-free, bounded."""
    return " ".join(_meaningful_lines(tail)[-3:])[-limit:]


def classify(tail: str, *, state: str = "", alive: bool = True, is_agent: bool = True,
             prev_cls: str = "") -> dict:
    """(inventory state, current bottom region) -> one class.

    The structured `agent_list` state is trusted FIRST: an agent the inventory calls
    working is working, and no stale phrase anywhere in scrollback may reclassify it —
    that exact contamination flagged the watcher's own maintenance pane as blocked
    because its scrollback QUOTED a blocker sentence. Text evidence then refines the
    at-rest states, and completion additionally demands the absence of continuation
    evidence: "4 shells still running" or "1 in progress, 4 open" is not done.
    """
    if not alive or not is_agent:
        return {"cls": "crashed", "reason": "process_gone"}
    st = (state or "").strip()
    region = _bottom_region(tail)
    if st in _ACTIVE_STATES:
        return {"cls": "working", "reason": f"inventory_state_{st}"}
    if _CRASH_RE.search(region):
        return {"cls": "crashed", "reason": "crash_text"}
    if _WORKING_RE.search(_WS.sub(" ", tail[-400:] if tail else "")):
        # The live interrupt affordance sits in the chrome we strip; check it raw.
        return {"cls": "working", "reason": "active_execution_evidence"}
    if st in _PROMPT_STATES and _OWNER_PROMPT_RE.search(region):
        return {"cls": "owner_prompt", "reason": "decision_prompt_at_bottom"}
    if _BLOCKER_RE.search(region):
        return {"cls": "blocker", "reason": "paused_waiting_text_at_bottom"}
    if prev_cls == "working":
        if _CONTINUATION_RE.search(region):
            # Came to rest but its own words say the work is not finished. Stay
            # "working" so a REAL finish later is still a fresh transition.
            return {"cls": "working", "reason": "continuation_evidence_at_rest"}
        return {"cls": "completed", "reason": "came_to_rest_after_work"}
    return {"cls": "idle", "reason": "no_signal"}


# ── routing identity ────────────────────────────────────────────────────────
def project_for(agent: dict, conn=None) -> str:
    """Canonical project for a pane: the agent registry's project_id for this target
    first, else the normalized /opt/<name> basename when it names a known project.
    Unmapped is '', which the wake router turns into the owner-os fallback — labelled,
    never dropped."""
    from core import wake_routes
    conn, own = _c(conn)
    try:
        target = agent.get("target") or ""
        try:
            r = conn.execute("SELECT project_id FROM agent WHERE target=?",
                             (target,)).fetchone()
            if r and (r[0] or "").strip():
                return wake_routes.normalize_key(r[0])
        except Exception:  # noqa: BLE001
            pass
        cwd = (agent.get("claude_cwd") or agent.get("cwd") or "").rstrip("/")
        return wake_routes.normalize_key(cwd.rsplit("/", 1)[-1]) if cwd else ""
    finally:
        if own:
            conn.close()


# ── the scan ────────────────────────────────────────────────────────────────
_EVENT_FOR = {
    "owner_prompt": ("agent_prompt_needs_response", "high", True),
    "blocker": ("agent_waiting_input", "high", True),
    "completed": ("task_completed", "info", False),
    "crashed": ("agent_process_failed", "critical", True),
}


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def scan(*, agents: Optional[list] = None, read_fn: Optional[Callable] = None,
         emit_fn: Optional[Callable] = None, conn=None,
         now: Optional[float] = None) -> dict:
    """One watch pass over the live agents. Returns what it emitted and why it skipped.

    Injectable for tests: `agents` (inventory rows), `read_fn(target)->tail`,
    `emit_fn(...)->{'event_id':int}`. Defaults read tmux via core.agent_control and emit
    through the CTO inbox — the same doorway every other event uses, so wake routing,
    dedup and notifications all apply unchanged.
    """
    now = now if now is not None else now_ts()
    if agents is None:
        from core import agent_control
        agents = [a for a in agent_control.agent_list().get("agents", [])
                  if a.get("is_agent")]
    if read_fn is None:
        from core import agent_control

        def read_fn(target):  # noqa: F811
            return agent_control.agent_read(target, 60).get("output", "")
    if emit_fn is None:
        from core.control_plane.cto import emit as emit_fn  # noqa: F811

    conn, own = _conn(conn)
    try:
        emitted, skipped = [], []
        for a in agents:
            target = a.get("target") or ""
            if not target:
                continue
            tail = ""
            try:
                if a.get("alive"):
                    tail = read_fn(target)
            except Exception:  # noqa: BLE001 — one unreadable pane must not stop the sweep
                skipped.append({"target": target, "why": "unreadable"})
                continue
            row = conn.execute(
                "SELECT cls,digest,notified_cls,notified_digest,notified_ts "
                "FROM agent_watch_state WHERE target=?", (target,)).fetchone()
            prev_cls = row[0] if row else ""
            c = classify(tail, state=a.get("state", ""), alive=bool(a.get("alive")),
                         is_agent=bool(a.get("is_agent")), prev_cls=prev_cls)
            cls = c["cls"]
            dg = digest_of(_bottom_region(tail)) if cls != "crashed" else "gone"
            # Persist the observation first — the record of what IS outlives any
            # decision about whether to announce it.
            conn.execute(
                "INSERT INTO agent_watch_state (target,cls,digest,at,ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(target) DO UPDATE SET cls=excluded.cls, digest=excluded.digest,"
                "at=excluded.at, ts=excluded.ts", (target, cls, dg, now_iso(), now))
            # RESUME RE-ARMS. An agent that went back to work has consumed whatever it was
            # asked; if the very same prompt appears again later, that is a new event, not
            # a duplicate of the old one.
            if cls == "working" and row and row[2]:
                conn.execute("UPDATE agent_watch_state SET notified_cls='', "
                             "notified_digest='' WHERE target=?", (target,))
            if cls not in _EVENT_FOR:
                skipped.append({"target": target, "why": f"class_{cls}"})
                continue
            n_cls, n_dg, n_ts = (row[2], row[3], row[4]) if row else ("", "", 0)
            # Completed and crashed dedupe on CLASS alone: one rest period is one
            # completion, however the on-screen text drifts between scans — the 4070/4071
            # double was a digest drifting across an unchanged rest. Waiting classes keep
            # digest sensitivity, because a NEW question genuinely is a new event.
            already = (n_cls == cls if cls in ("completed", "crashed")
                       else (n_cls == cls and n_dg == dg))
            if already:
                overdue = (REMINDER_SECS and cls in ("owner_prompt", "blocker")
                           and (now - float(n_ts or 0)) >= REMINDER_SECS)
                if not overdue:
                    skipped.append({"target": target, "why": "already_notified"})
                    conn.commit()
                    continue
            etype, severity, oar = _EVENT_FOR[cls]
            project = project_for(a, conn=conn)
            ex = excerpt_of(tail) if cls != "crashed" else (excerpt_of(tail) or
                                                           "process gone from pane")
            ev = emit_fn(
                "agent_watch", etype, project_id=project, agent_id=target,
                severity=severity, owner_action_required=oar,
                payload={"target": target, "class": cls, "digest": dg,
                         "cwd": a.get("claude_cwd") or a.get("cwd") or "",
                         "project": project or "(unmapped -> owner-os)",
                         "excerpt": ex},
                action_taken=f"{target} [{project or 'unmapped'}]: {cls} — {ex[:120]}",
                correlation_id=f"agentwatch:{target}",
                dedup_key=f"agentwatch:{target}:{cls}:{dg}",
                dedup_window_secs=(REMINDER_SECS or 86400), conn=conn)
            conn.execute(
                "UPDATE agent_watch_state SET notified_cls=?, notified_digest=?, "
                "notified_at=?, notified_ts=?, emissions=emissions+1 WHERE target=?",
                (cls, dg, now_iso(), now, target))
            conn.commit()
            emitted.append({"target": target, "class": cls, "project": project,
                            "event_id": (ev or {}).get("event_id")})
        # A tracked pane that VANISHED mid-flight is a crash even though the inventory no
        # longer lists it (a dead pane has no Claude, so is_agent filtering hides it). A
        # pane that vanished from rest just left; only interrupted work is news.
        present = {a.get("target") for a in agents}
        for target, prev_cls, n_cls in conn.execute(
                "SELECT target, cls, notified_cls FROM agent_watch_state").fetchall():
            if target in present or prev_cls in ("crashed", "idle", "completed"):
                continue
            if n_cls == "crashed":
                continue
            project_row = conn.execute("SELECT project_id FROM agent WHERE target=?",
                                       (target,)).fetchone()
            from core import wake_routes as _wr
            project = _wr.normalize_key((project_row[0] if project_row else "") or "")
            ev = emit_fn(
                "agent_watch", "agent_process_failed", project_id=project,
                agent_id=target, severity="critical", owner_action_required=True,
                payload={"target": target, "class": "crashed",
                         "previous_class": prev_cls,
                         "project": project or "(unmapped -> owner-os)",
                         "excerpt": "pane vanished while " + (prev_cls or "tracked")},
                action_taken=f"{target}: pane vanished while {prev_cls or 'tracked'}",
                correlation_id=f"agentwatch:{target}",
                dedup_key=f"agentwatch:{target}:vanished",
                dedup_window_secs=86400, conn=conn)
            conn.execute(
                "UPDATE agent_watch_state SET cls='crashed', digest='gone', "
                "notified_cls='crashed', notified_digest='gone', notified_at=?, "
                "notified_ts=?, emissions=emissions+1 WHERE target=?",
                (now_iso(), now, target))
            emitted.append({"target": target, "class": "crashed", "project": project,
                            "event_id": (ev or {}).get("event_id")})
        conn.commit()
        return {"emitted": emitted, "skipped": skipped, "agents_seen": len(agents)}
    finally:
        if own:
            conn.close()


def recent_alerts(limit: int = 30, conn=None) -> list:
    """The agent-derived alert history, for the notifications surface."""
    conn, own = _c(conn)
    try:
        rows = conn.execute(
            "SELECT id, ts, type, project_id, agent_id, severity, action_taken "
            "FROM event WHERE source='agent_watch' ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"event_id": r[0], "at": r[1], "type": r[2], "project": r[3],
                 "agent": r[4], "severity": r[5], "summary": r[6]} for r in rows]
    finally:
        if own:
            conn.close()
