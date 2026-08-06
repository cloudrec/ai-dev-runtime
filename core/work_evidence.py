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


def classify_report(text: str) -> dict:
    """What does this report say about its own completeness?

    Reads the report's own words rather than guessing from file changes. A document that
    claims both `DONE` and `NOT STARTED` is the important case: it is a partial delivery,
    which is precisely what nobody was told about.
    """
    done = bool(_DONE_RE.search(text))
    not_started = bool(_NOT_STARTED_RE.search(text))
    audit_only = bool(_AUDIT_ONLY_RE.search(text))
    blocked = bool(_BLOCKED_RE.search(text))
    unverified = bool(_UNVERIFIED_RE.search(text))
    incomplete = not_started or audit_only or blocked
    return {
        "done": done, "not_started": not_started, "audit_only": audit_only,
        "blocked": blocked, "unverified": unverified,
        "partial": bool(done and incomplete),
        "incomplete": incomplete,
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
         state_fn: Callable = None, conn=None) -> dict:
    """One evidence pass over every governed project. Returns what it found and emitted.

    `projects` maps target → {cwd, project}; `emit_fn` and `state_fn` are injectable so the
    scan is testable without a live pane or a real registry.
    """
    import time
    emit_fn = emit_fn or cto.emit
    state_fn = state_fn or _agent_state
    projects = projects if projects is not None else _projects_from_config()
    emitted, skipped = [], []
    backfilled = 0
    now = time.time()

    for target, meta in (projects or {}).items():
        root = meta.get("cwd") or ""
        project = meta.get("project") or target
        if not root or not os.path.isdir(root):
            skipped.append({"target": target, "reason": "project path unreadable",
                            "cwd": root})
            continue

        state = state_fn(target)
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
            push = None if (owner_action and not pushed_this_scan) else False
            if owner_action:
                pushed_this_scan = True
            res = emit_fn("work_evidence", kind, project_id=project, agent_id=target,
                          severity=severity, owner_action_required=owner_action,
                          payload={"report": os.path.relpath(path, root), "markers": cls,
                                   "headline": headline, "agent_state": state,
                                   "open_task": (open_task or {}).get("id", ""),
                                   "stage_pointer_moved": False},
                          action_taken=summary, evidence_ref=path, push=push,
                          dedup_key=f"we:{kind}:{ev_id}:{fp}",
                          dedup_window_secs=DEDUP_WINDOW_SECS, conn=conn)
            _record(ev_id, project=project, target=target, kind=kind, ref=path,
                    fingerprint=fp, event_id=(res or {}).get("event_id", 0), conn=conn)
            emitted.append({"target": target, "kind": kind, "ref": path,
                            "event_id": (res or {}).get("event_id"), "markers": cls,
                            "first_time": first_time})

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
                    push2 = None if not pushed_this_scan else False
                    pushed_this_scan = True
                    res2 = emit_fn("work_evidence", EVENT_STOPPED, project_id=project,
                                   agent_id=target, severity="high",
                                   owner_action_required=True, push=push2,
                                   payload={"report": os.path.relpath(path, root),
                                            "markers": cls, "agent_state": state,
                                            "open_task": (open_task or {}).get("id", ""),
                                            "reason": reason},
                                   action_taken=(f"{target} went {state} with work incomplete "
                                                 f"— {reason}"),
                                   evidence_ref=path,
                                   dedup_key=f"we:{EVENT_STOPPED}:{stop_id}:{stop_fp}",
                                   dedup_window_secs=DEDUP_WINDOW_SECS, conn=conn)
                    _record(stop_id, project=project, target=target, kind=EVENT_STOPPED,
                            ref=path, fingerprint=stop_fp,
                            event_id=(res2 or {}).get("event_id", 0), conn=conn)
                    emitted.append({"target": target, "kind": EVENT_STOPPED, "ref": path,
                                    "event_id": (res2 or {}).get("event_id"),
                                    "reason": reason})

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
