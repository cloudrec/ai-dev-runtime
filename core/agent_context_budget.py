"""Context-budget control for EXISTING Claude agents (Commander hardening).

NOT a product feature or dashboard — an internal orchestrator mechanism that keeps
long-running tmux agents cheap and reliable by rotating their context BEFORE input
tokens pile up. It reads the context signal each agent already prints in its pane,
classifies it, and — only at a proven-safe task boundary — performs a verified
context rotation (compact handoff → /clear → resume) on the SAME existing agent. It
never creates or stops an agent and never invents a new task.

Cost-optimized policy (percent of the context window used; all thresholds are
configurable per project/model, these are the defaults):
  <45      → ok         : do nothing
  45–55    → checkpoint : prepare/update a COMPACT handoff at the next natural boundary
  55–65    → rotate*    : rotate (handoff → /clear) at a safe boundary IFF substantial
                          work remains (otherwise finish the work instead)
  >=65     → rotate     : rotate at the FIRST safe boundary

Finish-soon suppression: if the task will plausibly finish within 1–2 commands or a
single test/build run, FINISH it instead of clearing (a rotation there would waste
the very tokens it tries to save).

Hard safety rules:
  * NEVER /clear while a command / edit / test / build / migration / deploy /
    approval prompt / external action is active (the safe-boundary gate).
  * NEVER rotate an idle *completed* agent — nothing to resume.
  * Write AND verify a fresh handoff BEFORE /clear; abort if missing/stale.
  * Cooldown + changed-hash: a rotation may repeat in an exceptionally long phase,
    but only after the cooldown AND only if the handoff content actually changed —
    so it can never loop on an unchanged state.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from core import agent_control as ac

# ── default tunables (overridable per project/model via cfg['context_budget']) ─
_DEFAULTS = {
    "window_tokens": int(os.getenv("CONTEXT_WINDOW_TOKENS", "1000000")),
    "checkpoint_pct": float(os.getenv("CONTEXT_CHECKPOINT_PCT", "45")),
    "rotate_substantial_pct": float(os.getenv("CONTEXT_ROTATE_SUBSTANTIAL_PCT", "55")),
    "rotate_pct": float(os.getenv("CONTEXT_ROTATE_PCT", "65")),
    "cooldown_secs": int(os.getenv("CONTEXT_ROTATE_COOLDOWN_SECS", "2700")),   # 45 min
    "handoff_fresh_secs": int(os.getenv("CONTEXT_HANDOFF_FRESH_SECS", "900")),
    "handoff_max_bytes": int(os.getenv("CONTEXT_HANDOFF_MAX_BYTES", "2048")),  # ~2 KB
    # An agent must be STABLY at rest — at rest last sweep AND no fresh activity for
    # this many seconds — before any /clear. A single-instant idle read of an agent
    # that is actually mid-turn must never trigger a rotation (2026-07-22 regression:
    # a momentary lull on the first post-restart sweep cleared a working agent).
    "min_idle_dwell_secs": int(os.getenv("CONTEXT_MIN_IDLE_DWELL_SECS", "150")),
}
HANDOFF_FILENAME = "CONTEXT_HANDOFF.md"

# Active-execution markers: RELIABLE live-run evidence only (aligned with
# classify_state) — a live "esc to interrupt", a spinner WITH a running timer, or a
# streaming token counter. A bare ✻/✽ glyph or a past-tense "Crunched/Baked for …"
# is NOT active (it appears at rest) and must not falsely block a safe rotation.
# The explicit run phrases add belt-and-suspenders for the owner's exclusion list;
# when any of these truly run, classify_state already reports `working` (not idle).
_ACTIVE_EXEC_RE = re.compile(
    r"(esc to interrupt|…\s*\(\d+\s*m?s\b|[↑↓]\s*[\d.]+\s*k?\s*tokens\b|\bcompacting\b|"
    r"\btool call\b|\bexecuting\b|\bexploring\b|\bsub-?agent\b|\bdispatching\b|"
    r"\bmigrat(?:e|ing)\b|\bdeploy(?:ing)?\b|\bbuilding\b|\binstalling\b|"
    r"\bprovisioning\b|\bupgrading\b|\brestarting\b|running (?:the )?(?:tests?|build|migration))",
    re.I)

# Finish-soon cues: the agent is one or two steps from done → finish, don't clear.
_FINISH_SOON_RE = re.compile(
    r"(final commit|last step|one (?:more|last)|almost done|wrapping up|ready to commit|"
    r"about to (?:commit|finish)|just need to|running the (?:tests?|build)|one (?:test|build) run)",
    re.I)

# Context signal printed in the pane (several Claude Code footer forms).
_PCT_LEFT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:context\s+)?(?:left|remaining|until auto-?compact)", re.I)
_PCT_USED_RE = re.compile(
    r"(?:context(?:\s+window)?\s+(?:used|full)[:\s]+(\d{1,3}(?:\.\d+)?)\s*%|"
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:context\s+)?(?:used|full))", re.I)
# `/clear to save N tokens`, or a raw context total `N k/M tokens` / `N.NN MB`
# (MB → assume ~4 chars/token vs the window is unreliable, so only the token forms
# yield a percentage; MB is treated as "near/over" the compact bound → 90%).
_TOKENS_RE = re.compile(r"(?:/clear to save|context[:\s]+)\s*([\d.]+)\s*([kmKM])?\s*tokens", re.I)
_MB_CONTEXT_RE = re.compile(r"context[^\n]{0,20}?([\d.]+)\s*MB|([\d.]+)\s*MB[^\n]{0,12}context", re.I)


def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def thresholds(cfg: dict) -> dict:
    """Resolve per-project/model thresholds, defaulting to the module defaults.
    cfg may carry `context_budget: {...}` and/or `model` (for a model-specific
    window)."""
    t = dict(_DEFAULTS)
    override = (cfg or {}).get("context_budget") or {}
    for k in t:
        if override.get(k) is not None:
            t[k] = type(t[k])(override[k])
    # a model-specific context window, if the config declares one.
    models = (cfg or {}).get("context_windows") or {}
    model = (cfg or {}).get("model")
    if model and models.get(model):
        t["window_tokens"] = int(models[model])
    return t


# ── detection ───────────────────────────────────────────────────────────────
def detect_context_pct(tail: str, window_tokens: int = _DEFAULTS["window_tokens"]) -> Optional[float]:
    """Percent of the context window USED, from the agent's own pane. None when no
    signal is present (then the policy does nothing — never guesses)."""
    if not tail:
        return None
    m = _PCT_USED_RE.search(tail)
    if m:
        return _clamp(float(m.group(1) or m.group(2)))
    m = _PCT_LEFT_RE.search(tail)
    if m:
        return _clamp(100.0 - float(m.group(1)))
    m = _TOKENS_RE.search(tail)
    if m and window_tokens > 0:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        tokens = val * 1000 if unit == "k" else val * 1_000_000 if unit == "m" else val
        return _clamp(100.0 * tokens / window_tokens)
    # A raw conversation size in MB near/over the auto-compact bound → treat as
    # high (>=rotate). Claude prints this only when context is large.
    m = _MB_CONTEXT_RE.search(tail)
    if m:
        return 90.0
    return None


def _clamp(p: float) -> float:
    return max(0.0, min(100.0, round(p, 1)))


def classify(pct: Optional[float], t: dict) -> str:
    """ok | checkpoint | rotate_substantial | rotate | unknown."""
    if pct is None:
        return "unknown"
    if pct < t["checkpoint_pct"]:
        return "ok"
    if pct < t["rotate_substantial_pct"]:
        return "checkpoint"
    if pct < t["rotate_pct"]:
        return "rotate_substantial"
    return "rotate"


# ── safe-boundary + finish-soon gates ───────────────────────────────────────
def at_safe_boundary(agent: dict, state: str) -> bool:
    """True only when it is provably safe to /clear: the agent is idle between
    steps of an ONGOING phase — no active command/edit/test/build, no approval
    prompt, no external action, and not an idle *completed* agent."""
    tail = agent.get("recent_activity") or agent.get("_tail") or ""
    if _ACTIVE_EXEC_RE.search(tail):
        return False
    return state == "idle"


def stably_idle(rec: dict, prev: dict, min_dwell: int) -> bool:
    """True only when the agent is SETTLED, not momentarily quiet: it was already
    at rest on the PREVIOUS sweep (so a single-instant idle read of a mid-turn
    agent cannot trigger anything), and no fresh activity has been observed for at
    least `min_dwell` seconds. A brand-new record (no prior sweep) is never
    'settled' — the first post-restart sweep must not rotate."""
    prev_state = (prev or {}).get("state")
    if prev_state not in ("idle", "completed"):
        return False
    lfa = rec.get("last_fresh_activity_ts") or (prev or {}).get("last_fresh_activity_ts")
    if not lfa:
        return False
    try:
        return (_now_ts() - float(lfa)) >= min_dwell
    except (TypeError, ValueError):
        return False


def finish_soon(agent: dict, rec: dict) -> bool:
    """The task will plausibly finish within 1–2 commands or one test/build run —
    finishing is cheaper than rotating. True when a completion report was just
    written, or the pane shows an explicit wrap-up cue."""
    if rec.get("completion_evidence"):
        return True
    tail = agent.get("recent_activity") or agent.get("_tail") or ""
    return bool(_FINISH_SOON_RE.search(tail))


def _next_phase_title(cfg: dict) -> Optional[str]:
    phases = (cfg or {}).get("phases") or []
    if len(phases) > 1:
        return phases[1].get("title") or phases[1].get("id")
    return None


def remaining_approved_subphase(cfg: dict) -> Optional[dict]:
    """The EXACT owner-approved remaining work, or None. Two sources, both exact
    (never a bare config placeholder — the 2026-07-22 false-resume root cause):
      1. `active_task_text` — the CURRENT approved task to CONTINUE after a /clear
         (e.g. Part D, in progress); preferred.
      2. an owner-approved NEXT phase (`approved_task_text`, merged from get_phase_text).
    Cleared by the owner when the task completes → then classification falls to
    task_completed_* and nothing is resumed."""
    active = ((cfg or {}).get("active_task_text") or "").strip()
    if active:
        return {"id": (cfg.get("active_task_id") or "active"),
                "title": cfg.get("active_task_title") or "active approved task", "text": active}
    phases = (cfg or {}).get("phases") or []
    if len(phases) > 1:
        txt = (phases[1].get("approved_task_text") or "").strip()
        if txt:
            return {"id": phases[1].get("id"), "title": phases[1].get("title"), "text": txt}
    return None


# The report/pane says the task is done but blocked on an OWNER/external action
# (credentials, publish, verification) — surface it; never invent a next subphase.
_EXTERNAL_WAIT_RE = re.compile(
    r"(waiting (?:for|on) (?:owner|you|external|credential|approval|verification|publish)|"
    r"external action required|needs? (?:owner|external|credential|publish|deploy) |"
    r"awaiting (?:owner|credentials?|approval|verification)|"
    r"blocked on (?:owner|external|credential))", re.I)


def completion_class(rec: dict, cfg: dict, tail: str) -> tuple[str, Optional[dict]]:
    """Distinguish three completion states so a completed task is never falsely
    resumed:
      * work_remaining               → exact owner-approved remaining subphase exists
      * task_completed_waiting_external → done, blocked on an owner/external action
      * task_completed_no_remaining_work → done, nothing left to do
    Returns (state, remaining_subphase_or_None)."""
    nxt = remaining_approved_subphase(cfg)
    if nxt:
        return "work_remaining", nxt
    if _EXTERNAL_WAIT_RE.search(tail or ""):
        return "task_completed_waiting_external", None
    return "task_completed_no_remaining_work", None


# A `cd <path>` / `git -C <path>` / docker `-w <path>` inside the pane's recent
# commands — the agent may be operating on ANOTHER project than its tmux session.
_CWD_CHANGE_RE = re.compile(r"(?:\bcd\s+|\bgit\s+-C\s+|--workdir[=\s]+|-w\s+)(/[^\s'\";|&]+)")
# Active-thinking / running markers that must override a stale state=idle.
_THINKING_RE = re.compile(r"(\bthinking\b|…\s*\(\d+\s*m?s|[↑↓]\s*[\d.]+\s*k?\s*tokens|esc to interrupt|"
                          r"\b\w+ing…|Shenaniganing|Combobulating|Cerebrating|Ruminating)", re.I)


def resolve_active_project(agent: dict, rec: dict, cfg: dict, tail: str) -> dict:
    """Authoritative project/task identity — NOT merely the tmux session name or a
    stale pane cwd. Prefers the recorded active approved task; detects a `cd`/
    `git -C`/docker-workdir into a DIFFERENT path in the recent commands and flags
    `cross_project_task` rather than mislabelling."""
    canonical = cfg.get("project") or rec.get("project")
    root = os.path.realpath(cfg.get("root") or "") if cfg.get("root") else ""
    out = {"project": canonical, "active_task_id": cfg.get("active_task_id"),
           "cross_project": False, "command_path": None}
    paths = _CWD_CHANGE_RE.findall(tail or "")
    for p in paths:
        rp = os.path.realpath(p)
        if root and rp != root and not rp.startswith(root + os.sep):
            out["command_path"] = p
            out["project"] = "cross_project_task"
            out["cross_project"] = True
            break
    return out


def detect_surfaceable_event(agent: dict, rec: dict, cfg: dict, tail: str,
                             prev: Optional[dict] = None) -> Optional[dict]:
    """The checkpoint / completion event to SURFACE — computed on DETECTION,
    independent of the context / dwell / dispatch gates for ROTATION, but STILL
    guarded so a false completion of an actively-working agent can never be emitted
    (the 2026-07-22 job false-completion):
      * recent ACTIVE evidence (running command / thinking / spinner / streaming
        tokens) overrides a stale state=idle — never surface;
      * require STABLE idle (at rest last sweep + a dwell) so a momentary lull of a
        working agent never surfaces;
      * a completed event additionally needs genuine completion evidence.
    Project identity comes from the active-task record / command context."""
    # TRACKED tasks only: the Commander surfaces checkpoint/completion events ONLY
    # for an agent with a recorded canonical task (`active_task_id`). An agent doing
    # untracked / ad-hoc / cross-project work (e.g. `job` reusing its pane for a
    # clients-help-landing CSS task) never gets a completion event tied to its stale
    # session project (the 2026-07-22 repeated `job → jobhunter-ai` false completions).
    active_id = cfg.get("active_task_id")
    _next = remaining_approved_subphase(cfg)
    if not (active_id or _next):
        return None                                   # untracked agent → no completion event
    active_id = active_id or (_next or {}).get("id")
    state = rec.get("state")
    # Active-evidence override — a running command / thinking marker (or state=working)
    # beats everything: never surface while the agent is executing.
    if state == "working" or _ACTIVE_EXEC_RE.search(tail or "") or _THINKING_RE.search(tail or ""):
        return None
    ident = resolve_active_project(agent, rec, cfg, tail)
    base = {"project": ident["project"], "canonical_task_id": active_id,
            "active_task_id": active_id, "cross_project": ident["cross_project"],
            "command_path": ident["command_path"], "context_pct": detect_context_pct(tail)}

    # OWNER-DECLARED completion of the tracked task (authoritative). Surface exactly
    # once regardless of the momentary rest state (idle / completed / waiting).
    if cfg.get("active_task_completed"):
        et = ("task_completed_waiting_external_action" if cfg.get("external_inputs_only")
              else "task_completed_no_remaining_work")
        return {**base, "event_type": et,
                "completion_class": ("task_completed_waiting_external" if cfg.get("external_inputs_only")
                                     else "task_completed_no_remaining_work"),
                "result": cfg.get("active_task_result") or "completed",
                "dedup_key": f"complete:{active_id}"}

    # In-progress checkpoint: at-rest state + stable idle + a fresh handoff (a
    # momentary lull of a working agent never surfaces).
    if state not in ("idle", "completed", "externally_blocked"):
        return None
    if not at_safe_boundary(agent, "idle"):
        return None
    if not stably_idle(rec, prev or {}, _DEFAULTS["min_idle_dwell_secs"]):
        return None
    root = cfg.get("root") or agent.get("claude_cwd") or agent.get("cwd") or ""
    handoff = resolve_handoff_path(root, cfg, {}, _DEFAULTS["handoff_fresh_secs"])
    if not (rec.get("completion_evidence") or handoff):
        return None
    cls, remaining = completion_class(rec, cfg, tail)
    if cls == "work_remaining":
        return {**base, "event_type": "checkpoint_completed_work_remaining",
                "completion_class": cls, "remaining": (remaining or {}).get("text"),
                "remaining_id": (remaining or {}).get("id"), "handoff_path": handoff,
                "dedup_key": (remaining or {}).get("id", "active")}
    return None


def resolve_handoff_path(root: str, cfg: dict, rotation_row: dict, max_age: int) -> Optional[str]:
    """The AUTHORITATIVE handoff path — NEVER a hardcoded root/CONTEXT_HANDOFF.md.
    Priority: explicit per-project config → the path persisted from a prior
    rotation → the agent's own fresh reports/ handoff → any discovered fresh
    handoff. None when nothing exists (caller must not resume without one)."""
    p = (cfg or {}).get("handoff_path")
    if p and os.path.exists(p):
        return p
    p = (rotation_row or {}).get("handoff_path")
    if p and os.path.exists(p):
        return p
    return agent_authored_handoff(root, max_age) or existing_fresh_handoff(root, max_age)


def substantial_work_remains(rec: dict, cfg: dict) -> bool:
    """Substantial work remains when there is a queued next approved phase or the
    current phase is not the final one — used to gate the 55–65% tier."""
    if rec.get("approved_next_task"):
        return True
    phases = (cfg or {}).get("phases") or []
    return len(phases) > 1


# ── persistence (cooldown / changed-hash, own table in the shared db) ─────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_context_rotation (
        agent_key TEXT PRIMARY KEY, phase TEXT, pct REAL, stage TEXT,
        handoff_path TEXT, handoff_hash TEXT, rotated_at REAL, updated_at TEXT)""")
    conn.commit()
    return conn


def _rotation_row(agent_key: str) -> dict:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT agent_key,phase,pct,stage,handoff_path,handoff_hash,rotated_at,updated_at "
            "FROM agent_context_rotation WHERE agent_key=?", (agent_key,)).fetchone()
        if not row:
            return {}
        cols = ("agent_key", "phase", "pct", "stage", "handoff_path", "handoff_hash",
                "rotated_at", "updated_at")
        return dict(zip(cols, row))
    finally:
        conn.close()


def _save_rotation(agent_key: str, phase: str, pct: float, stage: str, handoff_path: Optional[str],
                   handoff_hash: Optional[str], rotated_at: Optional[float]) -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO agent_context_rotation"
            "(agent_key,phase,pct,stage,handoff_path,handoff_hash,rotated_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(agent_key) DO UPDATE SET "
            "phase=excluded.phase,pct=excluded.pct,stage=excluded.stage,"
            "handoff_path=excluded.handoff_path,handoff_hash=excluded.handoff_hash,"
            "rotated_at=excluded.rotated_at,updated_at=excluded.updated_at",
            (agent_key, phase, pct, stage, handoff_path, handoff_hash, rotated_at, _now_iso()))
        conn.commit()
    finally:
        conn.close()


def can_rotate_again(agent_key: str, new_hash: str, cooldown_secs: int) -> bool:
    """A repeat rotation is allowed only after the cooldown AND only if the handoff
    content changed since the last rotation — so it can never loop on an unchanged
    state. The first rotation for an agent is always allowed."""
    row = _rotation_row(agent_key)
    if not row or not row.get("rotated_at"):
        return True
    if row.get("handoff_hash") == new_hash:
        return False                                   # unchanged state → would loop
    return (_now_ts() - float(row["rotated_at"])) >= cooldown_secs


# ── compact, delta-based handoff document ───────────────────────────────────
def _git(root: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", root, *args], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=8)
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def git_facts(root: str) -> dict:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "(unknown)"
    commit = _git(root, "rev-parse", "--short", "HEAD") or "(unknown)"
    dirty_raw = _git(root, "status", "--porcelain")
    dirty = [ln[3:] for ln in dirty_raw.splitlines()] if dirty_raw else []
    return {"branch": branch, "commit": commit, "dirty": dirty}


def build_handoff(rec: dict, cfg: dict, root: str, pct: float, max_bytes: int) -> tuple[str, str]:
    """Return (content, stable_hash). COMPACT (target ~1–2 KB) and delta-based:
    it references the report and files instead of copying any conversation history.
    The hash covers the factual body only (not the timestamp) so an unchanged state
    produces an unchanged hash — the anti-loop guard depends on this."""
    g = git_facts(root)
    project = rec.get("project") or cfg.get("project") or "(unknown)"
    approved = cfg.get("approved_goal") or rec.get("approved_goal") or "(no approved task on record)"
    phase = rec.get("phase") or "(none)"
    next_task = rec.get("approved_next_task") or rec.get("current_task")
    next_cmd = (f"Continue approved task: {approved}"
                + (f"; next approved phase: {next_task}" if next_task else ""))
    blocker = rec.get("blocker_text") or rec.get("blocker_category") or "none"
    pending = (rec.get("decision") or {}).get("action") if isinstance(rec.get("decision"), dict) else None
    report = rec.get("report_path") or "(see project reports/)"
    # dirty files as references (names only) — compact, capped.
    dirty = g["dirty"][:15]
    dirty_line = ", ".join(dirty) + ("" if len(g["dirty"]) <= 15 else f", …(+{len(g['dirty']) - 15})")

    # Stable factual body (hashed). Delta/reference style — no history copied.
    body = "\n".join([
        f"- Project: {project}",
        f"- Approved task: {approved}",
        f"- Phase: {phase} · State: {rec.get('state')}",
        f"- Branch: {g['branch']} · Commit: {g['commit']}",
        f"- Report (authoritative completed-work log): {report}",
        f"- Dirty files (see git status for full): {dirty_line or '(clean)'}",
        f"- Tests: re-verify from report, then re-run the project test command "
        f"(not re-run during rotation).",
        f"- Blocker: {blocker}",
        f"- Pending decision: {pending or 'none'}",
        f"- NEXT (already approved — do NOT invent a task): {next_cmd}",
        f"- Rollback: git revert {g['commit']} on {g['branch']}; restart the affected service.",
        f"- Do NOT touch: files outside this project; uncommitted work you did not create; "
        f"held/external projects, credentials, payments, outreach, publishing.",
    ])
    stable_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    header = (f"# CONTEXT HANDOFF — {project}\n"
              f"_Rotation checkpoint at {_now_iso()} (~{pct:.0f}% context). Delta only; "
              f"read the report for full detail. NOT a completion._\n\n")
    content = header + body + "\n"
    if len(content.encode("utf-8")) > max_bytes:      # keep it compact
        content = content.encode("utf-8")[:max_bytes].decode("utf-8", "ignore") + "\n"
    return content, stable_hash


def write_handoff(root: str, content: str) -> Optional[str]:
    if not root or not os.path.isdir(root):
        return None
    path = os.path.join(root, HANDOFF_FILENAME)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path
    except OSError:
        return None


def handoff_is_fresh(path: Optional[str], max_age: int) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        return (_now_ts() - os.path.getmtime(path)) <= max_age
    except OSError:
        return False


def _handoff_candidates(root: str) -> list[str]:
    """Where a coherent-checkpoint handoff may live: the module's own file at the
    project root, or one the AGENT wrote under reports/."""
    if not root:
        return []
    return [os.path.join(root, HANDOFF_FILENAME),
            os.path.join(root, "reports", HANDOFF_FILENAME)]


def existing_fresh_handoff(root: str, max_age: int) -> Optional[str]:
    """The freshest existing handoff (module's or the agent's own) within the
    freshness window — the 'coherent completed checkpoint' evidence."""
    fresh = [(os.path.getmtime(p), p) for p in _handoff_candidates(root)
             if os.path.exists(p) and (_now_ts() - os.path.getmtime(p)) <= max_age]
    return max(fresh)[1] if fresh else None


def agent_authored_handoff(root: str, max_age: int) -> Optional[str]:
    """The AGENT's own fresh checkpoint handoff under reports/, if present. This is
    the authoritative, detailed one to RESUME from — preferred over the module's
    compact root/ handoff so the agent picks up its real subphase detail."""
    if not root:
        return None
    p = os.path.join(root, "reports", HANDOFF_FILENAME)
    if os.path.exists(p) and (_now_ts() - os.path.getmtime(p)) <= max_age:
        return p
    return None


def completion_checkpoint_due(agent: dict, rec: dict, cfg: dict, tail: str, t: dict) -> bool:
    """A coherent subphase completed with an EXACT owner-approved remaining subphase,
    at a safe idle boundary — a first-class rotation-and-resume event that fires even
    with NO context% figure. Requires `work_remaining` (genuine approved next text):
    a completed task with nothing left, or one waiting on an external action, is NOT
    a checkpoint-to-resume (it is surfaced separately, never falsely resumed)."""
    state = rec.get("state")
    if state not in ("idle", "completed"):
        return False
    if not at_safe_boundary(agent, "idle"):
        return False
    cls, _nxt = completion_class(rec, cfg, tail)
    if cls != "work_remaining":
        return False
    root = cfg.get("root") or agent.get("claude_cwd") or agent.get("cwd") or ""
    if rec.get("completion_evidence") or existing_fresh_handoff(root, t["handoff_fresh_secs"]):
        return True
    return bool(re.match(r"^/(clear|compact)\b", (agent.get("_pending_input") or "").strip()))


# ── rotation ────────────────────────────────────────────────────────────────
def _resume_instruction(handoff_path: str, remaining: dict) -> str:
    """Resume message: the EXACT remaining approved subphase text/id from the
    pre-clear state — never generic 'next subphase' language, and explicitly NOT a
    request to re-run full test suites."""
    ident = remaining.get("id") or remaining.get("title") or "the approved subphase"
    return (f"Context was rotated to stay cheap. Read {handoff_path} and resume ONLY this exact "
            f"remaining approved subphase [{ident}]:\n{remaining['text']}\n"
            "Do NOT invent a new task, do NOT repeat completed work, and do NOT run full test "
            "suites unless that exact remaining task requires it.")


def evaluate(agent: dict, cfg: dict, rec: dict, prev: dict, *, act: bool = True, dispatch: bool) -> dict:
    """Assess one agent's context budget and act per policy. Returns fields to merge
    into the record: context_pct, context_tier, and (when acted) notification_state
    + handoff_path.

    `act` gates ALL side effects (writing a handoff, recording a rotation, sending
    keys). It must be False for monitor/hold agents so held/external projects are
    never mutated — those agents are detection-only. `dispatch` further gates the
    actual /clear keystroke (False = dry-run: write+verify handoff but do not clear)."""
    t = thresholds(cfg)
    tail = agent.get("recent_activity") or agent.get("_tail") or ""
    pct = detect_context_pct(tail, t["window_tokens"])
    tier = classify(pct, t)
    out: dict = {"context_pct": pct, "context_tier": tier}
    state = rec.get("state")
    key = rec.get("agent_key") or agent.get("target")
    phase = rec.get("phase") or "(none)"
    root = cfg.get("root") or agent.get("claude_cwd") or agent.get("cwd") or ""

    # Detection-only for non-auto (monitor/hold) agents: report the tier, touch nothing.
    if not act:
        if tier not in ("ok", "unknown"):
            out["notification_state"] = "context_observed"
        return out

    # A previously-rotated agent verifies its resume FIRST — after /clear its context
    # is low, so the tier would read ok/unknown and skip this if checked later.
    row = _rotation_row(key)
    if row.get("stage") == "cleared":
        if state == "working":
            _save_rotation(key, phase, pct or row.get("pct") or 0.0, "resumed",
                           row.get("handoff_path"), row.get("handoff_hash"), row.get("rotated_at"))
            out["notification_state"] = "rotated_resumed"
        else:
            out["notification_state"] = "rotated_awaiting_resume"
        return out

    # Pending-input snapshot (for /clear safety) + completion class (drives resume).
    pending = ac.pending_input_text(key, tail=tail)
    agent["_pending_input"] = pending
    cls, _rem_early = completion_class(rec, cfg, tail)

    # The actual /clear is ALWAYS gated on a real context-threshold signal (owner
    # policy: "only when context reaches policy threshold"). A completed/checkpoint
    # event at LOW context is SURFACED separately (durable commander_event) but must
    # NOT trigger a /clear. checkpoint_due is retained only for the notification name.
    checkpoint_due = completion_checkpoint_due(agent, rec, cfg, tail, t)

    if tier in ("ok", "unknown"):
        return out

    # Finish-soon suppression — unless there is genuine work_remaining to rotate INTO.
    if finish_soon(agent, rec) and cls != "work_remaining":
        out["notification_state"] = "context_finish_soon_suppressed"
        return out

    # 45–55%: prepare a compact handoff only, no clear.
    if tier == "checkpoint":
        if at_safe_boundary(agent, state):
            content, _h = build_handoff(rec, cfg, root, pct or t["checkpoint_pct"], t["handoff_max_bytes"])
            path = write_handoff(root, content)
            out["handoff_path"] = path
            out["notification_state"] = "context_checkpoint_prepared" if path else "context_checkpoint_failed"
        else:
            out["notification_state"] = "context_checkpoint_pending"
        return out

    # 55–65% rotates only when genuine work remains to rotate INTO; >=65% rotates.
    if tier == "rotate_substantial" and cls != "work_remaining" \
            and not substantial_work_remains(rec, cfg):
        out["notification_state"] = "context_rotate_skipped_finishing"
        return out

    if not at_safe_boundary(agent, state):
        out["notification_state"] = "context_rotate_deferred"
        return out

    # STABLE-IDLE gate — the agent must have been at rest across sweeps + a dwell,
    # so a single-instant idle read of a mid-turn agent can never clear live work.
    if not stably_idle(rec, prev, t["min_idle_dwell_secs"]):
        out["notification_state"] = "context_rotate_deferred_unsettled"
        return out

    # Classify the completion so a DONE task is never falsely resumed.
    cls, remaining = completion_class(rec, cfg, tail)

    # /clear input-line safety: an empty line is safe; a bare `/clear` the agent
    # itself queued is the rotation request (submitted as-is); ANY other queued
    # instruction would be SUBMITTED by a paste → refuse.
    clear_only = bool(re.match(r"^/(clear|compact)\s*$", pending))
    if pending and not clear_only:
        out["notification_state"] = "context_rotate_deferred_pending_input"
        return out

    # Capture the AGENT's own detailed handoff BEFORE writing the module's compact
    # one (write_handoff would otherwise become the freshest and shadow it).
    agent_handoff = agent_authored_handoff(root, t["handoff_fresh_secs"])
    content, chash = build_handoff(rec, cfg, root, pct or t["rotate_pct"], t["handoff_max_bytes"])
    if not can_rotate_again(key, chash, t["cooldown_secs"]):
        out["notification_state"] = "context_rotate_cooldown"
        return out
    path = write_handoff(root, content)
    if not handoff_is_fresh(path, t["handoff_fresh_secs"]):
        out["notification_state"] = "context_handoff_failed"     # abort — never /clear without it
        return out
    # AUTHORITATIVE resume handoff — resolved/persisted, never a hardcoded root path.
    resume_handoff = resolve_handoff_path(root, cfg, row, t["handoff_fresh_secs"]) or agent_handoff or path
    out["handoff_path"] = path
    out["rotation"] = {
        "project": rec.get("project") or cfg.get("project"),
        "reason": ("checkpoint_completed_work_remaining" if cls == "work_remaining" else cls),
        "completion_class": cls,
        "resume_handoff": resume_handoff,
        "remaining": (remaining.get("text") if remaining else None),
        "remaining_id": (remaining.get("id") if remaining else None),
        "handoff_path": path,
        "context_pct": pct,
    }
    _save_rotation(key, phase, pct or 0.0, "handoff_written", path, chash, None)

    if not dispatch:
        out["notification_state"] = "safe_rotation_due" if checkpoint_due else "context_rotate_pending"
        return out

    idem = f"ctxrot:{key}:{chash}"
    try:
        if clear_only:
            ac.submit_clear(key, idempotency_key=f"{idem}:clear")   # submit the agent's own /clear
        else:
            ac.agent_send(key, "/clear", idempotency_key=f"{idem}:clear")
        time.sleep(0.6)
        # Resume WORK only when there is an exact owner-approved remaining subphase.
        # A completed task (nothing left, or waiting on an external action) is
        # rotated to save tokens but NOT told to continue — it stays idle and the
        # completion/external event is surfaced.
        if cls == "work_remaining" and remaining:
            ac.agent_send(key, _resume_instruction(resume_handoff, remaining),
                          idempotency_key=f"{idem}:resume")
        try:
            ac.ensure_auto_mode(key)                    # /clear can reset mode → restore
        except Exception:  # noqa: BLE001 — best-effort; the sweep also re-enforces
            pass
    except ac.AgentControlError as e:
        out["notification_state"] = "context_rotate_dispatch_failed"
        out["error"] = str(e)[:160]
        return out
    _save_rotation(key, phase, pct or 0.0, "cleared", path, chash, _now_ts())
    out["rotation"]["action"] = "rotated_and_resumed" if cls == "work_remaining" else "rotated_idle"
    if cls == "work_remaining":
        out["notification_state"] = "context_rotated_checkpoint" if checkpoint_due else "context_rotated"
    elif cls == "task_completed_waiting_external":
        out["notification_state"] = "task_completed_waiting_external_action"
    else:
        out["notification_state"] = "task_completed_no_remaining_work"
    return out
