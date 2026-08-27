"""Server-side direct-agent continuation watchdog.

Fixes the 2026-08-03 failure: an approved agent finished a step, went idle, and
its pane held a typed `continue with the next safe step` that was NEVER SUBMITTED
(the Enter keystroke's tmux return code was 0, so the old delivery path reported
`submitted=true` while the text still sat unsent in the input line). The hourly
ChatGPT control did not catch it and the owner had to intervene 33 minutes later.

This watchdog runs inside the always-on daemon (no external automation), polls
live tmux agents at a low-cost interval, and for an APPROVED (managed-auto) agent
that is idle/waiting with a documented SAFE next step it delivers via the existing
multiline-safe `agent_send` path and then PROVES the delivery by requiring ALL of:

  * submitted            — the Enter keystroke command succeeded,
  * pane_changed         — the pane capture differs before/after,
  * prompt_consumed      — the input line no longer holds the continuation,
  * conversation_modified— Claude's own conversation .jsonl advanced,
  * state_transitioned   — the agent left idle / activity changed,

within a timeout. If the text was only typed (prompt not consumed) it retries
Enter exactly once, safely; if it still will not submit, it records a durable
blocker and notifies — it never silently claims success.

Guarantees:
  * never creates a duplicate agent (only ever acts on the exact live pane);
  * never repeats the same continuation — durable idempotency by
    (target, conversation_id, step_hash);
  * respects the project allowlist (managed-auto sessions only);
  * refuses to submit any pending text that is destructive / live / payment /
    credential / publication — that is surfaced to the owner, never auto-sent;
  * restart-safe (state persisted); surfaces watcher health + last action.

The decision core is pure and the side effects are injected as a `ctrl` object,
so everything is testable without a live tmux server.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional

# ── tunables ─────────────────────────────────────────────────────────────────
ENABLED = os.getenv("CONTINUATION_WATCHDOG_ENABLED", "1") not in ("0", "false", "no", "")
INTERVAL = int(os.getenv("CONTINUATION_WATCHDOG_INTERVAL_SECS", "30"))
# The agent must have been idle at least this long before we act — debounces a
# false idle while Claude is between tool calls / thinking.
IDLE_CONFIRM_SECS = int(os.getenv("CONTINUATION_WATCHDOG_IDLE_CONFIRM_SECS", "20"))
# How long to wait for the submission to prove itself (Claude can take several
# seconds to consume the line and start streaming).
VERIFY_TIMEOUT = int(os.getenv("CONTINUATION_WATCHDOG_VERIFY_TIMEOUT_SECS", "12"))
# Documented default safe next step (a meta-instruction, not a command).
DEFAULT_CONTINUATION = os.getenv(
    "CONTINUATION_WATCHDOG_DEFAULT_STEP", "continue with the next safe step")
# P4 PREP (default OFF): route delivery through the canonical Control Plane Actuator under
# a lease instead of the legacy inline path. Dormant until this flag AND the actuator's own
# CONTROL_PLANE_ACTUATOR_ENABLED are both on (owner cutover gate G1) — with the actuator
# disabled, routing is a safe no-op (delivers nothing).
ROUTE_VIA_ACTUATOR = os.getenv("CONTINUATION_VIA_ACTUATOR", "0") not in ("0", "false", "no", "")
# Max proactive deliveries per conversation (only for opt-in proactive sessions).
MAX_CONTINUATIONS = int(os.getenv("CONTINUATION_WATCHDOG_MAX_PER_CONV", "25"))
# A BLOCKED step is not permanent — it may be re-attempted after this cooldown
# (conditions / code may have changed), up to a hard attempt cap. A VERIFIED step is
# never repeated.
BLOCKED_RETRY_COOLDOWN_SECS = int(os.getenv("CONTINUATION_WATCHDOG_BLOCK_COOLDOWN_SECS", "600"))
MAX_STEP_ATTEMPTS = int(os.getenv("CONTINUATION_WATCHDOG_MAX_STEP_ATTEMPTS", "6"))

# A continuation must be benign prose. Anything that looks like a destructive /
# live / payment / credential / publication action is NEVER auto-submitted — it is
# surfaced to the owner. Deny-by-default on these tokens. The denylist is a
# SECONDARY wall: the primary gate (is_safe_continuation) is allowlist-structural
# and fail-closed — a denylist miss alone never makes text safe.
_FORBIDDEN_RE = re.compile(
    r"(rm\s+-rf|\brm\b|\bdrop\b|truncate|delete\s+from|\bdelete\b|\bkill\b|shutdown|reboot|"
    r"\bpush\b|force[- ]?push|publish|deploy|release|npm\s+publish|docker\s+push|"
    # 2026-08-03 audit §3.1 verified holes: "promote"/"send … BTC … wallet" dodged
    # the list while an autonomous-safe prefix classified them safe.
    r"\bpromote\b|promotion|\bsend\b|\bwallet\b|\bbtc\b|\beth\b|bitcoin|crypto|"
    r"\bbuy\b|\bsell\b|\btrade\b|trading|\bwipe\b|\berase\b|\bdestroy\b|\brestart\b|"
    r"payment|charge|stripe|invoice|billing|payout|refund|withdraw|transfer|"
    r"credential|secret|token|api[_-]?key|password|\.env|private[_-]?key|"
    r"mainnet|live\s+trade|real\s+order|systemctl|reset\s+--hard|chmod|chown|"
    r"curl|wget|ssh\b|scp\b"
    # Russian destructive / live / credential stems (case-folded). The script gate
    # in is_safe_continuation already fail-closes ALL non-ASCII text; these stems
    # additionally upgrade recognisably destructive Russian to PROHIBITED in
    # classify_action. Live incident vocabulary included: «удали …» (delete).
    r"|удал|снес(и|ти)|снос|стереть|сотр(и|ите)|очист|перезапус|рестарт"
    r"|деплой|публик|отправ|перевед|перевест|перевод|перечисл"
    r"|\bкупи|прода(й|ть|ж)|ключ|парол|секрет|токен|продвин|промоут|промот"
    r"|залей|залить|форс)", re.I)

# Active-execution markers — Claude is mid-turn (thinking / running a tool). Their
# presence means an idle-looking pane must NOT be treated as finished.
_ACTIVE_EXEC_RE = re.compile(
    r"(esc to interrupt|\bthinking[.…]|\brunning[.…]|\bcompacting|\btool call|"
    r"\bexecuting\b|✻|✽)", re.I)

EVENT_BLOCKED = "agent_continuation_blocked"       # notify owner: could not continue
EVENT_CONTINUED = "agent_continuation_submitted"   # durable log: continued safely


# ── time ─────────────────────────────────────────────────────────────────────
def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── persistence (restart-safe) ───────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    # idempotency + attempt ledger, one row per (target, conversation, step)
    conn.execute("""CREATE TABLE IF NOT EXISTS cw_step (
        idkey TEXT PRIMARY KEY, target TEXT, conversation_id TEXT, step_hash TEXT,
        attempts INTEGER DEFAULT 0, submitted INTEGER DEFAULT 0, verified INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0, last_outcome TEXT, created_at TEXT, updated_at TEXT)""")
    # idle-dwell tracking per pane
    conn.execute("""CREATE TABLE IF NOT EXISTS cw_target (
        target TEXT PRIMARY KEY, last_state TEXT, idle_since_ts REAL, updated_at TEXT)""")
    # single-row health / last action for Owner OS
    conn.execute("""CREATE TABLE IF NOT EXISTS cw_health (
        id INTEGER PRIMARY KEY CHECK (id=1), last_run_at TEXT, last_action TEXT,
        agents_checked INTEGER, submitted INTEGER, verified INTEGER, retried INTEGER,
        blocked INTEGER, errors INTEGER)""")
    conn.commit()
    return conn


def _step_row(conn, idkey: str) -> Optional[dict]:
    r = conn.execute("SELECT attempts,submitted,verified,blocked,last_outcome,updated_at "
                     "FROM cw_step WHERE idkey=?", (idkey,)).fetchone()
    if not r:
        return None
    return {"attempts": r[0], "submitted": bool(r[1]), "verified": bool(r[2]),
            "blocked": bool(r[3]), "last_outcome": r[4], "updated_at": r[5]}


def _iso_epoch(s: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except Exception:  # noqa: BLE001
        return None


def should_skip_prior(prior: Optional[dict], now_ts: float) -> bool:
    """A verified step is never repeated. A blocked step is re-attemptable after a
    cooldown, up to a hard attempt cap — so a fixed watchdog can self-heal a stale
    blocker rather than leaving the agent stuck forever."""
    if not prior:
        return False
    if prior.get("verified"):
        return True
    if prior.get("blocked"):
        if (prior.get("attempts") or 0) >= MAX_STEP_ATTEMPTS:
            return True                                    # give up permanently
        last = _iso_epoch(prior.get("updated_at"))
        if last is not None and (now_ts - last) < BLOCKED_RETRY_COOLDOWN_SECS:
            return True                                    # still cooling down
    return False


def _save_step(conn, idkey, target, conv, step_hash, *, attempts, submitted, verified,
               blocked, outcome) -> None:
    now = _now_iso()
    exists = conn.execute("SELECT 1 FROM cw_step WHERE idkey=?", (idkey,)).fetchone()
    if exists:
        conn.execute("UPDATE cw_step SET attempts=?,submitted=?,verified=?,blocked=?,"
                     "last_outcome=?,updated_at=? WHERE idkey=?",
                     (attempts, int(submitted), int(verified), int(blocked), outcome, now, idkey))
    else:
        conn.execute("INSERT INTO cw_step(idkey,target,conversation_id,step_hash,attempts,"
                     "submitted,verified,blocked,last_outcome,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (idkey, target, conv, step_hash, attempts, int(submitted), int(verified),
                      int(blocked), outcome, now, now))
    conn.commit()


def _get_target(conn, target: str) -> Optional[dict]:
    r = conn.execute("SELECT last_state,idle_since_ts FROM cw_target WHERE target=?",
                     (target,)).fetchone()
    return {"last_state": r[0], "idle_since_ts": r[1]} if r else None


def _save_target(conn, target, state, idle_since_ts) -> None:
    conn.execute("INSERT INTO cw_target(target,last_state,idle_since_ts,updated_at) "
                 "VALUES(?,?,?,?) ON CONFLICT(target) DO UPDATE SET last_state=excluded.last_state,"
                 "idle_since_ts=excluded.idle_since_ts,updated_at=excluded.updated_at",
                 (target, state, idle_since_ts, _now_iso()))
    conn.commit()


def _save_health(conn, h: dict) -> None:
    conn.execute(
        "INSERT INTO cw_health(id,last_run_at,last_action,agents_checked,submitted,verified,"
        "retried,blocked,errors) VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "last_run_at=excluded.last_run_at,last_action=excluded.last_action,"
        "agents_checked=excluded.agents_checked,submitted=excluded.submitted,"
        "verified=excluded.verified,retried=excluded.retried,blocked=excluded.blocked,"
        "errors=excluded.errors",
        (h.get("last_run_at"), h.get("last_action"), h.get("agents_checked", 0),
         h.get("submitted", 0), h.get("verified", 0), h.get("retried", 0),
         h.get("blocked", 0), h.get("errors", 0)))
    conn.commit()


def health(load_config: Optional[Callable] = None,
           live_sessions: Optional[Callable] = None) -> dict:
    """Watchdog health + last action. Flags MISCONFIGURATION when zero eligible
    (managed-auto) sessions are configured — otherwise a watchdog that can never
    act would falsely look healthy (the arbitrage2-opus incident)."""
    if load_config is None:
        try:
            from core import agent_orchestrator as _orch
            load_config = _orch.load_config
        except Exception:  # noqa: BLE001
            load_config = lambda: {}
    elig = sorted(eligible_sessions(load_config))
    conn = _db()
    try:
        r = conn.execute("SELECT last_run_at,last_action,agents_checked,submitted,verified,"
                         "retried,blocked,errors FROM cw_health WHERE id=1").fetchone()
        base = {"enabled": ENABLED, "eligible_sessions": elig,
                "eligible_count": len(elig)}
        if not r:
            base.update({"last_run_at": None, "last_action": None})
        else:
            base.update({"last_run_at": r[0], "last_action": r[1], "agents_checked": r[2],
                         "submitted": r[3], "verified": r[4], "retried": r[5],
                         "blocked": r[6], "errors": r[7]})
        # COVERAGE, not just configuration. The original warning caught "zero
        # sessions configured" (the arbitrage2-opus incident). The next step of the
        # same blind spot is a watchdog with sessions configured that are not
        # RUNNING: it checks nothing, reports ok, and the owner believes idle agents
        # are being continued automatically while every live one sits outside its
        # allowlist. Coverage is therefore measured against the live inventory.
        # The inventory is INJECTED, like every other side effect in this module:
        # health() must stay pure and hermetic, so it reports coverage only when a
        # caller supplies the live sessions (the API does). Without it, the
        # configuration-level checks below still apply.
        live, live_error = None, None
        if live_sessions is not None:
            try:
                live = {str(x).split(":")[0] for x in (live_sessions() or set()) if x}
            except Exception as e:  # noqa: BLE001 — health never depends on tmux
                live_error = f"{type(e).__name__}: {str(e)[:80]}"
        if live is not None:
            eligible_live = sorted(live & set(elig))
            unmanaged_live = sorted(live - set(elig))
            base.update({"live_agent_sessions": len(live),
                         "eligible_live": eligible_live,
                         "unmanaged_live": unmanaged_live,
                         "coverage": f"{len(eligible_live)}/{len(live)}"})
        else:
            eligible_live, unmanaged_live = [], []
        if live_error:
            base["live_inventory_error"] = live_error

        # Misconfiguration: enabled but nothing it is allowed to act on.
        if ENABLED and len(elig) == 0:
            base["status"] = "warning"
            base["warning"] = "no_eligible_sessions: watchdog enabled but no managed-auto " \
                              "session is configured — it can never act (misconfiguration)"
        elif ENABLED and live and not eligible_live:
            # Configured, running, and covering nothing that exists.
            base["status"] = "warning"
            base["warning"] = (
                f"covering_nothing: {len(live)} live agent session(s) and none is "
                f"managed-auto, so no idle agent can be continued automatically — "
                f"unmanaged: {', '.join(unmanaged_live[:8])}"
                + (" …" if len(unmanaged_live) > 8 else ""))
        else:
            base["status"] = "ok"
        return base
    finally:
        conn.close()


# ── pure helpers ─────────────────────────────────────────────────────────────
# FAIL-CLOSED continuation gate. 2026-08-03 live incident (arbitrage2-opus:0.0):
# this gate was DENYLIST-ONLY and ENGLISH-ONLY, so six owner-typed RUSSIAN
# instructions — including «удали старый scratchpad», a delete — were auto-
# Entered: an English denylist cannot see Russian at all. The gate is now
# ALLOWLIST-STRUCTURAL; when the classifier is UNSURE it REFUSES:
#   * any character outside printable ASCII (Cyrillic, CJK, …) → the denylist
#     cannot evaluate the text → refuse;
#   * a bare dialog answer ("1", "y", "yes", "да", "нет") → refuse — submitting
#     it would ANSWER a permission dialog;
#   * digits anywhere → refuse (amounts/ids are never part of a safe meta-step);
#   * otherwise the text must have a RECOGNISED SAFE STEP SHAPE — a documented
#     continuation prefix AND every word from a closed benign vocabulary — or be
#     an exact bare /clear | /compact.
# Anything unrecognised → refuse (surfaced to the owner). Over-refusal is
# acceptable; under-refusal is not.
_EVALUABLE_TEXT_RE = re.compile(r"^[\x09\x0a\x0d\x20-\x7e]*$")
_DIALOG_ANSWER_RE = re.compile(
    r"^\s*(\d{1,2}[.)]?|y|yes|n|no|ok|okay|да|нет|д|н)\s*[.!]?\s*$", re.I)
_BARE_CONTEXT_RE = re.compile(r"^\s*/(clear|compact)\s*$", re.I)
_SAFE_STEP_PREFIX_RE = re.compile(
    r"^\s*(continue|proceed|resume|carry on|keep going|go on|next safe step)\b", re.I)
_SAFE_STEP_VOCAB = frozenset("""
    continue proceed resume carry keep going go on next safe step steps task tasks
    work working with the a an and then to in of for from do done doing nothing only
    please now current remaining internal observability report reports reporting
    test tests testing suite run running checks check checking commit commits
    locally local canary note notes append dated line lines log logs external read
    audit auditing update updating qa checkpoint checkpoints connection mapping
    recovery record recording write progress plan planned documented finish finishing
    complete completing wrap up summary summarize review verify verification
    analysis analyze document its all
    """.split())


def is_safe_continuation(text: str) -> bool:
    """A continuation may be auto-submitted only when it is a RECOGNISED benign
    meta-step (see the fail-closed rules above) — never a destructive / live /
    payment / credential / publication action, never unknown-script text, never a
    dialog answer, and never merely 'text the denylist did not match'."""
    t = (text or "").strip()
    if not t or len(t) > 300:
        return False
    if _FORBIDDEN_RE.search(t):
        return False
    if _BARE_CONTEXT_RE.match(t):
        return True                     # exact bare context command — recognised shape
    if not _EVALUABLE_TEXT_RE.match(t):
        return False                    # unknown script — cannot be evaluated → refuse
    if _DIALOG_ANSWER_RE.match(t):
        return False                    # would answer a permission dialog → refuse
    if re.search(r"\d", t):
        return False                    # amounts / ids / option numbers → refuse
    if not _SAFE_STEP_PREFIX_RE.match(t):
        return False                    # not a documented continuation shape → refuse
    words = re.findall(r"[a-z]+", t.lower())
    return bool(words) and all(w in _SAFE_STEP_VOCAB for w in words)


_GATE_CMD_RE = re.compile(
    r"(?:run|execute|allow|permit)?\s*(?:this\s+)?(?:command|tool)?\s*[:\-]?\s*"
    r"[`\"']?([^\n`\"']{3,200})[`\"']?")


def gate_command_text(tail: str) -> str:
    """The command a permission dialog is asking about. Best effort and deliberately
    conservative: if it cannot be isolated, the caller gets '' and REFUSES."""
    lines = [ln.strip() for ln in (tail or "").splitlines() if ln.strip()]
    for ln in reversed(lines[-14:]):
        s = ln.lstrip("│┃|>❯ ").strip()
        # a bare shell-ish line is the usual rendering of the proposed command
        if re.match(r"^[a-z0-9_./\-]+(\s+\S+)*$", s, re.I) and len(s) >= 3:
            if not re.match(r"^\d+[.)]", s) and "?" not in s:
                return s[:200]
    return ""


def _approved_gate_answer(agent: dict, cfg: dict) -> dict:
    """Consult the owner's pre-approved gate registry for this pane. Fail-closed."""
    tail = agent.get("_tail") or ""
    target = agent.get("target") or ""
    try:
        from core import approved_gates
        command = gate_command_text(tail)
        scopes = cfg.get("approved_gate_scopes") if isinstance(cfg, dict) else None
        res = approved_gates.match(target, command, scope_allowed=scopes)
        res["command"] = command
        return res
    except Exception as e:  # noqa: BLE001
        return {"allowed": False, "reason": f"gate_registry_unavailable:{e}",
                "command": ""}


def _audit_gate(conn, target: str, decision: dict, *, answered: bool) -> None:
    """Durable record of every gate answer — what was asked, which entry approved it."""
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS gate_answer_log (
            ts TEXT, target TEXT, entry_id TEXT, command TEXT, answer TEXT,
            reason TEXT, answered INTEGER)""")
        conn.execute("INSERT INTO gate_answer_log VALUES (?,?,?,?,?,?,?)",
                     (_now_iso(), target, decision.get("gate_entry") or "",
                      (decision.get("gate_command") or "")[:400],
                      (decision.get("step_text") or "")[:80],
                      decision.get("reason") or "", 1 if answered else 0))
        conn.commit()
    except Exception:  # noqa: BLE001
        pass


def _live_active_marker(tail: str) -> bool:
    """True when the LIVE status region shows real active execution. Fail-closed: if the
    shared detector is unavailable, fall back to the local regex over the whole tail
    (the conservative, over-skipping behaviour)."""
    try:
        from core.agent_control import _STATE_ACTIVE_RUN_RE, live_status_region
        return bool(_STATE_ACTIVE_RUN_RE.search(live_status_region(tail or "")))
    except Exception:  # noqa: BLE001
        return bool(_ACTIVE_EXEC_RE.search(tail or ""))


def pane_shows_dialog(tail: str) -> bool:
    """FAIL-CLOSED wrapper around the bilingual dialog detector: when detection is
    unavailable the pane is treated as SHOWING a dialog (refuse), never as clear."""
    if not (tail or "").strip():
        return False
    try:
        from core import agent_control as _ac
        return _ac.looks_like_dialog(tail)
    except Exception:  # noqa: BLE001
        return True


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def step_hash(conversation_id: Optional[str], step_text: str) -> str:
    return hashlib.sha256(
        f"{conversation_id or ''}\x1f{_norm(step_text)}".encode()).hexdigest()[:16]


def idkey(target: str, conversation_id: Optional[str], sh: str) -> str:
    return f"{target}|{conversation_id or ''}|{sh}"


def eligible_sessions(load_config: Callable) -> set:
    """Approved projects = managed-auto sessions in the orchestrator config, plus
    an explicit env allowlist. Everything else is observed, never actuated."""
    raw = os.getenv("CONTINUATION_WATCHDOG_SESSIONS", "").strip()
    sessions = {s.strip() for s in raw.split(",") if s.strip()}
    try:
        cfg = load_config() or {}
        sessions |= {name for name, sc in (cfg.get("sessions") or {}).items()
                     if isinstance(sc, dict) and sc.get("mode") == "auto"}
    except Exception:  # noqa: BLE001
        pass
    return sessions


def decide(*, agent: dict, cfg: dict, pending: str, state: str, prev_target: Optional[dict],
           now_ts: float, eligible: bool, continuation: str, proactive: bool,
           conv_count: int) -> dict:
    """Pure: what should the watchdog do with this agent right now?
    Returns {action: skip|submit|deliver|blocker, ...}."""
    if not (agent.get("alive") and agent.get("is_agent")):
        return {"action": "skip", "reason": "dead_or_not_agent"}
    if not eligible:
        return {"action": "skip", "reason": "not_allowlisted"}
    if state in ("working", "shell_running"):
        return {"action": "skip", "reason": "active"}          # real work in progress
    # ACTIVE-EXECUTION guard. 2026-08-04: this used a watchdog-local regex over the WHOLE
    # tail, and that regex matches a BARE spinner glyph — so a completed line like
    # "✻ Sautéed for 6s" anywhere in scrollback pinned the agent at "thinking" forever and
    # a queued line could never be recovered (live on cp-canary). Use the SAME detector as
    # the state classifier and the actuator, over the LIVE status region only, so there is
    # one definition of "actually running" instead of three that disagree.
    if _live_active_marker(agent.get("_tail") or ""):
        return {"action": "skip", "reason": "thinking"}        # false idle — mid-turn
    if state == "waiting_owner":
        # a genuine permission dialog — the supervisor's job, not a continuation
        return {"action": "skip", "reason": "waiting_owner_supervisor"}
    if pane_shows_dialog(agent.get("_tail") or ""):
        # FAIL-CLOSED (2026-08-03): even when the state classifier read the pane as
        # idle, a visible system/tool-permission or confirmation dialog (RU or EN)
        # means a HUMAN answer is required — pressing Enter or pasting here would
        # ANSWER the dialog. Never auto-submit anything on such a pane.
        # A dialog is never answered on its own authority. The ONLY exception is an exact
        # match in the owner's pre-approved gate registry (target + command hash/pattern
        # + scope + expiry); everything else — unknown wording, expired, ambiguous, or
        # carrying a prohibited marker — is refused, with the reason recorded.
        gate = _approved_gate_answer(agent, cfg)
        if gate.get("allowed"):
            return {"action": "answer_gate", "step_text": gate["answer"],
                    "reason": gate["reason"], "gate_entry": gate.get("entry_id"),
                    "gate_command": gate.get("command", "")[:200]}
        return {"action": "skip", "reason": "dialog_open_never_auto_answer",
                "gate_refusal": gate.get("reason"),
                "gate_command": gate.get("command", "")[:200]}

    # Idle must be CONFIRMED across the dwell window (restart-safe via cw_target).
    idle_since = (prev_target or {}).get("idle_since_ts")
    if idle_since is None or (now_ts - idle_since) < IDLE_CONFIRM_SECS:
        return {"action": "skip", "reason": "idle_not_confirmed"}

    # M2 (2026-08-04 targeted review) — UNOBSERVABLE-PANE GUARD. `_pane_tail` returns
    # "" when capture-pane fails, and every tail-based guard above (thinking marker,
    # dialog fail-closed gate) reads "" as "clear". If capture failed while send-keys
    # still worked, the watchdog would type onto a pane whose contents — a dialog,
    # foreign queued text, live work — are unknown. Unobservable ⇒ never act.
    # Placed after the classification returns above (skip/blocker touch no pane) and
    # before EVERY path that reaches the keyboard.
    # Blind = NOTHING was captured. `pending` is read from the same styled capture as
    # the tail, so text on the input line proves the pane WAS readable; only the
    # all-empty snapshot is the capture-failure signature.
    blind = not (agent.get("_tail") or "").strip() and not pending.strip()

    if pending.strip():
        # Text typed but not submitted — the exact bug. Submit only if it is SAFE.
        if not is_safe_continuation(pending):
            return {"action": "blocker", "reason": "unsafe_pending_text",
                    "step_text": pending}
        return {"action": "submit", "step_text": pending}

    # Input line empty. Proactive delivery is opt-in and capped so the watchdog
    # never invents open-ended direction or loops forever.
    if proactive and is_safe_continuation(continuation) and conv_count < MAX_CONTINUATIONS:
        if blind:
            return {"action": "skip", "reason": "unobservable_pane"}
        return {"action": "deliver", "step_text": continuation}
    return {"action": "skip", "reason": "nothing_to_submit"}


# The Claude Code input box is the region between the LAST two horizontal rules at the
# bottom of the pane. Text sitting there is TYPED BUT NOT SUBMITTED.
_RULE_RE = re.compile(r"^[\s─━═\-_]{20,}$")


def input_region(tail: str) -> str:
    """The pane's input box contents (best effort). Used to detect text that was typed
    but never submitted — `pending` alone missed it live (2026-08-04 MESS incident)."""
    lines = [ln.rstrip() for ln in (tail or "").splitlines()]
    rules = [i for i, ln in enumerate(lines) if _RULE_RE.match(ln.strip())]
    if len(rules) >= 2:
        region = lines[rules[-2] + 1:rules[-1]]
    elif rules:
        region = lines[rules[-1] + 1:]
    else:
        # No input box rendered in this capture — infer NOTHING. Guessing from the last
        # lines would read ordinary output as queued input (false "delivery failed").
        return ""
    text = " ".join(x.strip().lstrip("❯>").strip() for x in region if x.strip())
    return text if len(text) <= 400 else ""


def _body_text(tail: str) -> str:
    """The pane ABOVE the input box — the agent's actual output. Excludes the input line
    so that typing (which the original defect mistook for progress) can never change it."""
    lines = [ln.rstrip() for ln in (tail or "").splitlines()]
    rules = [i for i, ln in enumerate(lines) if _RULE_RE.match(ln.strip())]
    if not rules:
        # No input box identifiable — we cannot separate output from the input line, so
        # claim NOTHING. Returning the whole tail here would let typing masquerade as
        # output, which is the defect this whole guard exists to prevent.
        return ""
    body = lines[:rules[-2]] if len(rules) >= 2 else lines[:rules[-1]]
    return "\n".join(x for x in body if x.strip())


def text_is_queued(after: dict, expected_pending: str, *, target: str = "") -> bool:
    """True when the delivered text is STILL sitting in the input line, unsubmitted.

    Ghost-aware. Claude Code re-renders the last submitted command as a DIM recall
    suggestion in the same box; that ghost is not queued input (Enter does not submit
    it). Reading the plain capture cannot tell the two apart, so when a live target is
    available the STYLED reader (`pending_input_text`, which drops SGR-2 runs) is the
    authority — otherwise a successful delivery is followed by its own ghost, read as
    "still queued", and retried forever (observed live: attempts=4, verify_failed).
    """
    want = _norm(expected_pending)
    if not want:
        return False
    if want == _norm(after.get("pending") or ""):
        return True
    if target:
        try:
            from core import agent_control as ac
            ok, live_tail = ac.pane_capture(target, 12)
            if ok:
                # styled read: real typed text only, ghosts excluded
                return want == _norm(ac.pending_input_text(target, live_tail))
        except Exception:  # noqa: BLE001
            pass
    return want in _norm(input_region(after.get("tail") or ""))


def verify(before: dict, after: dict, *, expected_pending: str, enter_rc: int,
           target: str = "") -> dict:
    """Proofs that the step was actually EXECUTED — not merely that text appeared.

    2026-08-04 incident: MESS was left at `waiting_input` with the delivered text sitting
    unsubmitted in the input line, yet this returned ok=True. Two holes:
      * `state_transitioned` alone satisfied the final clause — but simply TYPING into the
        pane changes `activity`, so it flips for text that was never submitted;
      * `prompt_consumed` trusted `pending`, which read empty while the text was visibly
        queued in the input box.
    Now: queued text is a hard failure, and acceptance requires real progress evidence
    (transcript write, live active-execution marker, or a working/shell_running state) —
    never a mere pane/activity delta.
    """
    submitted = enter_rc == 0
    pane_changed = before.get("tail") != after.get("tail")
    queued_input = text_is_queued(after, expected_pending, target=target)
    prompt_consumed = not queued_input
    conversation_modified = (
        after.get("conv_mtime") is not None
        and after.get("conv_mtime") != before.get("conv_mtime"))
    after_tail = after.get("tail") or ""
    try:
        from core.agent_control import _STATE_ACTIVE_RUN_RE, live_status_region
        active_marker = bool(_STATE_ACTIVE_RUN_RE.search(live_status_region(after_tail)))
    except Exception:  # noqa: BLE001
        active_marker = False
    working_state = after.get("state") in ("working", "shell_running")
    # A fast step can finish inside the poll window: back at rest, transcript mtime not
    # yet visible. The line being CONSUMED plus new content OUTSIDE the input box is real
    # evidence — and it cannot be produced by typing, which is what the original defect
    # exploited (typing leaves the text queued, which `queued_input` now rejects).
    body_changed = _body_text(before.get("tail") or "") != _body_text(after.get("tail") or "")
    output_progress = bool(prompt_consumed and body_changed)
    # REAL execution evidence — a pane/activity delta ALONE is NOT evidence.
    progressed = bool(conversation_modified or active_marker or working_state
                      or output_progress)
    # reported for diagnostics only; deliberately NOT part of `ok` any more.
    state_transitioned = (
        after.get("state") != "idle"
        or after.get("activity") != before.get("activity"))
    ok = bool(submitted and pane_changed and prompt_consumed and progressed)
    return {"submitted": submitted, "pane_changed": pane_changed,
            "prompt_consumed": prompt_consumed, "queued_input": queued_input,
            "conversation_modified": conversation_modified,
            "active_marker": active_marker, "working_state": working_state,
            "progressed": progressed,
            "state_transitioned": state_transitioned, "ok": ok}


# ── side-effect controller (injectable) ──────────────────────────────────────
class Controller:
    """Wraps the real `agent_control` side effects. Tests inject a fake."""

    def __init__(self):
        from core import agent_control as ac
        from core import agent_orchestrator as orch
        self._ac = ac
        self._orch = orch

    def inventory(self):
        return self._ac.agent_list()

    def load_config(self):
        return self._orch.load_config()

    def snapshot(self, target, cwd):
        # capture_ok distinguishes "tmux could not read the pane" from "the pane is
        # blank" — the actuator refuses to act on the former (M2 guard).
        capture_ok, tail = self._ac.pane_capture(target, 12)
        pending = self._ac.pending_input_text(target, tail, cwd=cwd or "")
        conv = self._ac.conversation_evidence(cwd) or {}
        latest = conv.get("latest") or {}
        # derive state from a fuller tail
        st = self._ac.agent_status(target)
        return {"capture_ok": capture_ok, "tail": tail, "pending": pending,
                "conv_mtime": latest.get("modified_at"),
                "state": st.get("state"),
                "activity": (st.get("recent_activity") or "")[-400:]}

    def send(self, target, text, idem):
        return self._ac.agent_send(target, text, idempotency_key=idem)

    def enter(self, target):
        rc, _, _ = self._ac._tmux(["send-keys", "-t", target, "Enter"])
        return rc

    def robust_submit(self, target, text):
        """Reliable submission for a line a bare Enter would not send (the live
        "missed Enter" failure): clear the input line, then re-deliver the exact
        text via the proven multiline-safe buffer-paste + Enter path (agent_send).
        Same safety-classified text, so no new content is introduced."""
        try:
            self._ac._tmux(["send-keys", "-t", target, "C-u"])       # clear partial line
        except Exception:  # noqa: BLE001
            pass
        res = self._ac.agent_send(target, text)                      # fresh uuid → delivers
        return bool(res.get("submitted"))

    def emit(self, target, project, event_type, payload, dedup_key):
        return self._ac.record_commander_event(target, project, event_type, payload,
                                                dedup_key=dedup_key, dedup_window_secs=86400)


def _count_route_skip(h: dict, reason: str) -> None:
    h["last_action"] = f"route_noop:{reason}"


def _route_via_actuator(target: str) -> bool:
    """Route to the Actuator ONLY when routing is enabled AND this target is an explicit
    canary agent. So enabling the routing retires the legacy inline path for the canary
    alone — all other agents keep the proven legacy path."""
    if not ROUTE_VIA_ACTUATOR:
        return False
    try:
        from core.control_plane import actuator
        return target in actuator.CANARY_AGENTS
    except Exception:  # noqa: BLE001
        return False


def deliver_via_actuator(target: str, step_text: str, conversation_id: str, cwd: str, ctrl):
    """P4-prep bridge: acquire the agent lease and route delivery through the canonical
    Control Plane Actuator. Safe no-op when the actuator is disabled. Returns the
    actuator result verbatim."""
    from core.control_plane import api as cp
    from core.control_plane import actuator
    lease = cp.acquire_lease(f"agent:{target}", "continuation_watchdog", ttl_secs=120)
    return actuator.actuate(target=target, action_text=step_text,
                            controller="continuation_watchdog", conversation_id=conversation_id,
                            cwd=cwd, lease=lease, ctrl=ctrl)


def deliver_and_verify(ctrl, *, target: str, cwd: str, action: str, step_text: str,
                       expected_pending: str, sleep=time.sleep) -> dict:
    """Perform the submit/deliver, then poll until the five proofs hold or timeout;
    retry Enter exactly once if the prompt was not consumed."""
    before = ctrl.snapshot(target, cwd)
    if action == "deliver":
        # transport idempotency key is UNIQUE PER ATTEMPT. 2026-08-03 live failure:
        # the old constant key f"cw:{step_hash(None, step_text)}" meant the SECOND
        # legitimate delivery of the same text EVER (e.g. the bare /clear of every
        # context rotation after the first) was silently dropped by agent_send's
        # durable dedupe → verify_failed → spurious owner gate. Logical dedupe is
        # the job of the layered ledgers (cw_step / cp_action), not the transport.
        idem = f"cw:{step_hash(None, step_text)}:{int(_now_ts() * 1000)}"
        res = ctrl.send(target, step_text, idem)
        enter_rc = 0 if res.get("submitted") else 1
    else:                                    # submit an already-typed line
        enter_rc = ctrl.enter(target)

    def _poll_verify():
        deadline = _now_ts() + VERIFY_TIMEOUT
        last = None
        while _now_ts() < deadline:
            sleep(1)
            after = ctrl.snapshot(target, cwd)
            last = verify(before, after, expected_pending=expected_pending,
                          enter_rc=enter_rc, target=target)
            if last["ok"]:
                return last, after
        return last, ctrl.snapshot(target, cwd)

    v, after = _poll_verify()
    retried = False
    # Retry whenever the text is still queued — either detected in `pending` or seen in
    # the rendered input box (2026-08-04: `pending` read empty while the line was queued,
    # so keying the retry off `prompt_consumed` alone missed the real failure).
    if not (v and v["ok"]) and v and (v.get("queued_input") or not v["prompt_consumed"]):
        # Text is still sitting unsubmitted (the live "missed Enter" failure) —
        # retry ONCE with the reliable clear + paste + Enter path rather than a bare
        # Enter that already did not land. 2026-08-03 live race fix: re-check the
        # input line IMMEDIATELY before the re-paste — if the first Enter was merely
        # slow and the line was consumed between polls, a blind robust_submit pastes
        # a DUPLICATE copy of the step; and if DIFFERENT text has appeared meanwhile,
        # C-u would destroy someone else's queued input and paste ours over it.
        fresh = ctrl.snapshot(target, cwd)
        fresh_pending = _norm(fresh.get("pending") or "")
        still_queued = text_is_queued(fresh, step_text, target=target)
        if fresh_pending == _norm(step_text) or (still_queued and not fresh_pending):
            # Our exact text is still queued. Press Enter FIRST — a bare submit can never
            # duplicate it. Only if that fails to consume the line do we fall back to the
            # clear+repaste path (same text, so still no new content introduced).
            retried = True
            enter_rc = ctrl.enter(target)
            v, after = _poll_verify()
            if not (v and v["ok"]) and text_is_queued(after, step_text, target=target):
                enter_rc = 0 if ctrl.robust_submit(target, step_text) else 1
                v, after = _poll_verify()
        elif not fresh_pending and not still_queued:
            # line already consumed (slow Enter landed) — treat the keystroke as
            # delivered and re-verify; never paste a second copy.
            enter_rc = 0
            v, after = _poll_verify()
        # else: foreign text now occupies the line — do NOT clear or overwrite it;
        # fall through with the failed verify (caller records a blocker/owner gate).
    return {"verify": v or {}, "retried": retried, "enter_rc": enter_rc,
            "after": after}


# ── sweep ─────────────────────────────────────────────────────────────────────
def run_once(ctrl=None, *, now_ts: Optional[float] = None, sleep=time.sleep) -> dict:
    """One watchdog pass over eligible agents."""
    if not ENABLED:
        return {"enabled": False}
    ctrl = ctrl or Controller()
    now_ts = now_ts if now_ts is not None else _now_ts()
    conn = _db()
    h = {"agents_checked": 0, "submitted": 0, "verified": 0, "retried": 0,
         "blocked": 0, "errors": 0, "last_action": None}
    actions = []
    try:
        try:
            inv = ctrl.inventory()
        except Exception as e:  # noqa: BLE001
            h["errors"] += 1
            _save_health(conn, {**h, "last_run_at": _now_iso(),
                                "last_action": f"inventory_error:{str(e)[:80]}"})
            return {"enabled": True, "error": str(e)[:160], "actions": []}
        allow = eligible_sessions(ctrl.load_config)

        for a in inv.get("agents", []):
            if not (a.get("is_agent") and a.get("alive")):
                continue
            target = a["target"]
            session = a.get("session") or target.split(":", 1)[0]
            cwd = a.get("claude_cwd") or a.get("cwd") or ""
            state = a.get("state") or "idle"
            is_elig = session in allow
            if not is_elig:
                continue
            h["agents_checked"] += 1

            # dwell bookkeeping (restart-safe)
            prev_t = _get_target(conn, target)
            # AT-REST states accumulate the dwell window. `waiting_input` MUST be included:
            # it is exactly the missed-Enter failure this watchdog exists to recover, and
            # excluding it meant a queued line could never clear the dwell gate — decide()
            # returned `idle_not_confirmed` forever while the agent sat doing nothing
            # (2026-08-04 MESS incident: the step stayed typed but unsubmitted).
            at_rest = ("idle", "waiting_input")
            if state in at_rest:
                idle_since = ((prev_t or {}).get("idle_since_ts")
                              if (prev_t and prev_t.get("last_state") in at_rest
                                  and (prev_t or {}).get("idle_since_ts") is not None)
                              else now_ts)
            else:
                idle_since = None
            _save_target(conn, target, state, idle_since)
            prev_for_decide = {"idle_since_ts": idle_since, "last_state": state}

            try:
                pending = a.get("_pending")
                tail_for_decide = a.get("_tail")
                if pending is None or tail_for_decide is None:
                    # PRODUCTION CONTRACT: the real agent_list() inventory carries no
                    # `_tail` key, so decide()'s tail-based guards (thinking marker,
                    # dialog fail-closed gate) would otherwise evaluate against "".
                    snap = ctrl.snapshot(target, cwd)
                    if pending is None:
                        pending = snap.get("pending") or ""
                    if tail_for_decide is None:
                        tail_for_decide = snap.get("tail") or ""
                a = {**a, "_tail": tail_for_decide}
                cfg = (ctrl.load_config().get("sessions") or {}).get(session, {}) or {}
                continuation = cfg.get("safe_continuation") or DEFAULT_CONTINUATION
                proactive = bool(cfg.get("proactive_continue"))
                conv = ctrl.snapshot(target, cwd).get("conv_mtime")  # cheap; conv id below
                conv_id = _conv_id(ctrl, cwd)
                conv_count = _conv_count(conn, target, conv_id)

                d = decide(agent=a, cfg=cfg, pending=pending, state=state,
                           prev_target=prev_for_decide, now_ts=now_ts, eligible=is_elig,
                           continuation=continuation, proactive=proactive, conv_count=conv_count)
                if d["action"] == "skip":
                    # every refusal carries its reason into the audit trail
                    h["last_action"] = f"skip:{target}:{d.get('reason')}" + (
                        f":{d.get('gate_refusal')}" if d.get("gate_refusal") else "")
                    continue

                if d["action"] == "answer_gate":
                    # An owner-pre-approved confirmation: deliver the RECORDED answer.
                    log.info(f"approved gate answer for {target}: entry="
                             f"{d.get('gate_entry')} cmd={d.get('gate_command')!r}")
                    _audit_gate(conn, target, d, answered=True)
                    d = {**d, "action": "deliver"}

                step_text = d["step_text"]
                sh = step_hash(conv_id, step_text)
                ik = idkey(target, conv_id, sh)
                prior = _step_row(conn, ik)
                if should_skip_prior(prior, now_ts):
                    continue      # verified → never repeat; blocked → cooling down / capped

                if d["action"] == "blocker":
                    _emit_blocker(ctrl, conn, target, session, cfg, ik, conv_id, sh,
                                  reason="unsafe_pending_text", detail=step_text[:160])
                    h["blocked"] += 1
                    h["last_action"] = f"blocked:{target}:unsafe_pending_text"
                    actions.append({"target": target, "action": "blocker",
                                    "reason": "unsafe_pending_text"})
                    continue

                # submit / deliver with verification + one retry. The text expected
                # to LEAVE the input line is the step itself (the already-typed line
                # for submit; the just-delivered text for deliver).
                if _route_via_actuator(target):
                    # Route through the canonical lease-gated Actuator for this canary and
                    # RETIRE the legacy path entirely for it: the Actuator owns the durable
                    # record (cp_action) and the CTO event (action_verified/blocked), so the
                    # watchdog writes NO cw_step and emits NO cw-ok here — it fully handles
                    # the result and continues (no dual bookkeeping / double notification).
                    bridged = deliver_via_actuator(target, step_text, conv_id, cwd, ctrl)
                    reason = bridged.get("reason")
                    if reason in ("actuator_disabled", "stale_or_no_lease", "not_canary",
                                  "already_verified", "lease_lost_midaction", "target_working"):
                        _count_route_skip(h, reason)
                    elif bridged.get("verified"):
                        h["verified"] += 1
                        h["last_action"] = f"actuator_continued:{target}"
                        actions.append({"target": target, "action": "actuator", "verified": True})
                    else:
                        h["blocked"] += 1
                        h["last_action"] = f"actuator_blocked:{target}:{reason}"
                        actions.append({"target": target, "action": "actuator", "verified": False,
                                        "reason": reason})
                    continue      # legacy ledger/event bypassed → legacy retired for canary
                out = deliver_and_verify(ctrl, target=target, cwd=cwd, action=d["action"],
                                         step_text=step_text, expected_pending=step_text,
                                         sleep=sleep)
                v = out["verify"]
                attempts = (prior["attempts"] if prior else 0) + 1 + (1 if out["retried"] else 0)
                h["submitted"] += 1 if v.get("submitted") else 0
                h["retried"] += 1 if out["retried"] else 0

                if v.get("ok"):
                    _save_step(conn, ik, target, conv_id, sh, attempts=attempts, submitted=True,
                               verified=True, blocked=False, outcome="verified")
                    ctrl.emit(target, cfg.get("project") or session, EVENT_CONTINUED,
                              {"target": target, "cwd": cwd, "conversation_id": conv_id,
                               "action": d["action"], "step": step_text[:200],
                               "verify": v, "retried": out["retried"],
                               "detected_at": _now_iso()}, dedup_key=f"cw-ok:{ik}")
                    h["verified"] += 1
                    h["last_action"] = f"continued:{target}:{d['action']}" + (":retried" if out["retried"] else "")
                    actions.append({"target": target, "action": d["action"],
                                    "verified": True, "retried": out["retried"]})
                else:
                    _save_step(conn, ik, target, conv_id, sh, attempts=attempts,
                               submitted=bool(v.get("submitted")), verified=False, blocked=True,
                               outcome="verify_failed")
                    _emit_blocker(ctrl, conn, target, session, cfg, ik, conv_id, sh,
                                  reason="continuation_not_verified", detail=str(v)[:200])
                    h["blocked"] += 1
                    h["last_action"] = f"blocked:{target}:not_verified"
                    actions.append({"target": target, "action": d["action"],
                                    "verified": False, "blocked": True, "verify": v})
            except Exception as e:  # noqa: BLE001
                h["errors"] += 1
                h["last_action"] = f"error:{target}:{str(e)[:60]}"
        _save_health(conn, {**h, "last_run_at": _now_iso()})
    finally:
        conn.close()
    return {"enabled": True, "actions": actions, "health": h}


def _conv_id(ctrl, cwd: str) -> Optional[str]:
    try:
        from core import agent_control as ac
        ev = ac.conversation_evidence(cwd) or {}
        return (ev.get("latest") or {}).get("conversation_id")
    except Exception:  # noqa: BLE001
        return None


def _conv_count(conn, target: str, conv_id: Optional[str]) -> int:
    if not conv_id:
        return 0
    r = conn.execute("SELECT count(*) FROM cw_step WHERE target=? AND conversation_id=? "
                     "AND verified=1", (target, conv_id)).fetchone()
    return r[0] if r else 0


def _emit_blocker(ctrl, conn, target, session, cfg, ik, conv_id, sh, *, reason, detail) -> None:
    _save_step(conn, ik, target, conv_id, sh, attempts=(_step_row(conn, ik) or {}).get("attempts", 0),
               submitted=(_step_row(conn, ik) or {}).get("submitted", False), verified=False,
               blocked=True, outcome=reason)
    try:
        ctrl.emit(target, cfg.get("project") or session, EVENT_BLOCKED,
                  {"target": target, "reason": reason, "detail": detail,
                   "conversation_id": conv_id, "owner_action_required": True,
                   "note": "watchdog could not safely continue this agent — owner input needed",
                   "detected_at": _now_iso()}, dedup_key=f"cw-block:{ik}")
    except Exception:  # noqa: BLE001
        pass


# ── always-on loop ────────────────────────────────────────────────────────────
async def run_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger("continuation_watchdog")
    if not ENABLED:
        log.info("continuation watchdog disabled")
        return
    log.info(f"continuation watchdog started (interval {INTERVAL}s)")
    while True:
        try:
            res = await asyncio.to_thread(run_once)
            acts = res.get("actions") or []
            if acts:
                log.info(f"continuation watchdog: {[a.get('action') for a in acts]}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"continuation watchdog tick error: {e}")
        await asyncio.sleep(INTERVAL)
