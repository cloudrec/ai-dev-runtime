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


# Maintenance suppression is TEMPORARY by construction. The v4 static env exclusion
# fixed self-observation (event 4096) and then hid a REAL owner prompt on the same pane
# the moment maintenance ended — a permanent blind spot. A suppression row carries an
# expiry; once it lapses, the pane is a normal agent again and its next prompt alerts.
def suppress(target: str, *, ttl_secs: int, reason: str, by: str = "owner", conn=None,
             now: Optional[float] = None) -> dict:
    """Suppress one target for a bounded time. Audited, idempotent, self-expiring."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        until = now + max(0, int(ttl_secs))
        conn.execute("INSERT OR REPLACE INTO agent_watch_suppress "
                     "(target, until_ts, at, by, reason) VALUES (?,?,?,?,?)",
                     ((target or "").strip(), until, now_iso(), by, reason[:200]))
        conn.commit()
        return {"ok": True, "target": target, "until_ts": until}
    finally:
        if own:
            conn.close()


def is_suppressed(target: str, conn=None, now: Optional[float] = None) -> bool:
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT until_ts FROM agent_watch_suppress WHERE target=?",
                         ((target or "").strip(),)).fetchone()
        return bool(r) and float(r[0] or 0) > now
    finally:
        if own:
            conn.close()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_watch_state (
    target TEXT PRIMARY KEY,
    cls TEXT, digest TEXT, at TEXT, ts REAL,
    notified_cls TEXT, notified_digest TEXT, notified_at TEXT, notified_ts REAL,
    emissions INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_alert_invalid (
    event_id INTEGER PRIMARY KEY, at TEXT, ts REAL, by TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS agent_watch_suppress (
    target TEXT PRIMARY KEY, until_ts REAL, at TEXT, by TEXT, reason TEXT
)
"""

_STATE_COLUMNS = (("miss_count", "INTEGER DEFAULT 0"),)


def _migrate_state(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(agent_watch_state)")}
    for name, decl in _STATE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE agent_watch_state ADD COLUMN {name} {decl}")

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
    r"^\s*(?:[─│╭╮╰╯═┌┐└┘┤├╌┄┈]+.*|❯(?!\s*\d+\.).*|\?\s+for shortcuts.*|⏵⏵.*|\[caveman\].*"
    r"|.*shift\+tab to cycle.*|.*new task\?\s*/clear.*|.*esc to interrupt.*"
    r"|[✻✽✶✳✢✣✤✥✦✧✨*]\s*\S+…\s*$|✻.*|✽.*|·.*"
    # the running-subagent widget rows and their token/duration counters (event 4255's
    # entire "summary" was three of these)
    r"|[◯●◉]\s.*|↓?\s*\d+(?:\.\d+)?k\b.*)\s*$", re.IGNORECASE)
# NOTE: `❯` lines are chrome (the composer) EXCEPT `❯ 1.` — a selected menu option is
# part of the question being asked.
# Evidence that work is NOT finished, whatever old checkmarks are on screen: running
# shells, open/in-progress task counts, explicit continuation talk.
_CONTINUATION_RE = re.compile(
    r"(still running|shells? (?:are )?running|background (?:shell|task|process)"
    r"|in progress|\bopen\b|todo|continu(?:e|ing)|next step|остал(?:ось|ись)"
    r"|продолж|в процессе"
    # An unchecked box in the CLI's todo widget is an open task by definition — the
    # jobhunter false completion (event 4086) showed exactly this shape at rest.
    r"|[◻☐]|\[ \]"
    # Running-subagent widget rows and live token counters (events 4255/4281): visible
    # subagents ARE work in flight, whatever the parent pane's state metadata says.
    r"|[◯◉]\s|↓\s*\d+(?:\.\d+)?k|\b\d+m\s+\d+s\b"
    # "running" in any rendering — including the column-mangled "runn ing" that event
    # 4300 wore — is a progress fragment, never a finish.
    r"|\brunning\b|\brunn\s+ing\b"
    # Conditional future intent is a QUESTION wearing a period (event 4485: "I'll
    # proceed. Otherwise the ... is complete." awaits the owner's objection), and an
    # interrupted shell notice (event 4456: '... was stopped') is an incident, not an
    # ending.
    r"|\bi'?ll (?:proceed|continue)\b|\bi will (?:proceed|continue)\b|\botherwise\b"
    r"|\bif you\b|\bunless\b|\bwas stopped\b)", re.IGNORECASE)
# Inventory states that mean ACTIVE — text may never override these into waiting/done.
_ACTIVE_STATES = frozenset({"working", "shell_running"})
# Inventory states in which a decision menu is credible.
_PROMPT_STATES = frozenset({"waiting_owner", "waiting_input", "idle", "unknown", ""})
# A numbered choice menu — "1. <option> ... 2. <option>" — whatever the option wording.
# Event 4088 was a five-option strategy menu with none of the yes/no vocabulary.
_MENU_RE = re.compile(r"\b1\.\s+\S.{0,300}?\b2\.\s+\S")
# POSITIVE final evidence. Completion is never inferred from mere quietness: idle after
# work with no finish statement stays un-announced (event 4086 was "idle" from UI
# metadata while an internal task list sat half-open).
_FINISH_RE = re.compile(
    r"(finish(?:ed)?\b|complet(?:e|ed)\b|\ball done\b|\bdone[.!]|final report"
    r"|all (?:checks|tests) pass(?:ed)?|завершен[оа]?|готово)", re.IGNORECASE)
# TOOL/HARNESS telemetry wearing finish vocabulary. Event 5051: a live working
# agent was announced task_completed because its pane showed `Background command
# "…monitor output" completed (exit code 0)` — a SHELL exiting, not the agent
# finishing. A completion notice about a command/monitor/subprocess is progress
# telemetry by definition; it can never satisfy the positive-finish requirement,
# whatever finish words it contains.
_TOOL_COMPLETION_RE = re.compile(
    r"(\(exit code \d+\)|exit(?:ed)? with code|\breturn code\b"
    r"|background (?:command|task|shell).{0,80}?(?:completed|finished|was stopped)"
    r"|command (?:completed|finished|exited)"
    r"|monitor .{0,60}?(?:stream ended|completed)"
    r"|task-notification|<system-reminder>)", re.IGNORECASE)


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


# Menu navigation footer — display furniture, never part of the question.
_FOOTER_RE = re.compile(r"(enter to select|↑/↓|↑ ?↓|esc to cancel|tab to cycle)",
                        re.IGNORECASE)


def excerpt_of(tail: str, limit: int = 300, cls: str = "") -> str:
    """`concise last meaningful line(s)` — chrome-free, bounded.

    For a prompt, the excerpt must carry the QUESTION AND ITS OPTIONS, not the widget
    footer: event 4100's summary was "4. Type something. 5. Chat about this Enter to
    select…" — the two throwaway options and the navigation hint, everything except what
    the owner was actually being asked."""
    lines = [ln for ln in _meaningful_lines(tail) if not _FOOTER_RE.search(ln)]
    if cls == "owner_prompt":
        first_opt = next((i for i, ln in enumerate(lines)
                          if re.match(r"^❯?\s*1\.\s+\S", ln)), None)
        if first_opt is not None:
            start = max(0, first_opt - 2)          # the question usually sits just above
            return " ".join(lines[start:first_opt + 6])[:limit]
    return " ".join(lines[-3:])[-limit:]


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
    # An ACTIVE spinner — "✶ Dilly-dallying…", a verb with a trailing ellipsis — at the
    # very bottom of the raw pane is execution in flight (event 4281 completed on one).
    # Only the last three raw lines count: a stale spinner higher up must not suppress a
    # genuine prompt menu sitting at the bottom (the 4187 shape).
    # The CLI's spinner cycles through a family of star glyphs; event 4456 wore
    # `✢ Transfiguring…`, one member the earlier class missed.
    raw_bottom = [ln for ln in (tail or "").splitlines() if ln.strip()][-3:]
    if any(re.search(r"[✻✽✶✳✢✣✤✥✦✧✨]\s*\S+…", ln) for ln in raw_bottom):
        return {"cls": "working", "reason": "active_spinner_at_bottom"}
    # waiting_owner ALWAYS outranks completion: the inventory says a human is being
    # asked, and event 4088 proved a choice menu can follow substantive work and read
    # like a finish. A menu in any at-rest state is likewise a question, not an ending.
    if st == "waiting_owner":
        return {"cls": "owner_prompt", "reason": "inventory_waiting_owner"}
    if st in _PROMPT_STATES and (_OWNER_PROMPT_RE.search(region)
                                 or _MENU_RE.search(region)):
        return {"cls": "owner_prompt", "reason": "decision_prompt_at_bottom"}
    if _BLOCKER_RE.search(region):
        return {"cls": "blocker", "reason": "paused_waiting_text_at_bottom"}
    if prev_cls == "working":
        # Finish evidence must sit in the FINAL response lines, not anywhere in the
        # region: event 4300 "completed" on finish vocabulary from scrollback while the
        # actual last line was a mangled progress fragment.
        final = _WS.sub(" ", " ".join(_meaningful_lines(tail)[-3:]))
        if _CONTINUATION_RE.search(region) or not _FINISH_RE.search(final):
            # Came to rest, but either its own words say the work continues, or its
            # closing lines never SAID it finished. Stay "working" so a real, stated
            # finish later is still a fresh transition — quietness alone never completes.
            return {"cls": "working", "reason": "no_positive_finish_evidence"}
        if _TOOL_COMPLETION_RE.search(final):
            # The finish vocabulary belongs to a command/monitor/subprocess notice
            # (event 5051): a shell exiting is not the agent finishing its task.
            return {"cls": "working", "reason": "tool_completion_is_not_task_finish"}
        return {"cls": "completed", "reason": "stated_finish_at_rest"}
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
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    _migrate_state(conn)
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
            if is_suppressed(target, conn=conn, now=now):
                skipped.append({"target": target, "why": "suppressed_maintenance"})
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
            # RECOVERY RECONCILES THE RECORD. A pane announced crashed that is now
            # observed ALIVE (event 5123: payorch declared dead at 09:11, visibly
            # working minutes later) must not leave a critical crash alert standing
            # as current truth. The event row is never touched — the alert is retired
            # via the audited invalid overlay, exactly like a proven-false alert.
            if row and row[2] == "crashed" and a.get("alive") and cls != "crashed":
                _reconcile_recovered_crash(conn, target, now)
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
            if (not already and n_cls == cls and n_dg != dg
                    and (now - float(n_ts or 0)) < REMINDER_SECS):
                # Same class, same agent, still unresolved, only the fingerprint moved —
                # a watcher upgrade or cosmetic redraw, not a new fact (events 4084/4085
                # were exactly this after a classifier deploy). Adopt the new digest
                # silently; the reminder interval still guards true staleness.
                conn.execute("UPDATE agent_watch_state SET notified_digest=? "
                             "WHERE target=?", (dg, target))
                already = True
            if already:
                overdue = (REMINDER_SECS and cls in ("owner_prompt", "blocker")
                           and (now - float(n_ts or 0)) >= REMINDER_SECS)
                if not overdue:
                    skipped.append({"target": target, "why": "already_notified"})
                    conn.commit()
                    continue
            etype, severity, oar = _EVENT_FOR[cls]
            project = project_for(a, conn=conn)
            ex = excerpt_of(tail, cls=cls) or "process gone from pane"
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
        if present:
            conn.execute("UPDATE agent_watch_state SET miss_count=0 WHERE target IN (%s)"
                         % ",".join("?" * len(present)), tuple(present))
        for target, prev_cls, n_cls, misses in conn.execute(
                "SELECT target, cls, notified_cls, COALESCE(miss_count,0) "
                "FROM agent_watch_state").fetchall():
            if target in present or is_suppressed(target, conn=conn, now=now) \
                    or prev_cls in ("crashed", "idle", "completed"):
                continue
            if misses < 1:
                # ONE missed scan is not a death: a busy Claude process can drop out of
                # the inventory for a single sweep (event 4393 declared this session's
                # own live pane crashed on exactly that). Two consecutive absences are
                # the threshold; presence resets the count.
                conn.execute("UPDATE agent_watch_state SET miss_count=1 WHERE target=?",
                             (target,))
                conn.commit()
                skipped.append({"target": target, "why": "absent_once_waiting_confirm"})
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


def _reconcile_recovered_crash(conn, target: str, now: float) -> list:
    """Retire crash alerts contradicted by the pane being alive again. Marks every
    un-invalidated agent_process_failed event for this target invalid (audited
    overlay, event rows untouched) and acknowledges their pending wakes so a
    recovered process cannot keep paging as dead."""
    retired = []
    try:
        rows = conn.execute(
            "SELECT e.id FROM event e LEFT JOIN agent_alert_invalid i "
            "ON i.event_id = e.id WHERE e.source='agent_watch' "
            "AND e.type='agent_process_failed' AND e.agent_id=? "
            "AND i.event_id IS NULL ORDER BY e.id DESC LIMIT 5", (target,)).fetchall()
        for (eid,) in rows:
            mark_invalid(eid, reason=f"process observed alive at {now_iso()} — "
                                     "transient/recovered, not a current crash",
                         by="agent_watch", conn=conn, now=now)
            try:
                from core import wake_bridge
                wake_bridge.acknowledge(eid, conn=conn, now=now)
            except Exception:  # noqa: BLE001
                pass
            retired.append(eid)
    except Exception:  # noqa: BLE001 — reconciliation must never break the sweep
        pass
    return retired


def mark_invalid(event_id: int, *, reason: str, by: str = "owner", conn=None,
                 now: Optional[float] = None) -> dict:
    """Retire a proven-false alert from the DEFAULT view. The event row itself is never
    touched — the audit trail keeps every mistake — this is a labelled overlay, exactly
    like wake coalescing: retired, never deleted."""
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        conn.execute("INSERT OR REPLACE INTO agent_alert_invalid "
                     "(event_id, at, ts, by, reason) VALUES (?,?,?,?,?)",
                     (int(event_id), now_iso(), now, by, reason[:200]))
        conn.commit()
        return {"ok": True, "event_id": int(event_id), "reason": reason[:200]}
    finally:
        if own:
            conn.close()


def recent_alerts(limit: int = 30, include_invalid: bool = False, conn=None) -> list:
    """The agent-derived alert history, for the notifications surface. Alerts marked
    invalid are omitted from the default view — a woken assistant reading notifications
    must see current truth, not retired mistakes — and remain reachable with
    include_invalid=True for audit."""
    conn, own = _conn(conn)
    try:
        q = ("SELECT e.id, e.ts, e.type, e.project_id, e.agent_id, e.severity, "
             "e.action_taken, i.reason FROM event e "
             "LEFT JOIN agent_alert_invalid i ON i.event_id = e.id "
             "WHERE e.source='agent_watch' ")
        if not include_invalid:
            q += "AND i.event_id IS NULL "
        rows = conn.execute(q + "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = {"event_id": r[0], "at": r[1], "type": r[2], "project": r[3],
                    "agent": r[4], "severity": r[5], "summary": r[6]}
            if r[7]:
                item["invalid"] = r[7]
            out.append(item)
        return out
    finally:
        if own:
            conn.close()
