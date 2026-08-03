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
# surfaced to the owner. Deny-by-default on these tokens.
_FORBIDDEN_RE = re.compile(
    r"(rm\s+-rf|\brm\b|\bdrop\b|truncate|delete\s+from|\bkill\b|shutdown|reboot|"
    r"\bpush\b|force[- ]?push|publish|deploy|release|npm\s+publish|docker\s+push|"
    r"payment|charge|stripe|invoice|billing|payout|refund|withdraw|transfer|"
    r"credential|secret|token|api[_-]?key|password|\.env|private[_-]?key|"
    r"mainnet|live\s+trade|real\s+order|systemctl|reset\s+--hard|chmod|chown|"
    r"curl|wget|ssh\b|scp\b)", re.I)

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


def health(load_config: Optional[Callable] = None) -> dict:
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
        # Misconfiguration: enabled but nothing it is allowed to act on.
        if ENABLED and len(elig) == 0:
            base["status"] = "warning"
            base["warning"] = "no_eligible_sessions: watchdog enabled but no managed-auto " \
                              "session is configured — it can never act (misconfiguration)"
        else:
            base["status"] = "ok"
        return base
    finally:
        conn.close()


# ── pure helpers ─────────────────────────────────────────────────────────────
def is_safe_continuation(text: str) -> bool:
    """A continuation may be auto-submitted only if it is benign prose — never a
    destructive / live / payment / credential / publication action."""
    t = (text or "").strip()
    if not t:
        return False
    return not _FORBIDDEN_RE.search(t)


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
    if _ACTIVE_EXEC_RE.search(agent.get("_tail") or ""):
        return {"action": "skip", "reason": "thinking"}        # false idle — mid-turn
    if state == "waiting_owner":
        # a genuine permission dialog — the supervisor's job, not a continuation
        return {"action": "skip", "reason": "waiting_owner_supervisor"}

    # Idle must be CONFIRMED across the dwell window (restart-safe via cw_target).
    idle_since = (prev_target or {}).get("idle_since_ts")
    if idle_since is None or (now_ts - idle_since) < IDLE_CONFIRM_SECS:
        return {"action": "skip", "reason": "idle_not_confirmed"}

    if pending.strip():
        # Text typed but not submitted — the exact bug. Submit only if it is SAFE.
        if not is_safe_continuation(pending):
            return {"action": "blocker", "reason": "unsafe_pending_text",
                    "step_text": pending}
        return {"action": "submit", "step_text": pending}

    # Input line empty. Proactive delivery is opt-in and capped so the watchdog
    # never invents open-ended direction or loops forever.
    if proactive and is_safe_continuation(continuation) and conv_count < MAX_CONTINUATIONS:
        return {"action": "deliver", "step_text": continuation}
    return {"action": "skip", "reason": "nothing_to_submit"}


def verify(before: dict, after: dict, *, expected_pending: str, enter_rc: int) -> dict:
    """Compute the five submission proofs from before/after snapshots."""
    submitted = enter_rc == 0
    pane_changed = before.get("tail") != after.get("tail")
    # prompt consumed = the continuation text is no longer sitting in the input line
    prompt_consumed = _norm(after.get("pending") or "") != _norm(expected_pending)
    conversation_modified = (
        after.get("conv_mtime") is not None
        and after.get("conv_mtime") != before.get("conv_mtime"))
    state_transitioned = (
        after.get("state") != "idle"
        or after.get("activity") != before.get("activity"))
    ok = submitted and pane_changed and prompt_consumed and (
        conversation_modified or state_transitioned)
    return {"submitted": submitted, "pane_changed": pane_changed,
            "prompt_consumed": prompt_consumed,
            "conversation_modified": conversation_modified,
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
        tail = self._ac._pane_tail(target, 12)
        pending = self._ac.pending_input_text(target, tail)
        conv = self._ac.conversation_evidence(cwd) or {}
        latest = conv.get("latest") or {}
        # derive state from a fuller tail
        st = self._ac.agent_status(target)
        return {"tail": tail, "pending": pending,
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
        res = ctrl.send(target, step_text, f"cw:{step_hash(None, step_text)}")
        enter_rc = 0 if res.get("submitted") else 1
    else:                                    # submit an already-typed line
        enter_rc = ctrl.enter(target)

    def _poll_verify():
        deadline = _now_ts() + VERIFY_TIMEOUT
        last = None
        while _now_ts() < deadline:
            sleep(1)
            after = ctrl.snapshot(target, cwd)
            last = verify(before, after, expected_pending=expected_pending, enter_rc=enter_rc)
            if last["ok"]:
                return last, after
        return last, ctrl.snapshot(target, cwd)

    v, after = _poll_verify()
    retried = False
    if not (v and v["ok"]) and v and not v["prompt_consumed"]:
        # Text is still sitting unsubmitted (the live "missed Enter" failure) —
        # retry ONCE with the reliable clear + paste + Enter path rather than a bare
        # Enter that already did not land.
        retried = True
        enter_rc = 0 if ctrl.robust_submit(target, step_text) else 1
        v, after = _poll_verify()
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
            if state == "idle":
                idle_since = (prev_t or {}).get("idle_since_ts") if (prev_t and prev_t.get("last_state") == "idle") else now_ts
            else:
                idle_since = None
            _save_target(conn, target, state, idle_since)
            prev_for_decide = {"idle_since_ts": idle_since, "last_state": state}

            try:
                pending = a.get("_pending")
                if pending is None:
                    snap = ctrl.snapshot(target, cwd)
                    pending = snap.get("pending") or ""
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
                    continue

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
                    # Route through the canonical lease-gated Actuator, but ONLY for an
                    # explicitly allowlisted canary agent — so the legacy inline path is
                    # retired for the canary while every other agent keeps it. If the
                    # actuator is disabled (or not our lease), this is a safe no-op.
                    bridged = deliver_via_actuator(target, step_text, conv_id, cwd, ctrl)
                    if bridged.get("reason") in ("actuator_disabled", "stale_or_no_lease",
                                                 "already_verified", "lease_lost_midaction"):
                        _count_route_skip(h, bridged.get("reason"))
                        continue
                    out = {"verify": bridged.get("verify") or {"ok": bool(bridged.get("verified"))},
                           "retried": bool(bridged.get("retried"))}
                else:
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
