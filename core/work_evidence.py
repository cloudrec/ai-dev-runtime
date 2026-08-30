"""Work-evidence observer — notice finished work, not just moving pointers.

Owner OS could see three things: an agent's observable STATE (working / idle / dead), a
project's stage POINTER, and the task ledger. It could not see WORK. On 2026-08-06 the
MESS agent shipped goal 2, wrote a 9.7 KB report saying goal 1 was audited but
`IMPLEMENTATION NOT STARTED`, committed the result and went idle. Every existing observer
was satisfied: the stage pointer had not moved (`skip:nothing_queued_and_stage_incomplete`),
`idle` is not in the pinger's significant-state map, and the agent was outside the canary
allowlist anyway. The owner was told nothing — not the report, not the half-finished task,
not the agent's decision to stop.

This module closes that. It correlates on EVIDENCE — reports, commits, artifacts, and the
task ledger — rather than on a pointer, and it is deliberately narrow about what counts:

  * a report file that is new or whose content changed materially;
  * commits that did not exist at the last scan;
  * a report whose own wording says part of the work is done and part is not
    (`DONE` alongside `NOT STARTED` / `AUDIT COMPLETE` / `BLOCKED`) — a partial completion;
  * an agent that went idle while its ledger task is still open, or while its own report
    says the requested implementation was never started — work refused, not finished.

Not a file watcher. Ordinary source edits, logs and scratch files raise nothing; only
reports, commits and artifacts do, each fingerprinted so the same evidence is announced
once and never again. Fail-closed in the honest direction: unreadable project, unreadable
report or unavailable git is recorded as `unknown` and skipped, never treated as "nothing
happened".

Read-only with respect to every project it observes: it opens files and runs `git log`.
It never writes into, commits to, or actuates a project.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable, Optional

from core.control_plane import cto
from core.control_plane.api import _c
from core.control_plane.store import now_iso

# Where a project's durable reports live, relative to its checkout. Deliberately a short
# list: everything else in a repository changes for reasons that are not owner news.
REPORT_DIRS = ("reports", "docs")
REPORT_SUFFIXES = (".md",)
MAX_REPORT_BYTES = 512 * 1024
MAX_REPORTS_PER_SCAN = 40
MAX_COMMITS_REPORTED = 10

# Completion vocabulary as agents actually write it in their own reports.
_DONE_RE = re.compile(r"\b(DONE|COMPLETED?|SHIPPED|IMPLEMENTED|VERIFIED PASS)\b")
_NOT_STARTED_RE = re.compile(r"\b(NOT STARTED|NOT IMPLEMENTED|NOT DONE)\b")
_AUDIT_ONLY_RE = re.compile(r"\bAUDIT (COMPLETE|ONLY)\b|\bANALYSIS ONLY\b|\bPLAN ONLY\b")
_BLOCKED_RE = re.compile(r"\bBLOCKED(_EXTERNAL)?\b|\bBLOCKED ON\b")
_UNVERIFIED_RE = re.compile(r"\bUNVERIFIED\b|\bNOT VERIFIED\b")

# A report below this size is read whole, exactly as before — no behaviour change for the
# ordinary case (565 of the 633 report files on this host).
LONG_REPORT_BYTES = int(os.getenv("WORK_EVIDENCE_LONG_REPORT_BYTES", "20000"))

# An explicit, structured completion declaration: "Status: blocked", "**Outcome:** done".
# Authoritative wherever it appears, because it is a declaration rather than narration.
_STATUS_DECL_RE = re.compile(
    r"^[ \t>*_-]*(?:\*\*)?(?:status|state|outcome|result)(?:\*\*)?[ \t]*[:：][ \t]*\S.*$",
    re.IGNORECASE | re.MULTILINE)

EVENT_REPORT = "work_report_published"
EVENT_PARTIAL = "work_partial_completion"
EVENT_STOPPED = "work_stopped_incomplete"
EVENT_COMMITS = "work_commits_without_stage_progress"

DEDUP_WINDOW_SECS = int(os.getenv("WORK_EVIDENCE_DEDUP_WINDOW_SECS", str(6 * 3600)))
# A project observed for the FIRST time is full of reports that are history, not news.
# Announcing them all would be the same spam this observer exists to avoid (the live first
# pass over /opt/mess would have emitted 43 events). Older evidence is recorded as seen
# WITHOUT an event; anything touched inside this window is genuinely recent work and is
# announced. The count of suppressed items is returned, never silently dropped.
BACKFILL_WINDOW_SECS = int(os.getenv("WORK_EVIDENCE_BACKFILL_WINDOW_SECS", str(24 * 3600)))
# Even inside that window a busy project has a day of reports; activating the observer must
# not replay them. On first sight, only the newest few reports can raise events — steady
# state then sees each new report as it lands. `backfilled` reports what this suppressed.
COLD_START_MAX_REPORTS = int(os.getenv("WORK_EVIDENCE_COLD_START_MAX_REPORTS", "3"))

# How long one piece of work may interrupt the owner only once. Live 2026-08-06:
# `MESS_AUTO_UPDATE_AND_MOBILE_MENU_2026-08-06.md` was saved three times in sixteen minutes
# and raised `work_partial_completion` three times — three owner pushes and three wake
# consultations for a single unchanged decision ("this work is partial"). The fingerprint
# was not misbehaving: the bytes really did change. What was wrong is that a NEW SET OF
# BYTES was treated as A NEW THING TO WAKE SOMEONE FOR.
OWNER_ACTION_COOLDOWN_SECS = int(
    os.getenv("WORK_EVIDENCE_OWNER_ACTION_COOLDOWN_SECS", str(30 * 60)))

# Ordering used to decide "did this get WORSE?". A rise in severity is news even inside the
# cooldown; anything else at or below the delivered level is the same interruption again.
_SEVERITY_RANK = {"": 0, "info": 0, "low": 0, "warning": 1, "medium": 1,
                  "high": 2, "critical": 3}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _seen(ev_id: str, fingerprint: str, conn=None) -> bool:
    """Has exactly this evidence already been announced? Fingerprint-based, so a report
    that is rewritten materially is news again while a re-read of the same bytes is not."""
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT fingerprint FROM work_evidence WHERE id=?", (ev_id,)).fetchone()
        if r and r[0] == fingerprint:
            conn.execute("UPDATE work_evidence SET last_seen_at=? WHERE id=?",
                         (now_iso(), ev_id))
            conn.commit()
            return True
        return False
    finally:
        if own:
            conn.close()


def _record(ev_id: str, *, project: str, target: str, kind: str, ref: str,
            fingerprint: str, event_id: int = 0, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute(
            "INSERT INTO work_evidence(id,project,target,kind,ref,fingerprint,first_seen_at,"
            "last_seen_at,event_id) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "fingerprint=excluded.fingerprint,last_seen_at=excluded.last_seen_at,"
            "event_id=excluded.event_id",
            (ev_id, project, target, kind, ref, fingerprint, now_iso(), now_iso(), event_id))
        conn.commit()
    finally:
        if own:
            conn.close()


def _stage_pointer(target: str) -> str:
    """The governed stage this target is on, or "" when there is no readable pointer.

    Only ever used to notice that the stage CHANGED. Unreadable resolves to a stable ""
    rather than to a fresh value, so a broken pointer file cannot manufacture a "the stage
    moved" reason and re-open owner delivery on every scan.
    """
    try:
        from core import continuation_governor as cg
        cfg = (cg.load_config() or {}).get(target) or {}
        ap = cfg.get("authoritative_pointer")
        if not ap:
            return ""
        return str((cg.parse_queue(ap) or {}).get("pointer") or "")
    except Exception:  # noqa: BLE001
        return ""


def _class_sig(kind: str, cls: dict, reason: str = "") -> str:
    """The MEANING of a finding, deliberately excluding the report's bytes.

    Two saves of the same half-finished report produce the same signature; a report that
    stops saying NOT STARTED and starts saying DONE does not.
    """
    flags = ",".join(k for k in ("done", "not_started", "audit_only", "blocked",
                                 "unverified", "partial", "incomplete") if cls.get(k))
    return f"{kind}|{flags}|{reason}"


def _ts_epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _iso_at(now: Optional[float]) -> str:
    """The stamp written for a delivery, on the SAME clock the cooldown is measured with.
    Using `now_iso()` here instead would compare an injected test clock against wall time
    and make the window untestable without really sleeping for half an hour."""
    if now is None:
        return now_iso()
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def _meaning_key(evidence_key: str, class_sig: str) -> str:
    """The identity a cooldown is scoped to: this report AND what it currently says.

    Keying on the report alone (v8) bounded a stream of identical saves but not a report
    that FLAPS. `partial → blocked → partial → blocked` then read as "the classification
    changed" four times and woke the owner four times, for two decisions he had already
    been told. With the meaning IN the key, each decision owns its own window and a
    flap-back is what it actually is: the same thing, said again.
    """
    return f"{evidence_key}|{class_sig}"


def _owner_action_gate(evidence_key: str, *, class_sig: str, task_id: str,
                       stage_pointer: str, severity: str, now: float, conn=None) -> dict:
    """May this owner-action finding interrupt the owner AGAIN?

    Fail-closed toward being heard: every reason to believe the situation is genuinely
    different re-opens delivery immediately. Only the exact same meaning, inside the
    cooldown, is coalesced — and even then the event itself is still recorded.

    The one invariant worth stating outright: nothing is EVER suppressed unless the owner
    was told THAT SAME THING inside the cooldown. A new meaning always gets through.
    """
    conn, own = _c(conn)
    try:
        key = _meaning_key(evidence_key, class_sig)
        r = conn.execute(
            "SELECT class_sig,task_id,stage_pointer,severity,last_push_at,last_event_id,"
            "suppressed_count FROM work_evidence_push WHERE meaning_key=?",
            (key,)).fetchone()
        if not r:
            # Never delivered under this meaning. Distinguish the two ways that happens, so
            # the payload can say WHY the owner is being woken: a report nobody has raised
            # before, or one whose state genuinely moved (partial → blocked, blocked → done).
            sibling = conn.execute(
                "SELECT 1 FROM work_evidence_push WHERE evidence_key=? AND meaning_key<>? "
                "AND last_push_at IS NOT NULL LIMIT 1", (evidence_key, key)).fetchone()
            return {"deliver": True,
                    "reason": "classification_changed" if sibling else "first_time",
                    "suppressed_count": 0, "prior_event_id": 0}
        _prev_sig, prev_task, prev_stage, prev_sev, last_push_at, last_eid, sup = r
        sup = int(sup or 0)
        prior = int(last_eid or 0)
        if not last_push_at:
            # Recorded but never actually delivered (e.g. the per-scan bound held it back).
            # An undelivered finding has not interrupted anyone, so it is still owed one.
            return {"deliver": True, "reason": "not_yet_delivered",
                    "suppressed_count": sup, "prior_event_id": prior}
        # No classification check here: the classification IS the key, so reaching this row
        # already means the meaning matched. What follows are the ways the SAME meaning can
        # still describe a materially different situation.
        if (prev_task or "") != (task_id or ""):
            return {"deliver": True, "reason": "task_correlation_changed",
                    "suppressed_count": sup, "prior_event_id": prior}
        if (prev_stage or "") != (stage_pointer or ""):
            return {"deliver": True, "reason": "stage_pointer_moved",
                    "suppressed_count": sup, "prior_event_id": prior}
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(prev_sev or "", 0):
            return {"deliver": True, "reason": "severity_increased",
                    "suppressed_count": sup, "prior_event_id": prior}
        elapsed = now - _ts_epoch(last_push_at)
        if elapsed >= OWNER_ACTION_COOLDOWN_SECS:
            return {"deliver": True, "reason": "cooldown_expired",
                    "suppressed_count": sup, "prior_event_id": prior}
        return {"deliver": False, "reason": "coalesced_same_meaning",
                "suppressed_count": sup + 1, "prior_event_id": prior,
                "cooldown_remaining_secs": int(OWNER_ACTION_COOLDOWN_SECS - elapsed)}
    finally:
        if own:
            conn.close()


def _meaning_columns(meaning_key: str, *, evidence_key: str, project: str, target: str,
                     ref: str, kind: str, class_sig: str, task_id: str, stage_pointer: str,
                     severity: str, seen_at: str, conn) -> None:
    """Upsert what a finding MEANS and when it was last OBSERVED, never the delivery
    counters. `last_seen_at` moves on every repeat, delivered or coalesced, so the row
    answers "is this still happening?" independently of "was the owner told?"."""
    ts = now_iso()
    conn.execute(
        "INSERT INTO work_evidence_push(meaning_key,evidence_key,project,target,ref,kind,"
        "class_sig,task_id,stage_pointer,severity,last_push_at,last_event_id,"
        "suppressed_count,last_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,0,?,?) "
        "ON CONFLICT(meaning_key) DO UPDATE SET evidence_key=excluded.evidence_key,"
        "task_id=excluded.task_id,stage_pointer=excluded.stage_pointer,"
        "severity=excluded.severity,kind=excluded.kind,ref=excluded.ref,"
        "last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at",
        (meaning_key, evidence_key, project, target, ref, kind, class_sig, task_id,
         stage_pointer, severity, seen_at, ts))


def _owner_action_record(evidence_key: str, *, project: str, target: str, ref: str,
                         kind: str, class_sig: str, task_id: str, stage_pointer: str,
                         severity: str, delivered: bool, event_id: int,
                         suppressed_count: int, now: float = None, conn=None) -> None:
    """Persist the coalescing state. `last_push_at` advances ONLY on a real delivery, so a
    suppressed repeat can never extend the window that is suppressing it."""
    conn, own = _c(conn)
    try:
        key = _meaning_key(evidence_key, class_sig)
        _meaning_columns(key, evidence_key=evidence_key, project=project, target=target,
                         ref=ref, kind=kind, class_sig=class_sig, task_id=task_id,
                         stage_pointer=stage_pointer, severity=severity,
                         seen_at=_iso_at(now), conn=conn)
        if delivered:
            conn.execute(
                "UPDATE work_evidence_push SET last_push_at=?,last_event_id=?,"
                "suppressed_count=0 WHERE meaning_key=?",
                (_iso_at(now), int(event_id or 0), key))
        else:
            conn.execute(
                "UPDATE work_evidence_push SET suppressed_count=? WHERE meaning_key=?",
                (int(suppressed_count), key))
        conn.commit()
    finally:
        if own:
            conn.close()


def completion_scope(text: str) -> tuple:
    """The slice of a report that speaks for its CURRENT completeness, plus why.

    The append-only log is the failure this exists for. `classify_report` used to regex the
    WHOLE document, so a 297 KB narrative log that MENTIONS "DONE", "NOT STARTED" and
    "BLOCKED" across a thousand historical notes reported all three as live claims — and did
    so permanently, by construction, because an append-only file can never stop matching.
    Measured on 2026-08-30: 51% of a week's `work_stopped_incomplete` events came from two
    such logs, and the owner cannot tell those from a real stop.

    Structured beats prose: an explicit `Status:`/`Outcome:` declaration is kept wherever it
    sits, because it is a declaration. Failing that, the trailing window is used — what a
    report says LAST is what it currently claims. Short reports are read whole, unchanged.

    The narrowing is stated plainly rather than hidden: a one-off "BLOCKED ON x" written
    only in the middle of a long report is no longer seen. Declaring it in a status line
    keeps it visible, which is the behaviour worth having.
    """
    if len(text) <= LONG_REPORT_BYTES:
        return text, "whole_report"
    tail = text[-LONG_REPORT_BYTES:]
    cut = tail.find("\n")
    if cut >= 0:
        tail = tail[cut + 1:]            # never start mid-line
    decls = _STATUS_DECL_RE.findall(text)
    if decls:
        return "\n".join(decls) + "\n" + tail, "status_declarations+tail"
    return tail, "tail"


def classify_report(text: str) -> dict:
    """What does this report say about its own completeness?

    Reads the report's own words rather than guessing from file changes. A document that
    claims both `DONE` and `NOT STARTED` is the important case: it is a partial delivery,
    which is precisely what nobody was told about — but only when both claims are CURRENT,
    which is what `completion_scope` establishes.
    """
    scope, basis = completion_scope(text)
    done = bool(_DONE_RE.search(scope))
    not_started = bool(_NOT_STARTED_RE.search(scope))
    audit_only = bool(_AUDIT_ONLY_RE.search(scope))
    blocked = bool(_BLOCKED_RE.search(scope))
    unverified = bool(_UNVERIFIED_RE.search(scope))
    incomplete = not_started or audit_only or blocked
    return {
        "done": done, "not_started": not_started, "audit_only": audit_only,
        "blocked": blocked, "unverified": unverified,
        "partial": bool(done and incomplete),
        "incomplete": incomplete,
        # auditable: which part of the document these markers came from
        "scope_basis": basis,
    }


def _report_headline(text: str, cls: dict) -> str:
    """A factual one-liner: the report's title plus the claims that make it news."""
    title = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip()
            break
    flags = [k for k in ("done", "not_started", "audit_only", "blocked", "unverified")
             if cls.get(k)]
    return f"{title or 'report'} [{', '.join(flags) or 'no completion markers'}]"[:300]


def _iter_reports(root: str) -> list:
    """Candidate reports, NEWEST FIRST.

    Order matters: the per-scan cap exists to bound work, and a cap applied to an
    alphabetical listing quietly drops the most recent report — which is the only one that
    could be news. `/opt/mess` has 40+ reports, and today's was alphabetically last.
    """
    found = []
    for d in REPORT_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            if not name.endswith(REPORT_SUFFIXES):
                continue
            path = os.path.join(base, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > MAX_REPORT_BYTES:
                continue
            found.append((st.st_mtime, path))
    found.sort(reverse=True)
    return [p for _, p in found]


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _git_head(root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _git_new_commits(root: str, since: str) -> list:
    """Commits reachable from HEAD that were not there at the last scan. Read-only."""
    if not since:
        return []
    try:
        r = subprocess.run(["git", "-C", root, "log", "--oneline", "--no-decorate",
                            f"{since}..HEAD"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()][:MAX_COMMITS_REPORTED]
    except Exception:  # noqa: BLE001
        return []


def _projects_from_config() -> dict:
    """Targets Owner OS already governs, with their checkout path. Reuses the existing
    registry rather than introducing a second source of truth."""
    try:
        from core import continuation_governor as cg
        cfg = cg.load_config()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for target, entry in (cfg or {}).items():
        cwd = (entry or {}).get("cwd") or ""
        if cwd:
            out[target] = {"cwd": cwd, "project": (entry or {}).get("project") or target}
    return out


def _agent_state(target: str) -> str:
    try:
        from core import agent_control as ac
        return (ac.agent_status(target) or {}).get("state") or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _open_task(target: str, conn=None) -> Optional[dict]:
    try:
        from core import os_task_queue as q
        return q.active_task(target, conn=conn)
    except Exception:  # noqa: BLE001
        return None


def scan(projects: Optional[dict] = None, *, emit_fn: Callable = None,
         state_fn: Callable = None, pointer_fn: Callable = None, now: float = None,
         conn=None) -> dict:
    """One evidence pass over every governed project. Returns what it found and emitted.

    `projects` maps target → {cwd, project}; `emit_fn`, `state_fn` and `pointer_fn` are
    injectable so the scan is testable without a live pane or a real registry. `now`
    overrides the clock so the owner-action cooldown can be exercised without sleeping.
    """
    import time
    emit_fn = emit_fn or cto.emit
    state_fn = state_fn or _agent_state
    pointer_fn = pointer_fn or _stage_pointer
    projects = projects if projects is not None else _projects_from_config()
    emitted, skipped = [], []
    backfilled = 0
    now = time.time() if now is None else float(now)

    for target, meta in (projects or {}).items():
        root = meta.get("cwd") or ""
        project = meta.get("project") or target
        if not root or not os.path.isdir(root):
            skipped.append({"target": target, "reason": "project path unreadable",
                            "cwd": root})
            continue

        state = state_fn(target)
        stage_pointer = pointer_fn(target)
        open_task = _open_task(target, conn=conn)
        known = _project_known(project, conn=conn)
        cold_budget = COLD_START_MAX_REPORTS if not known else 10 ** 6
        # One wake per scan at most: the first owner-action finding pushes, the rest are
        # inbox-only. Every event still reaches the CTO inbox — what is bounded is how many
        # times a single sweep can interrupt the owner.
        pushed_this_scan = False

        # ── reports (newest first, so the cap can never hide today's work) ──
        candidates = _iter_reports(root)
        if len(candidates) > MAX_REPORTS_PER_SCAN:
            skipped.append({"target": target, "reason": "report cap reached (oldest not read)",
                            "not_read": len(candidates) - MAX_REPORTS_PER_SCAN})
        for path in candidates[:MAX_REPORTS_PER_SCAN]:
            text = _read(path)
            if text is None:
                skipped.append({"target": target, "reason": "report unreadable", "ref": path})
                continue
            fp = _sha(text)
            ev_id = f"report:{project}:{os.path.relpath(path, root)}"
            if _seen(ev_id, fp, conn=conn):
                continue
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                age = 0.0
            if not known and (age > BACKFILL_WINDOW_SECS or cold_budget <= 0):
                # first sight of this project, and this report predates the window: it is
                # history. Recorded as seen so it never announces itself later, counted so
                # the suppression is visible rather than silent.
                _record(ev_id, project=project, target=target, kind="backfill", ref=path,
                        fingerprint=fp, conn=conn)
                backfilled += 1
                continue
            cold_budget -= 1
            cls = classify_report(text)
            headline = _report_headline(text, cls)
            first_time = True
            # A report that says nothing about completion is still news the first time it
            # appears, but never again unless its content changes.
            kind = EVENT_PARTIAL if cls["partial"] else EVENT_REPORT
            severity = "high" if cls["partial"] else "info"
            owner_action = bool(cls["partial"])
            summary = (f"{target}: {'partial completion' if cls['partial'] else 'new report'} "
                       f"— {headline}")
            rel = os.path.relpath(path, root)
            task_id = (open_task or {}).get("id", "")
            class_sig = _class_sig(kind, cls)
            payload = {"report": rel, "markers": cls, "headline": headline,
                       "agent_state": state, "open_task": task_id,
                       "stage_pointer_moved": False}
            # The owner is interrupted for MEANING, not for bytes: the window is scoped to
            # this report AND what it currently says, so a re-save is silent while a genuine
            # change of state is heard at once.
            gate = None
            deliver = False
            if owner_action:
                gate = _owner_action_gate(
                    ev_id, class_sig=class_sig, task_id=task_id,
                    stage_pointer=stage_pointer, severity=severity, now=now, conn=conn)
                # Scoped per MEANING, not per sweep. A sweep-level bound would silently
                # drop the second distinct report's owner-action entirely: `_seen()` skips
                # an unchanged fingerprint on the next scan, so a finding held back once is
                # never reconsidered. Two different half-finished reports are two different
                # decisions and each gets its own window; the sweep bound below still keeps
                # a single sweep from sending more than one Telegram push.
                deliver = bool(gate["deliver"])
                if not deliver:
                    # Inbox-only. Severity and owner_action_required are BOTH dropped
                    # because `cto.emit` consults the wake bridge and the night-shift
                    # signal on those two fields alone — `push=False` silences Telegram
                    # and would still have woken someone.
                    payload.update({
                        "coalesced": True,
                        "coalesced_reason": gate["reason"],
                        "coalesced_with_event_id": gate["prior_event_id"],
                        "suppressed_count": gate["suppressed_count"],
                        "owner_action_suppressed": True,
                        "original_severity": severity,
                        "cooldown_secs": OWNER_ACTION_COOLDOWN_SECS,
                    })
                    if "cooldown_remaining_secs" in gate:
                        payload["cooldown_remaining_secs"] = gate["cooldown_remaining_secs"]
            else:
                payload["coalesced"] = False
            eff_severity = severity if (not owner_action or deliver) else "info"
            eff_owner_action = bool(owner_action and deliver)
            if owner_action and deliver:
                payload["owner_action_delivery_reason"] = gate["reason"]
                if gate["suppressed_count"]:
                    payload["suppressed_since_last_delivery"] = gate["suppressed_count"]
            # Owner-action findings still reach the wake bridge individually, but one sweep
            # sends at most one push through the outbox — the original bound, kept.
            wants_push = eff_owner_action or eff_severity in ("high", "critical")
            push = None if (wants_push and not pushed_this_scan) else False
            if wants_push:
                pushed_this_scan = True
            res = emit_fn("work_evidence", kind, project_id=project, agent_id=target,
                          severity=eff_severity, owner_action_required=eff_owner_action,
                          payload=payload,
                          action_taken=summary, evidence_ref=path, push=push,
                          dedup_key=f"we:{kind}:{ev_id}:{fp}",
                          dedup_window_secs=DEDUP_WINDOW_SECS, conn=conn)
            _record(ev_id, project=project, target=target, kind=kind, ref=path,
                    fingerprint=fp, event_id=(res or {}).get("event_id", 0), conn=conn)
            if owner_action:
                _owner_action_record(
                    ev_id, project=project, target=target, ref=rel, kind=kind,
                    class_sig=class_sig, task_id=task_id, stage_pointer=stage_pointer,
                    severity=severity, delivered=deliver,
                    event_id=(res or {}).get("event_id", 0),
                    suppressed_count=gate["suppressed_count"], now=now, conn=conn)
            # Informational reports need no row at all: with the meaning in the key, a later
            # partial cannot be measured against a stale signature — it simply has its own.
            emitted.append({"target": target, "kind": kind, "ref": path,
                            "event_id": (res or {}).get("event_id"), "markers": cls,
                            "first_time": first_time,
                            "owner_action_delivered": eff_owner_action,
                            "coalesced": bool(owner_action and not deliver)})

            # ── the case nobody was told about: work stopped half-done ─────
            # An agent that is idle while its own report says the requested work was not
            # started (or while its ledger task is still open) has REFUSED to continue,
            # which is a decision the owner must see — not a quiet non-event.
            if state in ("idle", "completed", "unknown") and (cls["incomplete"] or open_task):
                stop_id = f"stopped:{project}:{os.path.relpath(path, root)}"
                stop_fp = _sha(f"{fp}|{state}|{(open_task or {}).get('id','')}")
                if not _seen(stop_id, stop_fp, conn=conn):
                    reason = ("its own report says the requested implementation was not started"
                              if cls["not_started"] or cls["audit_only"] else
                              "its ledger task is still open" if open_task else
                              "the report records blocked/unverified work")
                    stop_sig = _class_sig(EVENT_STOPPED, cls, reason=f"{reason}|{state}")
                    gate2 = _owner_action_gate(
                        stop_id, class_sig=stop_sig, task_id=task_id,
                        stage_pointer=stage_pointer, severity="high", now=now, conn=conn)
                    deliver2 = bool(gate2["deliver"])
                    stop_payload = {"report": rel, "markers": cls, "agent_state": state,
                                    "open_task": task_id, "reason": reason}
                    if deliver2:
                        stop_payload["coalesced"] = False
                        stop_payload["owner_action_delivery_reason"] = gate2["reason"]
                        if gate2["suppressed_count"]:
                            stop_payload["suppressed_since_last_delivery"] = \
                                gate2["suppressed_count"]
                    else:
                        stop_payload.update({
                            "coalesced": True,
                            "coalesced_reason": gate2["reason"],
                            "coalesced_with_event_id": gate2["prior_event_id"],
                            "suppressed_count": gate2["suppressed_count"],
                            "owner_action_suppressed": True,
                            "original_severity": "high",
                            "cooldown_secs": OWNER_ACTION_COOLDOWN_SECS,
                        })
                        if "cooldown_remaining_secs" in gate2:
                            stop_payload["cooldown_remaining_secs"] = \
                                gate2["cooldown_remaining_secs"]
                    push2 = None if (deliver2 and not pushed_this_scan) else False
                    if deliver2:
                        pushed_this_scan = True
                    res2 = emit_fn("work_evidence", EVENT_STOPPED, project_id=project,
                                   agent_id=target,
                                   severity=("high" if deliver2 else "info"),
                                   owner_action_required=deliver2, push=push2,
                                   payload=stop_payload,
                                   action_taken=(f"{target} went {state} with work incomplete "
                                                 f"— {reason}"),
                                   evidence_ref=path,
                                   dedup_key=f"we:{EVENT_STOPPED}:{stop_id}:{stop_fp}",
                                   dedup_window_secs=DEDUP_WINDOW_SECS, conn=conn)
                    _record(stop_id, project=project, target=target, kind=EVENT_STOPPED,
                            ref=path, fingerprint=stop_fp,
                            event_id=(res2 or {}).get("event_id", 0), conn=conn)
                    _owner_action_record(
                        stop_id, project=project, target=target, ref=rel,
                        kind=EVENT_STOPPED, class_sig=stop_sig, task_id=task_id,
                        stage_pointer=stage_pointer, severity="high", delivered=deliver2,
                        event_id=(res2 or {}).get("event_id", 0),
                        suppressed_count=gate2["suppressed_count"], now=now, conn=conn)
                    emitted.append({"target": target, "kind": EVENT_STOPPED, "ref": path,
                                    "event_id": (res2 or {}).get("event_id"),
                                    "reason": reason,
                                    "owner_action_delivered": deliver2,
                                    "coalesced": not deliver2})

        # ── commits ────────────────────────────────────────────────────────
        head = _git_head(root)
        if head:
            cur_id = f"head:{project}"
            prev = _head_cursor(cur_id, conn=conn)
            if prev and prev != head:
                lines = _git_new_commits(root, prev)
                if lines:
                    res = emit_fn("work_evidence", EVENT_COMMITS, project_id=project,
                                  agent_id=target, severity="info",
                                  owner_action_required=False,
                                  payload={"head": head, "previous_head": prev,
                                           "commits": lines, "agent_state": state,
                                           "stage_pointer_moved": False},
                                  action_taken=(f"{target}: {len(lines)} new commit(s) with no "
                                                f"stage-pointer change — {lines[0][:120]}"),
                                  dedup_key=f"we:{EVENT_COMMITS}:{project}:{head}",
                                  dedup_window_secs=DEDUP_WINDOW_SECS, conn=conn)
                    emitted.append({"target": target, "kind": EVENT_COMMITS, "ref": head,
                                    "event_id": (res or {}).get("event_id"),
                                    "commits": lines})
            _record(cur_id, project=project, target=target, kind="head_cursor", ref=root,
                    fingerprint=head, conn=conn)

    return {"emitted": emitted, "emitted_count": len(emitted), "skipped": skipped,
            "backfilled": backfilled, "projects_scanned": len(projects or {})}


def _project_known(project: str, conn=None) -> bool:
    """Has this project been observed before? Decides news from history on a cold start."""
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT 1 FROM work_evidence WHERE project=? LIMIT 1",
                         (project,)).fetchone()
        return bool(r)
    finally:
        if own:
            conn.close()


def _head_cursor(cur_id: str, conn=None) -> str:
    conn, own = _c(conn)
    try:
        r = conn.execute("SELECT fingerprint FROM work_evidence WHERE id=?", (cur_id,)).fetchone()
        return r[0] if r else ""
    finally:
        if own:
            conn.close()
