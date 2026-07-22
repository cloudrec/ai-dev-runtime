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
}
HANDOFF_FILENAME = "CONTEXT_HANDOFF.md"

# Active-execution markers: their presence means a command/tool/edit/test/build is
# still running, so the pane is NOT a safe boundary.
_ACTIVE_EXEC_RE = re.compile(
    r"(esc to interrupt|\bthinking…|\bthinking\.\.\.|\brunning…|\brunning\.\.\.|"
    r"\bcompacting|\btool call|\bexecuting\b|✻|✽)", re.I)

# Finish-soon cues: the agent is one or two steps from done → finish, don't clear.
_FINISH_SOON_RE = re.compile(
    r"(final commit|last step|one (?:more|last)|almost done|wrapping up|ready to commit|"
    r"about to (?:commit|finish)|just need to|running the (?:tests?|build)|one (?:test|build) run)",
    re.I)

# Context signal printed in the pane.
_PCT_LEFT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:left|remaining|until auto-?compact)", re.I)
_PCT_USED_RE = re.compile(r"context(?:\s+window)?\s+(?:used|full)[:\s]+(\d{1,3}(?:\.\d+)?)\s*%", re.I)
_TOKENS_RE = re.compile(r"/clear to save\s+([\d.]+)\s*([km])?\s*tokens", re.I)


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
        return _clamp(float(m.group(1)))
    m = _PCT_LEFT_RE.search(tail)
    if m:
        return _clamp(100.0 - float(m.group(1)))
    m = _TOKENS_RE.search(tail)
    if m and window_tokens > 0:
        val = float(m.group(1))
        unit = (m.group(2) or "").lower()
        tokens = val * 1000 if unit == "k" else val * 1_000_000 if unit == "m" else val
        return _clamp(100.0 * tokens / window_tokens)
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


def finish_soon(agent: dict, rec: dict) -> bool:
    """The task will plausibly finish within 1–2 commands or one test/build run —
    finishing is cheaper than rotating. True when a completion report was just
    written, or the pane shows an explicit wrap-up cue."""
    if rec.get("completion_evidence"):
        return True
    tail = agent.get("recent_activity") or agent.get("_tail") or ""
    return bool(_FINISH_SOON_RE.search(tail))


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


# ── rotation ────────────────────────────────────────────────────────────────
def _resume_instruction(root: str) -> str:
    return (f"Your context was rotated to stay cheap and reliable. Read {HANDOFF_FILENAME} in "
            f"{root} and continue the SAME approved task from where it left off. "
            "Do not start a new task; re-verify tests before changing code.")


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

    if tier in ("ok", "unknown"):
        return out

    # Finish-soon: never spend a rotation on work that is about to complete.
    if finish_soon(agent, rec):
        out["notification_state"] = "context_finish_soon_suppressed"
        return out

    if tier == "checkpoint":
        # Prepare/update a compact handoff at the boundary; do NOT /clear yet.
        if at_safe_boundary(agent, state):
            content, _h = build_handoff(rec, cfg, root, pct or t["checkpoint_pct"], t["handoff_max_bytes"])
            path = write_handoff(root, content)
            out["handoff_path"] = path
            out["notification_state"] = "context_checkpoint_prepared" if path else "context_checkpoint_failed"
        else:
            out["notification_state"] = "context_checkpoint_pending"
        return out

    # 55–65% rotates only when substantial work remains; >=65% rotates regardless.
    if tier == "rotate_substantial" and not substantial_work_remains(rec, cfg):
        out["notification_state"] = "context_rotate_skipped_finishing"
        return out

    if not at_safe_boundary(agent, state):
        out["notification_state"] = "context_rotate_deferred"
        return out
    if state == "completed":
        return out                                     # never rotate an idle completed agent

    content, chash = build_handoff(rec, cfg, root, pct or t["rotate_pct"], t["handoff_max_bytes"])
    if not can_rotate_again(key, chash, t["cooldown_secs"]):
        out["notification_state"] = "context_rotate_cooldown"    # cooldown or unchanged-hash
        return out

    path = write_handoff(root, content)
    if not handoff_is_fresh(path, t["handoff_fresh_secs"]):
        out["notification_state"] = "context_handoff_failed"     # abort — never /clear without it
        return out
    out["handoff_path"] = path
    _save_rotation(key, phase, pct or 0.0, "handoff_written", path, chash, None)

    if not dispatch:
        out["notification_state"] = "context_rotate_pending"     # dry-run: would rotate
        return out

    idem = f"ctxrot:{key}:{chash}"
    try:
        ac.agent_send(key, "/clear", idempotency_key=f"{idem}:clear")
        time.sleep(0.6)
        ac.agent_send(key, _resume_instruction(root), idempotency_key=f"{idem}:resume")
    except ac.AgentControlError as e:
        out["notification_state"] = "context_rotate_dispatch_failed"
        out["error"] = str(e)[:160]
        return out
    _save_rotation(key, phase, pct or 0.0, "cleared", path, chash, _now_ts())
    out["notification_state"] = "context_rotated"
    return out
