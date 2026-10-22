"""Agent event lifecycle supervisor.

Joins the four pieces that already exist but never closed the loop together:

  * agent events     — the durable control-plane `event` log (agent_watch, work_evidence,
                       direct_agent_lifecycle, discovery, runtime_events all write there);
  * the wake bridge  — `wake_bridge.should_wake/record` → companion → `wake_submitted` /
                       `wake_delivery` (the configured ChatGPT/assistant wake path);
  * verification     — the agent's own live pane status/output, conversation transcript
                       and git artifacts, read fresh, never the agent's or assistant's prose;
  * agent controls   — the continuation watchdog's proven same-pane `deliver_and_verify`.

Per agent it runs one small state machine:

    WORKING ──event──▶ VERIFYING ──▶ DONE | CONTINUE | OWNER_NEEDED | FAILED | PAUSED

`CONTINUE` is transient: the supervisor submits ONE safe continuation into the SAME pane,
SAME conversation, and on proof of delivery the agent is back in WORKING.

Guarantees (each one a failure this codebase already produced somewhere else):

  * DISABLED BY DEFAULT (`AGENT_EVENT_SUPERVISOR_ENABLED=0`). Nothing acts on import.
  * Deduplicated, restart-safe ledger. Every event is recorded once (`aes_event`, PK on the
    event key); the bus cursor, per-agent state and every continuation latch are durable.
    The first start never replays history, and an event older than MAX_EVENT_AGE_SECS at
    ingest (a backlog from while the supervisor was off) is ledgered, never acted on.
  * Episodes. The first event for an agent at rest opens an episode; further events fold
    into it (strongest kind wins). The wake state is aggregated over EVERY event of the
    episode straight from the bridge's own tables, so a later event can never mask an
    earlier wake that is still in flight, and a crash between the bridge's commit and ours
    still finds the wake.
  * Exactly-one continuation per episode. The latch is written BEFORE the keystroke (same
    idea as `wake_bridge.mark_submitted`); an unfinished latch at sweep start resolves to
    OWNER_NEEDED and counts against cooldown/budget — never to a second send. Sweeps are
    serialised across processes/workers by a control-plane lease.
  * Same agent / session / worktree only: pane target, Claude conversation id, cwd and tmux
    session must match what was pinned at event time (missing evidence = mismatch; a cwd
    shared by two agent panes = unprovable). Any agent_send into the pane, any recent
    watchdog submission, or any transcript write since the event means someone else acted →
    no continuation. Everything is re-judged under the `agent:<target>` lease right before
    the send. The supervisor never starts, restarts or creates an agent.
  * Claude idle ≠ background shell idle. A pane whose Claude is at rest while a background
    shell / monitor / subagent is still running is WORKING, never a continuation candidate
    (and escalates if it shows no progress for twice the stall window).
  * Owner gates. Prod/release, money/spend, credentials/secrets, CAPTCHA/anti-bot bypass,
    paid proxy/VPN, irreversible actions and substantive product questions anywhere in the
    captured tail → OWNER_NEEDED, no continuation, ever. Staged composer text likewise.
  * DONE needs evidence: completion event + agent at rest + no failure markers + a clean git
    tree. Anything less is OWNER_NEEDED with the reason.
  * PAUSED is sticky: events are still recorded, but nothing is woken, verified or continued
    until `unpause()` is called explicitly. Every sweep write is conditional on paused=0 in
    SQL, so a pause landing mid-sweep always wins.
  * Cooldowns: per-target wake cooldown, per-target continuation cooldown, and a capped number
    of continuations per rolling window.
  * FAIL CLOSED. Ambiguous wake delivery (fired but unconfirmed, acknowledged without a
    verified delivery, a wake-call error, or unresolved past the timeout), an unreadable pane
    or composer, or an unverifiable continuation all resolve to OWNER_NEEDED — never to
    "assume it worked" and never to "send again".
  * The woken assistant is asked to REVIEW continuable episodes (`aes_wake.requested_action`
    on the status API); the supervisor alone performs the one continuation. A wake that was
    never sent (bridge off, cooldown-suppressed) does not block the supervisor.

The decision core (`activity`, `owner_gate_reason`, `decide`) is pure. All side effects go
through an injected `ports` object (`Ports` is the live implementation), so the whole state
machine is deterministic under test.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# ── tunables (read at call time where a flip must not need a restart) ────────
INTERVAL = int(os.getenv("AGENT_EVENT_SUPERVISOR_INTERVAL_SECS", "60"))
# How long after an event before verifying — gives the woken assistant time to act, so the
# supervisor judges the outcome rather than racing the assistant.
VERIFY_SETTLE_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_VERIFY_SETTLE_SECS", "90"))
# A wake neither confirmed delivered nor definitively not-sent within this window is
# AMBIGUOUS (it may have reached the assistant, who may be acting) → fail closed.
WAKE_CONFIRM_TIMEOUT_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_WAKE_CONFIRM_SECS", "900"))
WAKE_COOLDOWN_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_WAKE_COOLDOWN_SECS", "600"))
CONTINUE_COOLDOWN_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_CONTINUE_COOLDOWN_SECS", "600"))
MAX_CONTINUATIONS = int(os.getenv("AGENT_EVENT_SUPERVISOR_MAX_CONTINUATIONS", "3"))
CONTINUATION_WINDOW_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_CONTINUATION_WINDOW_SECS",
                                         "3600"))
# No observable progress (pane / transcript change) for this long while at rest → stalled.
STALL_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_STALL_SECS", "1800"))
# A continuation-watchdog submission this recent blocks ours (any agent_send into the pane
# after the event blocks it regardless of age).
FOREIGN_CONTINUE_GUARD_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_FOREIGN_GUARD_SECS", "600"))
CONTINUATION_TEXT = os.getenv("AGENT_EVENT_SUPERVISOR_STEP", "continue with the next safe step")
EVENT_BATCH = 200
# Events older than this at ingest are history (supervisor was off/down), never acted on.
MAX_EVENT_AGE_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_MAX_EVENT_AGE_SECS", "3600"))
SWEEP_LEASE_SECS = int(os.getenv("AGENT_EVENT_SUPERVISOR_SWEEP_LEASE_SECS", "300"))
SOURCE = "agent_event_supervisor"
_log = logging.getLogger(SOURCE)


def enabled() -> bool:
    return os.getenv("AGENT_EVENT_SUPERVISOR_ENABLED", "0") not in ("0", "", "false", "no")


def scope() -> set:
    """Explicit owner scope (`AGENT_EVENT_SUPERVISOR_TARGETS`, comma list of tmux sessions
    or `session:w.p` targets), read per call. Empty = whole fleet (watchdog allowlist decides
    continuation). Set = canary: events outside it are ledgered and ignored — no wake, no
    verify, no owner-needed — and only targets inside it are eligible to be continued."""
    raw = os.getenv("AGENT_EVENT_SUPERVISOR_TARGETS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def in_scope(target: str) -> bool:
    sc = scope()
    return not sc or target in sc or target.split(":")[0] in sc


# ── states / event kinds ─────────────────────────────────────────────────────
WORKING, VERIFYING, DONE, CONTINUE = "WORKING", "VERIFYING", "DONE", "CONTINUE"
OWNER_NEEDED, FAILED, PAUSED = "OWNER_NEEDED", "FAILED", "PAUSED"
STATES = (WORKING, VERIFYING, DONE, CONTINUE, OWNER_NEEDED, FAILED, PAUSED)

K_INCOMPLETE, K_COMPLETED, K_WAITING = "incomplete", "completed", "waiting_input"
K_PROMPT, K_FAILED, K_STALLED, K_OWNER = "prompt", "failed", "stalled", "owner_decision"

EVENT_KINDS = {
    "work_stopped_incomplete": K_INCOMPLETE,
    "task_completed": K_COMPLETED, "agent_completed": K_COMPLETED,
    "work_completed": K_COMPLETED,
    "agent_waiting_input": K_WAITING, "agent_needs_response": K_WAITING,
    "agent_prompt_needs_response": K_PROMPT,
    "agent_process_failed": K_FAILED, "agent_dead": K_FAILED, "agent_crash_loop": K_FAILED,
    "wake_loop_no_progress": K_STALLED, "wake_loop_stalled": K_STALLED,
    "agent_stalled": K_STALLED, "agent_no_progress": K_STALLED,
    "owner_decision_required": K_OWNER, "owner_gate_opened": K_OWNER,
    "agent_owner_decision": K_OWNER, "agent_waiting_owner": K_OWNER,
}
# What the woken assistant is asked to do, per kind. Persisted with the wake and exposed on
# the read-only status API: the wake phrase itself carries no content by design.
# For continuable kinds the SUPERVISOR owns the continuation (one, verified, same session);
# the assistant is asked to review, so the two never both type into the pane.
REQUESTED_ACTION = {
    K_INCOMPLETE: "review_supervisor_continues_if_safe",
    K_COMPLETED: "verify_completion",
    K_WAITING: "review_supervisor_continues_if_safe",
    K_PROMPT: "review_prompt",
    K_FAILED: "diagnose_failure",
    K_STALLED: "diagnose_no_progress",
    K_OWNER: "owner_decision",
}
# The supervisor's own event; never ingested back (no feedback loop).
EVENT_OWNER_NEEDED = "agent_supervisor_owner_needed"

# Pane-less targets are runtime jobs; the runtime supervisor owns those.
_PANELESS_PREFIXES = ("runtimejob:",)


# ── persistence ─────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS aes_agent (
    target TEXT PRIMARY KEY, project TEXT, session TEXT, cwd TEXT, conversation_id TEXT,
    state TEXT, episode_start_ts REAL, pinned_conv_mtime TEXT, last_event_id INTEGER, last_event_key TEXT, last_event_type TEXT,
    last_event_kind TEXT, last_event_ts REAL,
    last_check_at TEXT, last_check_ts REAL, last_progress_at TEXT, last_progress_ts REAL,
    progress_sig TEXT,
    last_verified_result TEXT, last_evidence TEXT, next_action TEXT, owner_gate_reason TEXT,
    wake_status TEXT, wake_resolved_ts REAL,
    last_wake_ts REAL, continuation_count INTEGER DEFAULT 0, continuation_window_start REAL,
    last_continue_ts REAL,
    paused INTEGER DEFAULT 0, paused_at TEXT, paused_by TEXT, paused_reason TEXT,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS aes_event (
    event_key TEXT PRIMARY KEY, event_id INTEGER, target TEXT, project TEXT, type TEXT,
    kind TEXT, received_at TEXT, received_ts REAL, episode_ts REAL, disposition TEXT);
CREATE INDEX IF NOT EXISTS ix_aes_event_episode ON aes_event (target, episode_ts);
CREATE TABLE IF NOT EXISTS aes_cursor (
    id INTEGER PRIMARY KEY CHECK (id=1), last_event_id INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS aes_wake (
    event_key TEXT PRIMARY KEY, event_id INTEGER, target TEXT, project TEXT, event_type TEXT,
    requested_action TEXT, evidence TEXT, decision TEXT, reason TEXT, ts REAL, at TEXT);
CREATE TABLE IF NOT EXISTS aes_continuation (
    event_key TEXT PRIMARY KEY, target TEXT, conversation_id TEXT, cwd TEXT, text_hash TEXT,
    latched_ts REAL, latched_at TEXT, finished INTEGER DEFAULT 0, verified INTEGER DEFAULT 0,
    evidence TEXT);
CREATE TABLE IF NOT EXISTS aes_transition (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, at TEXT, target TEXT, from_state TEXT,
    to_state TEXT, event_key TEXT, reason TEXT)
"""

_AGENT_FIELDS = (
    "target", "project", "session", "cwd", "conversation_id", "state", "episode_start_ts",
    "pinned_conv_mtime", "last_event_id",
    "last_event_key", "last_event_type", "last_event_kind", "last_event_ts",
    "last_check_at", "last_check_ts", "last_progress_at", "last_progress_ts", "progress_sig",
    "last_verified_result", "last_evidence", "next_action", "owner_gate_reason",
    "wake_status", "wake_resolved_ts", "last_wake_ts",
    "continuation_count", "continuation_window_start", "last_continue_ts",
    "paused", "paused_at", "paused_by", "paused_reason", "updated_at")


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


def _get_agent(conn, target: str) -> Optional[dict]:
    r = conn.execute(f"SELECT {','.join(_AGENT_FIELDS)} FROM aes_agent WHERE target=?",
                     (target,)).fetchone()
    return dict(zip(_AGENT_FIELDS, r)) if r else None


_PAUSE_FIELDS = ("paused", "paused_at", "paused_by", "paused_reason")


def _put_agent(conn, row: dict, *, reason: str = "", event_key: str = "",
               prev_state: Optional[str] = None, now: float, owner_write: bool = False) -> bool:
    """Upsert one agent row. Only owner writes (pause/unpause) may touch the pause fields or
    a paused row: every sweep write is conditional on `paused=0` IN SQL, so a row dict read
    before an owner paused can never resurrect the agent. Returns False when refused."""
    row = dict(row)
    row["updated_at"] = now_iso()
    cols = [f for f in _AGENT_FIELDS if f in row and (owner_write or f not in _PAUSE_FIELDS)]
    cur = conn.execute(
        f"INSERT INTO aes_agent ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(target) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c != "target")
        + ("" if owner_write else " WHERE aes_agent.paused=0"),
        tuple(row[c] for c in cols))
    if cur.rowcount != 1:
        return False
    if prev_state != row.get("state"):
        conn.execute("INSERT INTO aes_transition (ts,at,target,from_state,to_state,event_key,"
                     "reason) VALUES (?,?,?,?,?,?,?)",
                     (now, now_iso(), row["target"], prev_state, row.get("state"), event_key,
                      (reason or "")[:200]))
    return True


def _cursor(conn) -> Optional[int]:
    """None = never initialised (distinct from 0, so an empty bus is not re-initialised
    forever)."""
    r = conn.execute("SELECT last_event_id FROM aes_cursor WHERE id=1").fetchone()
    return int(r[0]) if r else None


def _set_cursor(conn, eid: int) -> None:
    conn.execute("INSERT INTO aes_cursor (id,last_event_id,updated_at) VALUES (1,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET last_event_id=MAX(last_event_id,"
                 "excluded.last_event_id), updated_at=excluded.updated_at", (int(eid), now_iso()))


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)[:4000]


# ── pure classification ─────────────────────────────────────────────────────
# Claude itself mid-turn: only markers that the CLAUDE turn renders (not the footer
# counters for background shells/monitors/subagents, which agent_control folds into the
# same "working" state).
_CLAUDE_ACTIVE_RE = re.compile(
    r"(esc to interrupt|\(\d+\s*m?s(\s+\d+s)?\s*·|\(\d+m\s+\d+s\s*·|\(thinking\)|·\s*thinking\b"
    r"|…\s*\(\d+\s*m?s\b|[↑↓]\s*[\d.]+\s*k?\s*tokens\b|\bcompacting\b)", re.I)
# Background work that outlives Claude's turn: "· 1 shell", "· 2 monitors" footer counters
# (never the frozen "… still running" prose), and a parent waiting on child agents.
_BACKGROUND_RE = re.compile(
    r"(·[ \t]*[1-9]\d*[ \t]+(shells?|monitors?|background tasks?)\b(?![ \t]+still)"
    r"|waiting\s+for\s+[1-9]\d*\s+background\s+agents?\b)", re.I)
_SPINNER_GLYPHS = ("✻", "✶", "✽", "✳", "✢", "✷", "✵", "✺")
_FOOTER_LINES = 6


def _live_region(tail: str) -> str:
    lines = (tail or "").splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip()[:1] in _SPINNER_GLYPHS:
            return "\n".join(lines[i:])
    return "\n".join(lines[-_FOOTER_LINES:])


def activity(snap: Optional[dict]) -> str:
    """What the pane is doing RIGHT NOW, from fresh evidence only.

    unknown | dead | exited | dialog | claude_working | background | at_rest
    """
    if not snap or not snap.get("found") or not snap.get("capture_ok", True):
        return "unknown"
    if not snap.get("alive"):
        return "dead"
    if not snap.get("is_agent"):
        return "exited"
    state = (snap.get("state") or "").lower()
    tail = snap.get("tail") or ""
    if state == "waiting_owner":
        return "dialog"
    if _CLAUDE_ACTIVE_RE.search(_live_region(tail)):
        return "claude_working"
    footer = "\n".join(tail.splitlines()[-_FOOTER_LINES:])
    if snap.get("shell_running") or _BACKGROUND_RE.search(footer):
        return "background"
    if state == "working" or snap.get("recently_active"):
        return "claude_working"
    if state in ("idle", "waiting_input", "blocked", "done", "finished", "at_rest", ""):
        return "at_rest"
    return "unknown"


_GATE_PATTERNS = (
    ("prod_release", re.compile(
        r"\b(deploy(ing|ment)?|release|production|prod\b|go[- ]live|rollout|roll out|"
        r"publish|promote to|ship to)|деплой|релиз|выкат|прод\b|продакшн", re.I)),
    ("money_spend", re.compile(
        r"\b(payment|billing|invoice|charge|refund|payout|purchase|buy|subscription|spend|"
        r"budget|top[- ]?up|credits?|pricing|paid plan|wallet)\b|[$€£₽]\s?\d|"
        r"оплат|платеж|платёж|деньг|бюджет|купить|подписк", re.I)),
    ("credentials_secrets", re.compile(
        r"(password|passphrase|api[_ -]?key|secret|access token|auth token|\btoken\b|"
        r"credential|private key|ssh key|\.env\b|2fa|\botp\b|login required|sign in to|"
        r"пароль|ключ доступа|секрет|токен|логин)", re.I)),
    ("captcha_antibot", re.compile(
        r"(captcha|recaptcha|hcaptcha|turnstile|anti-?bot|bot detection|cloudflare challenge|"
        r"are you human|капч)", re.I)),
    ("paid_proxy_vpn", re.compile(
        r"(\bproxy\b|proxies|\bvpn\b|residential ip|прокси|впн)", re.I)),
    ("irreversible", re.compile(
        r"(rm\s+-rf|drop\s+(table|database)|delete\s+from|truncate\b|force[- ]push|"
        r"push\s+--force|reset\s+--hard|irreversible|cannot be undone|permanently|"
        r"\bwipe\b|\bdestroy\b|необратим|удалить безвозвратно)", re.I)),
)
# A question that is only "may I keep going" is not a product decision.
_BENIGN_CONTINUE_Q_RE = re.compile(
    r"^(shall|should|can|may)\s+i\s+(continue|proceed|keep going)"
    r"(\s+with\s+the\s+next\s+(safe\s+)?step)?\s*\?$"
    r"|^(do\s+you\s+)?want\s+me\s+to\s+(continue|proceed|keep going)\s*\?$", re.I)
_DECISION_RE = re.compile(
    r"(which (option|approach|one|direction)|would you like|do you want|should i\b|"
    r"do you prefer|please (choose|decide|confirm|approve)|your (decision|approval|call)|"
    r"awaiting (your|owner) (input|decision|approval)|needs? owner|"
    r"какой вариант|что выбрать|подтверд|решени[ея] владельц)", re.I)
_BOX_CHARS = "│┃|>❯─━╭╮╰╯ \t"


def _message_lines(tail: str, n: Optional[int] = None) -> list:
    """Message text of the captured tail minus pane chrome. The owner gate scans ALL of it:
    a question that scrolled a few lines up is still the question the agent is waiting on."""
    out = []
    lines = (tail or "").splitlines()
    for ln in (lines[-n:] if n else lines):
        s = ln.strip(_BOX_CHARS)
        if not s or set(s) <= set("─━-=_·•"):
            continue
        if _BACKGROUND_RE.search(s) or re.search(r"(auto mode|bypass permissions|"
                                                  r"\? for shortcuts|accept edits)", s, re.I):
            continue                                       # footer chrome, not the message
        out.append(s)
    return out


def owner_gate_reason(text: str) -> str:
    """'' when no owner gate applies; else the FIRST matching category. Deny-by-default:
    a false gate costs the owner a glance, a missed one can cost money or prod."""
    lines = _message_lines(text)
    body = "\n".join(lines)
    for name, rx in _GATE_PATTERNS:
        if rx.search(body):
            return name
    if _DECISION_RE.search(body):
        return "product_decision"
    last = lines[-1] if lines else ""
    if last.endswith("?") and not _BENIGN_CONTINUE_Q_RE.match(last):
        return "product_decision"
    return ""


_FAILURE_RE = re.compile(r"(traceback \(most recent call last\)|\bfailed\b|\berror:|"
                         r"\bexception\b|tests? failed|\d+ failed)", re.I)


def _same_session(row: dict, snap: dict) -> str:
    """'' when the live pane is provably the same agent/session/worktree recorded for the
    event, else the mismatch reason. Missing evidence is a mismatch (fail closed)."""
    if not snap.get("conversation_id"):
        return "conversation_unknown"
    if not row.get("conversation_id"):
        return "identity_not_pinned_at_event"
    if row["conversation_id"] != snap["conversation_id"]:
        return "conversation_changed"
    if not row.get("cwd") or not snap.get("cwd"):
        return "worktree_unverifiable"
    if row["cwd"] != snap["cwd"]:
        return "worktree_changed"
    if not row.get("session") or not snap.get("session"):
        return "session_unverifiable"
    if row["session"] != snap["session"]:
        return "session_changed"
    return ""


def decide(*, kind: str, snap: Optional[dict], row: dict, now: float,
           eligible: bool, foreign_recent: bool = False,
           artifacts: Optional[dict] = None) -> dict:
    # `row["pinned_conv_mtime"]` is the transcript mtime when the event was ingested; a
    # different mtime now means SOMEONE acted in this conversation since (the agent, the
    # woken assistant, the owner typing directly) — sends that leave no ledger row included.
    """Pure verdict for an agent in VERIFYING. Returns
    {state, reason, next_action, evidence}. `state` CONTINUE means: submit one continuation."""
    act = activity(snap)
    tail = (snap or {}).get("tail") or ""
    ev = {"activity": act, "kind": kind, "pane_state": (snap or {}).get("state"),
          "conversation_id": (snap or {}).get("conversation_id"),
          "artifacts": artifacts or {}, "tail_digest":
          hashlib.sha256(tail.encode()).hexdigest()[:16] if tail else ""}

    def out(state, reason, nxt):
        return {"state": state, "reason": reason, "next_action": nxt, "evidence": ev}

    if act == "unknown":
        if kind == K_FAILED and snap is not None and not snap.get("found"):
            return out(FAILED, "agent_gone_after_failure_event", "owner_review_failure")
        return out(OWNER_NEEDED, "verification_ambiguous", "owner_inspect_agent")
    if act in ("dead", "exited"):
        return out(FAILED, f"agent_{act}", "owner_review_failure")
    if act == "claude_working":
        return out(WORKING, "agent_progressing", "observe")
    if act == "background":
        if kind == K_STALLED:
            # Background work with no visible progress for the stall window: possibly a
            # hung shell. Never continue over it, never sit silent on it either.
            return out(OWNER_NEEDED, "background_work_no_progress", "owner_inspect_agent")
        return out(WORKING, "background_shell_progressing", "observe")
    if act == "dialog":
        return out(OWNER_NEEDED, "permission_prompt_pending", "owner_answer_prompt")

    # at rest from here on: Claude idle, nothing running in the background.
    if kind == K_OWNER:
        return out(OWNER_NEEDED, "owner_decision_event", "owner_decision")
    gate = owner_gate_reason(tail)
    if gate:
        return out(OWNER_NEEDED, f"owner_gate:{gate}", "owner_decision")
    if kind == K_FAILED:
        return out(FAILED, "failure_event_agent_not_progressing", "owner_review_failure")
    if kind == K_PROMPT:
        return out(OWNER_NEEDED, "agent_prompt_needs_response", "owner_answer_prompt")
    # staged text in the composer — the owner's (or anyone's) unsent draft. agent_control
    # only classifies `waiting_input` when such a line exists, so the state alone counts.
    if (snap or {}).get("pending") or (snap or {}).get("state") == "waiting_input":
        return out(OWNER_NEEDED, "foreign_pending_input", "owner_inspect_agent")
    pinned = row.get("pinned_conv_mtime")
    if pinned and (snap or {}).get("conv_mtime") and snap["conv_mtime"] != pinned:
        return out(WORKING, "conversation_advanced_since_event", "observe")
    failing = bool(_FAILURE_RE.search("\n".join(_message_lines(tail, 15))))
    if kind == K_COMPLETED and not failing:
        arts = artifacts or {}
        if not arts.get("head"):
            return out(OWNER_NEEDED, "completion_unverifiable_no_artifacts", "owner_review")
        if arts.get("dirty_files"):
            return out(OWNER_NEEDED, "completion_with_uncommitted_changes", "owner_review")
        return out(DONE, "completion_verified_at_rest_clean_tree", "none")
    # incomplete / waiting_input / stalled / completion-with-failures → continuation?
    if not eligible:
        return out(OWNER_NEEDED, "session_not_auto_continue_eligible", "owner_continue")
    mismatch = _same_session(row, snap or {})
    if mismatch:
        return out(OWNER_NEEDED, f"not_same_session:{mismatch}", "owner_inspect_agent")
    if foreign_recent:
        return out(WORKING, "recent_continuation_by_other_controller", "observe")
    win_start = row.get("continuation_window_start") or 0
    count = row.get("continuation_count") or 0
    if now - win_start > CONTINUATION_WINDOW_SECS:
        count = 0
    if count >= MAX_CONTINUATIONS:
        return out(OWNER_NEEDED, "continuation_budget_exhausted", "owner_continue")
    last_c = row.get("last_continue_ts") or 0
    if last_c and now - last_c < CONTINUE_COOLDOWN_SECS:
        return out(VERIFYING, "continue_cooldown_active", "wait_cooldown")
    return out(CONTINUE, "safe_same_session_continuation"
               + ("_after_unverified_completion" if kind == K_COMPLETED else ""),
               "continue_same_session")


# ── live ports ──────────────────────────────────────────────────────────────
class Ports:
    """Real side effects. Tests inject a fake with the same method names."""

    # events -----------------------------------------------------------------
    def events_since(self, cursor: int, limit: int, conn) -> list:
        types = tuple(EVENT_KINDS)
        rows = conn.execute(
            f"SELECT id,type,agent_id,project_id,ts_epoch,severity,owner_action_required,"
            f"payload FROM event WHERE id>? AND type IN ({','.join('?' * len(types))}) "
            f"ORDER BY id LIMIT ?", (int(cursor), *types, int(limit))).fetchall()
        return [{"id": r[0], "type": r[1], "agent_id": r[2] or "", "project_id": r[3] or "",
                 "ts": float(r[4] or 0), "severity": r[5] or "info",
                 "owner_action_required": bool(r[6]), "payload": r[7] or ""} for r in rows]

    def max_event_id(self, conn) -> int:
        r = conn.execute("SELECT MAX(id) FROM event").fetchone()
        return int(r[0] or 0)

    # wake -------------------------------------------------------------------
    def wake(self, ev: dict, conn) -> dict:
        """Ask the configured wake bridge. `cto.emit` already consults it on emission, so a
        prior decision for the same event is reused, never duplicated."""
        from core import wake_bridge as wb
        wb._conn(conn)
        prior = conn.execute("SELECT decision,reason FROM wake_audit WHERE event_id=? "
                             "ORDER BY (decision='wake') DESC, id DESC LIMIT 1",
                             (int(ev["id"]),)).fetchone()
        if prior and prior[0] == "wake":
            return {"decision": "wake", "reason": "already_decided"}
        d = wb.should_wake(event_id=ev["id"], severity=ev.get("severity") or "info",
                           event_type=ev["type"], project_id=ev.get("project_id", ""),
                           agent_id=ev.get("agent_id", ""),
                           owner_action_required=ev.get("owner_action_required", False),
                           conn=conn)
        wb.record(d, event_id=ev["id"], severity=ev.get("severity") or "info",
                  event_type=ev["type"], project_id=ev.get("project_id", ""),
                  agent_id=ev.get("agent_id", ""), conn=conn)
        return {"decision": "wake" if d.get("wake") else "skip", "reason": d.get("reason", "")}

    def wake_status(self, event_id: int, conn) -> str:
        """delivered | not_sent | pending | ambiguous — from the bridge's durable tables."""
        from core import wake_bridge as wb
        wb._conn(conn)
        for ddl in (wb._SUBMIT_SCHEMA, wb._DELIVERY_SCHEMA, wb._EXPIRE_SCHEMA):
            conn.execute(ddl)
        wb._migrate_delivery(conn)
        eid = int(event_id)
        if conn.execute("SELECT 1 FROM wake_delivery WHERE event_id=? AND delivered=1",
                        (eid,)).fetchone():
            return "delivered"
        # Only a verified delivery row counts as delivered. `acknowledged=1` alone is NOT
        # delivery: stall_doctor/agent_watch retirements and the public ack endpoint write it
        # too, and the assistant may or may not have seen the event → ambiguous.
        submitted = conn.execute("SELECT 1 FROM wake_submitted WHERE event_id=?",
                                 (eid,)).fetchone() is not None
        if submitted:
            return "pending"                 # fired; the verdict may still arrive
        expired = conn.execute("SELECT 1 FROM wake_expire_audit WHERE event_id=?",
                               (eid,)).fetchone() is not None
        woke = conn.execute("SELECT acknowledged, superseded_by FROM wake_audit WHERE "
                            "event_id=? AND decision='wake' ORDER BY id DESC LIMIT 1",
                            (eid,)).fetchone()
        if woke:
            if expired or woke[1]:
                return "not_sent"            # retired before any send ever fired
            return "ambiguous" if woke[0] else "pending"
        skip = conn.execute("SELECT reason FROM wake_audit WHERE event_id=? ORDER BY id DESC "
                            "LIMIT 1", (eid,)).fetchone()
        if not skip:
            return "not_sent"
        # a cooldown skip is re-decided later by the bridge — not final
        return "pending" if "cooldown" in (skip[0] or "") else "not_sent"

    # agent ------------------------------------------------------------------
    def inspect(self, target: str) -> dict:
        from core import agent_control as ac
        try:
            st = ac.agent_status(target)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            return {"found": not msg.startswith("no tmux pane"), "capture_ok": False,
                    "error": msg[:200]}
        inv = {}
        try:
            inv = next((a for a in ac.agent_list()["agents"] if a["target"] == st["target"]),
                       {})
        except Exception:  # noqa: BLE001
            pass
        conv = (st.get("conversation") or {}).get("latest") or {}
        # The transcript is found by cwd, so it only identifies THIS pane when no other agent
        # pane shares the cwd. Otherwise identity is unprovable → no conversation id.
        try:
            same_cwd = [a for a in ac.agent_list()["agents"] if a.get("is_agent") and
                        (a.get("claude_cwd") or a.get("cwd")) == st.get("cwd")]
        except Exception:  # noqa: BLE001
            same_cwd = [None, None]
        if len(same_cwd) != 1:
            conv = {}
        pending = ""
        if st.get("state") in ("idle", "waiting_input"):
            try:
                pending = ac.pending_input_text(st["target"], st.get("recent_activity") or "",
                                                cwd=st.get("cwd") or "") or ""
            except Exception:  # noqa: BLE001
                pending = "<unreadable>"     # fail closed: unknown composer = occupied
        return {"found": True, "capture_ok": True, "target": st["target"],
                "session": st.get("session"), "alive": st.get("alive"),
                "is_agent": st.get("is_agent"), "state": st.get("state"),
                "tail": st.get("recent_activity") or "", "cwd": st.get("cwd"),
                "conversation_id": conv.get("conversation_id"),
                "conv_mtime": conv.get("modified_at"),
                "shell_running": ac._pane_shell_running(inv) if inv else False,
                "pending": (pending or "").strip()}

    def artifacts(self, cwd: str) -> dict:
        """Cheap, read-only git evidence for the agent's worktree."""
        import subprocess
        if not cwd or not os.path.isdir(cwd):
            return {}
        try:
            head = subprocess.run(["git", "-C", cwd, "log", "-1", "--format=%h %ct"],
                                  capture_output=True, text=True, timeout=5).stdout.split()
            dirty = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=5).stdout
            return {"head": head[0] if head else "", "head_ts": int(head[1]) if len(head) > 1
                    else 0, "dirty_files": len([x for x in dirty.splitlines() if x.strip()])}
        except Exception:  # noqa: BLE001
            return {}

    def eligible(self, session: str) -> bool:
        if not session:
            return False
        if scope():
            return in_scope(session)     # explicit owner canary scope replaces the allowlist
        from core import agent_continuation_watchdog as cw
        from core import agent_orchestrator as orch
        return session in cw.eligible_sessions(orch.load_config)

    def foreign_send_since(self, target: str, *, event_ts: float, guard_since: float) -> bool:
        """Did anyone else type into this pane since the event (the woken assistant via
        agent_send, the owner's tools) or did the continuation watchdog submit recently?
        Both ledgers live in agent_control.db; an unreadable ledger counts as YES."""
        import sqlite3
        from datetime import datetime
        try:
            c = sqlite3.connect(os.getenv("AGENT_CONTROL_DB",
                                          "/root/ai-dev-runtime/agent_control.db"), timeout=5)
            try:
                names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
                if "deliveries" in names and c.execute(
                        "SELECT 1 FROM deliveries WHERE target=? AND created_ts>=? LIMIT 1",
                        (target, float(event_ts))).fetchone():
                    return True
                rows = c.execute("SELECT updated_at FROM cw_step WHERE target=? AND "
                                 "submitted=1", (target,)).fetchall() \
                    if "cw_step" in names else []
            finally:
                c.close()
        except Exception:  # noqa: BLE001
            return True
        for (u,) in rows:
            try:
                if datetime.fromisoformat(u).timestamp() >= guard_since:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def lease(self, target: str, conn):
        from core.control_plane import api as cp
        return cp.acquire_lease(f"agent:{target}", SOURCE, ttl_secs=120, conn=conn)

    def release(self, target: str, lease: dict, conn) -> None:
        from core.control_plane import api as cp
        try:
            cp.release_lease(f"agent:{target}", lease.get("lease_id", ""), conn=conn)
        except Exception:  # noqa: BLE001
            pass

    def continue_same_session(self, target: str, cwd: str, text: str) -> dict:
        """The watchdog's proven same-pane deliver + five-proof verification."""
        from core import agent_continuation_watchdog as cw
        res = cw.deliver_and_verify(cw.Controller(), target=target, cwd=cwd or "",
                                    action="deliver", step_text=text, expected_pending=text)
        v = res.get("verify") or {}
        return {"ok": bool(v.get("ok")), "retried": res.get("retried"),
                "proofs": {k: v.get(k) for k in v if k != "ok"}}

    def emit_stalled(self, row: dict, since_ts: float, now: float, conn) -> None:
        from core.control_plane import cto
        cto.emit(SOURCE, "agent_no_progress", project_id=row.get("project") or "",
                 agent_id=row["target"], severity="high", push=False,
                 payload={"no_progress_since": since_ts, "stall_secs": STALL_SECS},
                 # one per stall window: a still-stalled agent re-surfaces next window
                 dedup_key=f"aes:stall:{row['target']}:{int(since_ts)}:{int(now // STALL_SECS)}",
                 dedup_window_secs=86400, conn=conn)

    def emit_owner_needed(self, row: dict, reason: str, conn) -> None:
        from core.control_plane import cto
        cto.emit(SOURCE, EVENT_OWNER_NEEDED, project_id=row.get("project") or "",
                 agent_id=row["target"], severity="high", owner_action_required=True,
                 payload={"reason": reason, "event_id": row.get("last_event_id"),
                          "event_type": row.get("last_event_type"),
                          "next_action": row.get("next_action")},
                 dedup_key=f"aes:owner:{row['target']}:{row.get('last_event_key')}:{reason}",
                 dedup_window_secs=21600, conn=conn)


# ── the sweep ───────────────────────────────────────────────────────────────
# When several events land for one agent while an episode is open, the strongest wins.
_KIND_PRIORITY = (K_OWNER, K_FAILED, K_PROMPT, K_WAITING, K_STALLED, K_INCOMPLETE, K_COMPLETED)


def _stronger(a: Optional[str], b: str) -> str:
    if not a:
        return b
    return a if _KIND_PRIORITY.index(a) <= _KIND_PRIORITY.index(b) else b


def _new_row(target: str) -> dict:
    return {"target": target, "state": WORKING, "continuation_count": 0, "paused": 0}


def _ingest(ports, conn, now: float, summary: dict) -> None:
    cursor = _cursor(conn)
    if cursor is None and os.getenv("AGENT_EVENT_SUPERVISOR_REPLAY", "0") in ("0", "", "false"):
        # First start: never replay history (days of old events would each look actionable).
        _set_cursor(conn, ports.max_event_id(conn))
        conn.commit()
        summary["cursor_initialised"] = True
        return
    for ev in ports.events_since(cursor or 0, EVENT_BATCH, conn):
        try:
            _handle_event(ports, conn, ev, now, summary)
        except Exception as e:  # noqa: BLE001 — one bad event must not wedge the cursor
            conn.rollback()
            _record_event(conn, f"evt:{ev['id']}", ev, EVENT_KINDS.get(ev["type"], ""),
                          f"error:{type(e).__name__}", episode_ts=None, now=now)
            summary["errors"] += 1
            _log.warning("agent_event_supervisor: event %s failed: %r", ev.get("id"), e)
        _set_cursor(conn, ev["id"])
        conn.commit()


def _record_event(conn, key: str, ev: dict, kind: str, disposition: str, *,
                  episode_ts: Optional[float], now: float) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO aes_event (event_key,event_id,target,project,type,"
                       "kind,received_at,received_ts,episode_ts,disposition) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (key, ev.get("id"), ev.get("agent_id"), ev.get("project_id"),
                        ev.get("type"), kind, now_iso(), now, episode_ts, disposition))
    return cur.rowcount == 1


def _handle_event(ports, conn, ev: dict, now: float, summary: dict) -> None:
    kind = EVENT_KINDS.get(ev["type"])
    target = (ev.get("agent_id") or "").strip()
    key = f"evt:{ev['id']}"
    if not kind:
        return
    if not target or target.startswith(_PANELESS_PREFIXES):
        _record_event(conn, key, ev, kind, "ignored_no_pane_target", episode_ts=None, now=now)
        return
    if not in_scope(target):
        _record_event(conn, key, ev, kind, "ignored_out_of_scope", episode_ts=None, now=now)
        summary["out_of_scope"] += 1
        return
    if ev.get("ts") and now - ev["ts"] > MAX_EVENT_AGE_SECS:
        # A backlog (supervisor was disabled, or down) is history, not a live request: the
        # pane it describes has long moved on. Ledgered, never acted on.
        _record_event(conn, key, ev, kind, "ignored_stale", episode_ts=None, now=now)
        summary["stale"] += 1
        return
    row = _get_agent(conn, target) or _new_row(target)
    prev = row.get("state")
    if row.get("paused"):
        if _record_event(conn, key, ev, kind, "recorded_while_paused", episode_ts=None,
                         now=now):
            conn.execute("UPDATE aes_agent SET last_event_id=?, last_event_key=?, "
                         "last_event_type=?, updated_at=? WHERE target=?",
                         (ev["id"], key, ev["type"], now_iso(), target))
        summary["paused_skipped"] += 1
        return
    if prev == VERIFYING:
        # Open episode: fold the event in. Its wake (decided by the bridge at emission) joins
        # the episode's wake set, which must ALL resolve before anything is continued.
        if _record_event(conn, key, ev, kind, "folded_into_open_episode",
                         episode_ts=row.get("episode_start_ts"), now=now):
            row.update(last_event_id=ev["id"], last_event_key=key, last_event_type=ev["type"],
                       last_event_kind=_stronger(row.get("last_event_kind"), kind),
                       last_event_ts=ev.get("ts") or now)
            _put_agent(conn, row, prev_state=prev, now=now)
        summary["deduped"] += 1
        return
    if not _record_event(conn, key, ev, kind, "accepted", episode_ts=now, now=now):
        summary["deduped"] += 1
        return
    row.update(project=ev.get("project_id") or row.get("project") or "",
               state=VERIFYING, episode_start_ts=now, last_event_id=ev["id"],
               last_event_key=key, last_event_type=ev["type"], last_event_kind=kind,
               last_event_ts=ev.get("ts") or now, owner_gate_reason=None,
               next_action="wake", wake_status=None, wake_resolved_ts=None,
               session=None, cwd=None, conversation_id=None, pinned_conv_mtime=None)
    # pin the identity the continuation must later match: same pane, session, worktree,
    # conversation — and the transcript mtime, so any later activity is visible
    snap = ports.inspect(target)
    if snap.get("found") and snap.get("capture_ok", True):
        row.update(session=snap.get("session"), cwd=snap.get("cwd"),
                   conversation_id=snap.get("conversation_id"),
                   pinned_conv_mtime=snap.get("conv_mtime"))
    _put_agent(conn, row, reason=f"event:{ev['type']}", event_key=key, prev_state=prev, now=now)
    # durable BEFORE the wake: if we die after the bridge commits its decision, the episode
    # still names this event id and its wake is resolved from the bridge's own tables.
    conn.commit()
    summary["accepted"] += 1
    _request_wake(ports, conn, row, ev, kind, now, summary)


def _request_wake(ports, conn, row: dict, ev: dict, kind: str, now: float,
                  summary: dict) -> None:
    if row.get("last_wake_ts") and now - row["last_wake_ts"] < WAKE_COOLDOWN_SECS:
        decision = {"decision": "suppressed", "reason": "supervisor_wake_cooldown"}
    else:
        try:
            decision = ports.wake(ev, conn)
        except Exception as e:  # noqa: BLE001 — unknown outcome = ambiguous (see _episode_wake)
            conn.rollback()
            decision = {"decision": "error", "reason": f"wake_error:{type(e).__name__}"}
        if decision["decision"] == "wake":
            row["last_wake_ts"] = now
            summary["wakes"] += 1
    evidence = {"event_type": ev["type"], "severity": ev.get("severity"),
                "payload": (ev.get("payload") or "")[:1000],
                "conversation_id": row.get("conversation_id"), "cwd": row.get("cwd")}
    conn.execute("INSERT OR IGNORE INTO aes_wake (event_key,event_id,target,project,event_type,"
                 "requested_action,evidence,decision,reason,ts,at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (row["last_event_key"], ev["id"], row["target"], row.get("project"), ev["type"],
                  REQUESTED_ACTION[kind], _j(evidence), decision["decision"],
                  decision.get("reason", ""), now, now_iso()))
    row.update(next_action="await_wake_then_verify")
    _put_agent(conn, row, prev_state=row["state"], now=now)
    conn.commit()


def _episode_wake(ports, conn, row: dict, now: float) -> str:
    """Aggregate wake state over EVERY event of the open episode, straight from the bridge's
    tables: any ambiguous → ambiguous; any pending → pending (→ ambiguous after the confirm
    timeout); any delivered → delivered; else not_sent (nothing ever reached the assistant)."""
    ids = [r[0] for r in conn.execute(
        "SELECT event_id FROM aes_event WHERE target=? AND episode_ts=? AND event_id IS NOT NULL",
        (row["target"], row.get("episode_start_ts"))).fetchall()]
    errored = {r[0] for r in conn.execute(
        f"SELECT event_id FROM aes_wake WHERE decision='error' AND event_id IN "
        f"({','.join('?' * len(ids)) or 'NULL'})", ids).fetchall()} if ids else set()
    statuses = []
    for eid in ids:
        try:
            st = ports.wake_status(eid, conn)
        except Exception:  # noqa: BLE001
            st = "pending"
        if eid in errored and st == "not_sent":
            st = "ambiguous"               # the decision may exist in a txn we never saw
        statuses.append(st)
    if "ambiguous" in statuses:
        return "ambiguous"
    if "pending" in statuses:
        if now - (row.get("episode_start_ts") or now) >= WAKE_CONFIRM_TIMEOUT_SECS:
            return "ambiguous"
        return "pending"
    return "delivered" if "delivered" in statuses else "not_sent"


def _owner_needed(ports, conn, row: dict, reason: str, nxt: str, now: float,
                  summary: dict) -> None:
    prev = row["state"]
    row.update(state=OWNER_NEEDED, owner_gate_reason=reason, next_action=nxt)
    if not _put_agent(conn, row, reason=reason, event_key=row.get("last_event_key", ""),
                      prev_state=prev, now=now):
        return                              # paused meanwhile: the pause wins
    summary["owner_needed"] += 1
    if prev != OWNER_NEEDED and os.getenv("AGENT_EVENT_SUPERVISOR_EMIT", "1") not in (
            "0", "", "false", "no"):
        try:
            ports.emit_owner_needed(row, reason, conn)
        except Exception:  # noqa: BLE001 — notification is best-effort, state is durable
            summary["errors"] += 1


def _judge(ports, conn, row: dict, now: float) -> tuple:
    snap = ports.inspect(row["target"])
    readable = snap.get("found") and snap.get("capture_ok", True)
    arts = ports.artifacts(snap.get("cwd") or row.get("cwd") or "") if readable else {}
    foreign = ports.foreign_send_since(row["target"], event_ts=row.get("episode_start_ts") or now,
                                       guard_since=now - FOREIGN_CONTINUE_GUARD_SECS)
    verdict = decide(kind=row.get("last_event_kind") or K_INCOMPLETE, snap=snap, row=row,
                     now=now,
                     eligible=ports.eligible(row.get("session") or snap.get("session") or ""),
                     foreign_recent=foreign, artifacts=arts)
    return verdict, snap


def _apply(ports, conn, row: dict, verdict: dict, now: float, summary: dict) -> None:
    if verdict["state"] == OWNER_NEEDED:
        _owner_needed(ports, conn, row, verdict["reason"], verdict["next_action"], now, summary)
        return
    prev = row["state"]
    row.update(state=verdict["state"], next_action=verdict["next_action"])
    if _put_agent(conn, row, reason=verdict["reason"], event_key=row.get("last_event_key", ""),
                  prev_state=prev, now=now):
        summary[verdict["state"].lower()] = summary.get(verdict["state"].lower(), 0) + 1


def _verify(ports, conn, row: dict, now: float, summary: dict) -> None:
    if now - (row.get("episode_start_ts") or now) < VERIFY_SETTLE_SECS:
        return
    wake = _episode_wake(ports, conn, row, now)
    if wake != row.get("wake_status"):
        row.update(wake_status=wake, wake_resolved_ts=now)
        _put_agent(conn, row, prev_state=row["state"], now=now)
    if wake == "pending":
        return
    if wake == "ambiguous":
        _owner_needed(ports, conn, row, "wake_delivery_ambiguous", "owner_inspect_agent",
                      now, summary)
        return
    # A delivered wake means the assistant may be acting right now: give it the settle window
    # from when delivery was first observed, so the supervisor judges its outcome, not races it.
    if wake == "delivered" and now - (row.get("wake_resolved_ts") or now) < VERIFY_SETTLE_SECS:
        return
    verdict, snap = _judge(ports, conn, row, now)
    verdict["evidence"]["wake_status"] = wake
    row.update(last_check_at=now_iso(), last_check_ts=now,
               last_verified_result=verdict["reason"], last_evidence=_j(verdict["evidence"]))
    _observe_progress(row, snap, now)
    summary["verified"] += 1
    if verdict["state"] == CONTINUE:
        _continue(ports, conn, row, now, summary)
    else:
        _apply(ports, conn, row, verdict, now, summary)


def _continue(ports, conn, row: dict, now: float, summary: dict) -> None:
    from core import agent_continuation_watchdog as cw
    key = row["last_event_key"]
    if not cw.is_safe_continuation(CONTINUATION_TEXT):
        _owner_needed(ports, conn, row, "continuation_text_not_safe", "owner_continue", now,
                      summary)
        return
    lease = ports.lease(row["target"], conn)
    if not lease:
        # another controller holds the pane right now; stay in VERIFYING and re-judge next tick
        row.update(next_action="retry_after_lease")
        _put_agent(conn, row, reason="agent_leased_by_other_controller", event_key=key,
                   prev_state=row["state"], now=now)
        return
    try:
        # Re-judge under the lease: closes the window between the first look and the send
        # (a foreign send, a fresh draft, a pause, a state change).
        fresh = _get_agent(conn, row["target"]) or {}
        if fresh.get("paused"):
            return
        verdict, snap = _judge(ports, conn, row, now)
        if verdict["state"] != CONTINUE:
            row.update(last_verified_result=verdict["reason"])
            _apply(ports, conn, row, verdict, now, summary)
            return
        # exactly-once latch, durable BEFORE the keystroke
        cur = conn.execute(
            "INSERT OR IGNORE INTO aes_continuation (event_key,target,conversation_id,cwd,"
            "text_hash,latched_ts,latched_at) VALUES (?,?,?,?,?,?,?)",
            (key, row["target"], snap.get("conversation_id"), snap.get("cwd"),
             hashlib.sha256(CONTINUATION_TEXT.encode()).hexdigest()[:16], now, now_iso()))
        conn.commit()
        if cur.rowcount != 1:
            _owner_needed(ports, conn, row, "continuation_already_latched_for_event",
                          "owner_inspect_agent", now, summary)
            return
        if (_get_agent(conn, row["target"]) or {}).get("paused"):
            conn.execute("UPDATE aes_continuation SET finished=1, evidence=? WHERE event_key=?",
                         (_j({"aborted": "paused_before_send"}), key))
            conn.commit()
            return
        row.update(state=CONTINUE, next_action="continue_same_session")
        _put_agent(conn, row, reason="continuation_latched", event_key=key,
                   prev_state=VERIFYING, now=now)
        conn.commit()
        try:
            res = ports.continue_same_session(row["target"], snap.get("cwd") or "",
                                              CONTINUATION_TEXT)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": type(e).__name__}
        conn.execute("UPDATE aes_continuation SET finished=1, verified=?, evidence=? "
                     "WHERE event_key=?", (1 if res.get("ok") else 0, _j(res), key))
        _count_continuation(row, now)
        if res.get("ok"):
            row.update(state=WORKING, next_action="observe",
                       last_verified_result="continuation_verified",
                       last_progress_at=now_iso(), last_progress_ts=now)
            _put_agent(conn, row, reason="continuation_verified", event_key=key,
                       prev_state=CONTINUE, now=now)
            summary["continued"] += 1
        else:
            row["last_verified_result"] = "continuation_unverified"
            _owner_needed(ports, conn, row, "continuation_unverified", "owner_inspect_agent",
                          now, summary)
        conn.commit()
    finally:
        ports.release(row["target"], lease, conn)


def _count_continuation(row: dict, ts: float) -> None:
    if ts - (row.get("continuation_window_start") or 0) > CONTINUATION_WINDOW_SECS:
        row.update(continuation_window_start=ts, continuation_count=0)
    row.update(continuation_count=(row.get("continuation_count") or 0) + 1,
               last_continue_ts=ts)


def _observe_progress(row: dict, snap: dict, now: float) -> None:
    if not snap or not snap.get("found") or not snap.get("capture_ok", True):
        return
    sig = hashlib.sha256(f"{snap.get('conv_mtime')}|{snap.get('tail')}".encode()) \
        .hexdigest()[:16]
    if sig != row.get("progress_sig"):
        row.update(progress_sig=sig, last_progress_at=now_iso(), last_progress_ts=now)


def _recover_latches(ports, conn, now: float, summary: dict) -> None:
    """A latch without a result at sweep start = a sweep died mid-continuation (sweeps are
    serialised by the sweep lease). The send may or may not have landed: count it as a
    continuation (cooldown + budget apply) and escalate — whatever event is current now."""
    rows = conn.execute("SELECT event_key,target,latched_ts FROM aes_continuation "
                        "WHERE finished=0").fetchall()
    for key, target, latched_ts in rows:
        conn.execute("UPDATE aes_continuation SET finished=1, evidence=? WHERE event_key=?",
                     (_j({"recovered": "interrupted_mid_continuation"}), key))
        row = _get_agent(conn, target)
        if not row:
            continue
        _count_continuation(row, latched_ts or now)
        conn.execute("UPDATE aes_agent SET continuation_count=?, continuation_window_start=?, "
                     "last_continue_ts=? WHERE target=?",
                     (row["continuation_count"], row["continuation_window_start"],
                      row["last_continue_ts"], target))
        if not row.get("paused"):
            _owner_needed(ports, conn, row, "continuation_outcome_unknown_after_restart",
                          "owner_inspect_agent", now, summary)
    conn.commit()


def _watch_working(ports, conn, row: dict, now: float, summary: dict) -> None:
    """Internal stall detector for agents we track as WORKING/CONTINUE (no event needed)."""
    if now - (row.get("last_check_ts") or 0) < INTERVAL:
        return
    snap = ports.inspect(row["target"])
    row.update(last_check_at=now_iso(), last_check_ts=now)
    _observe_progress(row, snap, now)
    act = activity(snap)
    since = row.get("last_progress_ts") or now
    # at rest: STALL_SECS; background work gets twice as long before it counts as hung.
    limit = {"at_rest": STALL_SECS, "background": 2 * STALL_SECS}.get(act)
    if limit and now - since >= limit:
        # Emitted onto the bus, so a stall travels the SAME ingest → wake → verify path as
        # every other event (one code path, one ledger).
        try:
            ports.emit_stalled(row, since, now, conn)
            summary["stalled"] += 1
        except Exception:  # noqa: BLE001
            summary["errors"] += 1
    _put_agent(conn, row, prev_state=row["state"], now=now)


def run_once(ports=None, *, now: Optional[float] = None, conn=None) -> dict:
    """One sweep. Never raises for control flow; returns a summary."""
    now = now if now is not None else now_ts()
    summary = {"enabled": enabled(), "accepted": 0, "deduped": 0, "paused_skipped": 0,
               "stale": 0, "wakes": 0, "verified": 0, "continued": 0, "owner_needed": 0,
               "stalled": 0, "errors": 0, "out_of_scope": 0}
    if not summary["enabled"]:
        return summary
    from core.control_plane import api as cp
    ports = ports or Ports()
    try:
        conn, own = _conn(conn)
    except Exception as e:  # noqa: BLE001
        summary["errors"] += 1
        _log.warning("agent_event_supervisor: db unavailable: %r", e)
        return summary
    holder = f"{SOURCE}:{os.getpid()}"
    try:
        # One sweep at a time across processes/workers: the latch recovery below relies on it.
        lease = cp.acquire_lease("agent_event_supervisor:sweep", holder, ttl_secs=SWEEP_LEASE_SECS,
                                 now=now, conn=conn)
        if not lease:
            summary["skipped"] = "sweep_lease_held"
            return summary
        try:
            _recover_latches(ports, conn, now, summary)
            _ingest(ports, conn, now, summary)
            for target, in conn.execute("SELECT target FROM aes_agent WHERE paused=0 AND "
                                        "state IN (?,?,?)",
                                        (VERIFYING, WORKING, CONTINUE)).fetchall():
                if not in_scope(target):
                    continue                 # scope narrowed since: leave the row untouched
                row = _get_agent(conn, target)
                try:
                    if row["state"] == VERIFYING:
                        _verify(ports, conn, row, now, summary)
                    else:
                        _watch_working(ports, conn, row, now, summary)
                    conn.commit()
                except Exception as e:  # noqa: BLE001 — one agent must never stop the sweep
                    conn.rollback()
                    summary["errors"] += 1
                    _log.warning("agent_event_supervisor: %s failed: %r", target, e)
        finally:
            cp.release_lease("agent_event_supervisor:sweep", lease["lease_id"], conn=conn)
        return summary
    finally:
        if own:
            conn.close()


# ── owner controls (explicit, never automatic) ──────────────────────────────
def pause(target: str, *, by: str, reason: str = "", conn=None,
          now: Optional[float] = None) -> dict:
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        row = _get_agent(conn, target) or _new_row(target)
        prev = row.get("state")
        row.update(paused=1, state=PAUSED, paused_at=now_iso(), paused_by=by,
                   paused_reason=reason, next_action="await_owner_unpause")
        _put_agent(conn, row, reason=f"paused_by:{by}", prev_state=prev, now=now,
                   owner_write=True)
        conn.commit()
        return {"target": target, "state": PAUSED}
    finally:
        if own:
            conn.close()


def unpause(target: str, *, by: str, conn=None, now: Optional[float] = None) -> dict:
    """Only an explicit owner call leaves PAUSED. Resumes into WORKING (observe); the next
    meaningful event drives the machine again — nothing recorded while paused is replayed."""
    if not (by or "").strip():
        raise ValueError("unpause requires an explicit actor")
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        row = _get_agent(conn, target)
        if not row or not row.get("paused"):
            return {"target": target, "state": (row or {}).get("state"), "changed": False}
        row.update(paused=0, state=WORKING, next_action="observe", paused_reason=None,
                   paused_by=by, last_progress_ts=now, last_progress_at=now_iso())
        _put_agent(conn, row, reason=f"unpaused_by:{by}", prev_state=PAUSED, now=now,
                   owner_write=True)
        conn.commit()
        return {"target": target, "state": WORKING, "changed": True}
    finally:
        if own:
            conn.close()


# ── read-only status ────────────────────────────────────────────────────────
def status(target: Optional[str] = None, *, limit: int = 100, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        if target:
            rows = [r for r in [_get_agent(conn, target)] if r]
        else:
            rows = [dict(zip(_AGENT_FIELDS, r)) for r in conn.execute(
                f"SELECT {','.join(_AGENT_FIELDS)} FROM aes_agent ORDER BY updated_at DESC "
                "LIMIT ?", (int(limit),)).fetchall()]
        for r in rows:
            try:
                r["last_evidence"] = json.loads(r["last_evidence"]) if r["last_evidence"] \
                    else None
            except ValueError:
                pass
            w = conn.execute("SELECT event_id,event_type,requested_action,evidence,decision,"
                             "reason,at FROM aes_wake WHERE event_key=?",
                             (r.get("last_event_key"),)).fetchone()
            r["wake"] = dict(zip(("event_id", "event_type", "requested_action", "evidence",
                                  "decision", "reason", "at"), w)) if w else None
        counts = dict(conn.execute("SELECT state, COUNT(*) FROM aes_agent GROUP BY state")
                      .fetchall())
        return {"enabled": enabled(), "cursor": _cursor(conn), "counts": counts,
                "agents": rows}
    finally:
        if own:
            conn.close()


def history(target: str, *, limit: int = 50, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        return [dict(zip(("at", "from_state", "to_state", "event_key", "reason"), r))
                for r in conn.execute("SELECT at,from_state,to_state,event_key,reason FROM "
                                      "aes_transition WHERE target=? ORDER BY id DESC LIMIT ?",
                                      (target, int(limit))).fetchall()]
    finally:
        if own:
            conn.close()


async def run_loop() -> None:
    import asyncio
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(INTERVAL)
