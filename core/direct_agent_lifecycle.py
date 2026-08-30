"""Direct-agent lifecycle watcher — reliable completion / owner-action /
interruption events for DIRECT tmux agents (a manually-started Claude pane that
is NOT part of the orchestrator plan, e.g. ezetta-video, ACAP/Mess).

Why this exists (the confirmed 2026-07 defect): /opt/ezetta-video finished all
six masters and updated its reports, yet NO completion event was ever created.
The orchestrator's inline `agent_watcher.transition_event` is structurally unable
to cover a direct agent in two cases:

  1. BASELINE completion — `transition_event` returns None when there is no prior
     state (a resumed/first-seen agent that is ALREADY done at first observation
     never emits). An agent that completes before the watcher first records it is
     therefore lost forever. This is the ezetta miss.
  2. DEAD / VANISHED agents — the orchestrator sweep `continue`s on any pane that
     is not (`is_agent and alive`) BEFORE it computes a transition, and the
     dead-agent block only raises an event for orchestrator-PLAN agents that have
     an assigned unfinished task. A direct agent that is SIGKILLed or simply exits
     produces nothing — and must never be mislabelled as completed.

Ownership split (no double-notify): the existing inline path keeps owning every
ALIVE state transition for CONFIGURED sessions AND the normal active→completed
transition for direct sessions. This module owns ONLY what inline cannot:
baseline completion (with credible durable evidence) and interruption on death.
For the normal active→completed case it CEDES to inline (records the completion
so it is not re-emitted, but sends nothing). Everything routes through the
existing durable Commander event log (`record_commander_event`) — there is no
parallel notifier and no parallel history.

The decision core is PURE and fully injectable for tests; the sweep wires the
real tmux inventory, report scan, conversation history and event sink into it.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# ── tunables (env-overridable) ───────────────────────────────────────────────
ENABLED = os.getenv("DIRECT_AGENT_LIFECYCLE_ENABLED", "1") not in ("0", "false", "no", "")
# An idle agent must stay idle at least this long (across observations) before a
# NON-baseline idle→completed is trusted — debounces a false idle that appears
# between two tool calls.
IDLE_DWELL_SECS = int(os.getenv("DIRECT_LIFECYCLE_IDLE_DWELL_SECS", "20"))
# A report only counts as completion evidence if it was written within this window
# of the observation — so an OLD idle session (finished long ago) is never
# retro-notified on first observation.
COMPLETION_WINDOW_SECS = int(os.getenv("DIRECT_LIFECYCLE_COMPLETION_WINDOW_SECS", "1800"))
# Stored observations older than this with no matching live pane are pruned.
OBS_TTL_SECS = int(os.getenv("DIRECT_LIFECYCLE_OBS_TTL_SECS", "604800"))

_ACTIVE_STATES = ("working", "shell_running")
# Markers Claude Code shows WHILE a turn/tool runs. Their presence means an "idle"
# base classification is a lag artefact and MUST NOT be read as completion.
_ACTIVE_EXEC_RE = re.compile(
    r"(esc to interrupt|\bthinking[.…]|\brunning[.…]|\bcompacting|\btool call|"
    r"\bexecuting\b|✻|✽)", re.I)

# Event types — reuse the SAME vocabulary the notifier already renders/delivers.
EVENT_COMPLETED = "agent_completed"
EVENT_WAITING_OWNER = "agent_owner_decision"
EVENT_INTERRUPTED = "agent_process_failed"   # death/interruption — never completion


# ── persistence: last observation + emitted-completion flag (restart-safe) ───
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS direct_agent_lifecycle (
        target TEXT PRIMARY KEY, conversation_id TEXT, cwd TEXT, state TEXT,
        alive INTEGER, first_idle_ts REAL, last_seen_ts REAL,
        completion_emitted INTEGER DEFAULT 0, last_report_path TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS direct_agent_lifecycle_metrics (
        name TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    conn.commit()
    return conn


_OBS_FIELDS = ("target", "conversation_id", "cwd", "state", "alive", "first_idle_ts",
               "last_seen_ts", "completion_emitted", "last_report_path", "updated_at")


def get_obs(target: str, *, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    own = conn is None
    conn = conn or _db()
    try:
        row = conn.execute(
            f"SELECT {','.join(_OBS_FIELDS)} FROM direct_agent_lifecycle WHERE target=?",
            (target,)).fetchone()
        if not row:
            return None
        rec = dict(zip(_OBS_FIELDS, row))
        rec["alive"] = bool(rec["alive"])
        rec["completion_emitted"] = bool(rec["completion_emitted"])
        return rec
    finally:
        if own:
            conn.close()


def save_obs(rec: dict, *, conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    conn = conn or _db()
    try:
        rec = dict(rec)
        rec["updated_at"] = _now_iso()
        rec["alive"] = 1 if rec.get("alive") else 0
        rec["completion_emitted"] = 1 if rec.get("completion_emitted") else 0
        cols = ",".join(_OBS_FIELDS)
        ph = ",".join("?" for _ in _OBS_FIELDS)
        conn.execute(f"INSERT OR REPLACE INTO direct_agent_lifecycle ({cols}) VALUES ({ph})",
                     tuple(rec.get(f) for f in _OBS_FIELDS))
        conn.commit()
    finally:
        if own:
            conn.close()


def bump_metric(name: str, n: int = 1, *, conn: Optional[sqlite3.Connection] = None) -> None:
    own = conn is None
    conn = conn or _db()
    try:
        conn.execute(
            "INSERT INTO direct_agent_lifecycle_metrics(name,value) VALUES(?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+?", (name, n, n))
        conn.commit()
    finally:
        if own:
            conn.close()


def metrics() -> dict:
    conn = _db()
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT name,value FROM direct_agent_lifecycle_metrics").fetchall()}
    finally:
        conn.close()


# ── helpers ──────────────────────────────────────────────────────────────────
def _now_ts() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_fingerprint(path: str, modified_at: str) -> str:
    return hashlib.sha256(f"{path}\x1f{modified_at}".encode()).hexdigest()[:16]


def _summary(tail: str, limit: int = 220) -> str:
    """A short sanitized one-line summary from the pane tail (already redacted
    upstream). The newest non-empty line, trimmed."""
    for line in reversed((tail or "").splitlines()):
        s = line.strip()
        if s:
            return s[:limit]
    return ""


def build_observation(agent: dict, *, now_ts: float, reports: Optional[list] = None,
                      conversation_id: Optional[str] = None,
                      vanished: bool = False) -> dict:
    """Normalize a raw agent inventory dict into a lifecycle observation."""
    state = agent.get("state") or "idle"
    return {
        "target": agent["target"],
        "alive": bool(agent.get("alive")) and not vanished,
        "vanished": vanished,
        "state": state,
        "cwd": agent.get("claude_cwd") or agent.get("cwd") or "",
        "tail": agent.get("_tail") or agent.get("recent_activity") or "",
        "child_running": state == "shell_running" or bool(agent.get("shell_running")),
        "conversation_id": conversation_id,
        "reports": reports or [],
        "now_ts": now_ts,
    }


def _fresh_report(cur: dict, *, window_secs: int = COMPLETION_WINDOW_SECS) -> Optional[dict]:
    """The newest report if it was written within the completion window, else None
    (so a stray/old report can never fake a finish)."""
    reps = cur.get("reports") or []
    if not reps:
        return None
    latest = reps[0]
    try:
        mt = datetime.fromisoformat(latest["modified_at"]).timestamp()
    except Exception:  # noqa: BLE001
        return None
    if (cur["now_ts"] - mt) > window_secs:
        return None
    return {"report_path": latest["path"], "modified_at": latest["modified_at"],
            "newest_reports": [r.get("path") for r in reps[:3]]}


def completion_evidence(cur: dict, *, window_secs: int = COMPLETION_WINDOW_SECS) -> Optional[dict]:
    """Credible multi-signal completion evidence for a LIVE idle pane, or None
    (fail closed):
      1. genuinely idle (no live child command),
      2. no active-execution markers in the pane (not a mid-turn lag),
      3. a report written within the completion window (freshness).
    Used to recognise a completed session so a later clean exit is not mislabelled
    an interruption; the live working→idle→completed NOTIFICATION itself is owned
    by the inline transition path (this module does not double it)."""
    if cur.get("state") != "idle":
        return None
    if cur.get("child_running"):
        return None
    if _ACTIVE_EXEC_RE.search(cur.get("tail") or ""):
        return None
    return _fresh_report(cur, window_secs=window_secs)


def _completed_event(cur: dict, ev: dict, *, from_state: str, exited: bool = False) -> dict:
    conv = cur.get("conversation_id") or ""
    payload = {
        "target": cur["target"], "cwd": cur.get("cwd"), "conversation_id": conv,
        "event_time": _now_iso(), "from_state": from_state,
        "to_state": ("exited_after_completion" if exited else "completed"),
        "summary": _summary(cur.get("tail") or ""),
        "report_path": ev["report_path"], "newest_reports": ev.get("newest_reports"),
        "owner_action_required": False, "source": "direct_agent_lifecycle",
    }
    if exited:
        payload["note"] = "agent finished (fresh report) then exited cleanly"
    return {"kind": "completed", "event_type": EVENT_COMPLETED, "notify": True,
            "payload": payload,
            "dedup_key": f"dlc:completed:{cur['target']}:{conv}:"
                         f"{_report_fingerprint(ev['report_path'], ev['modified_at'])}"}


def _interrupted_event(cur: dict, prev: dict) -> dict:
    conv = prev.get("conversation_id") or ""
    fr = _fresh_report(cur)
    payload = {
        "target": cur["target"], "cwd": cur.get("cwd") or prev.get("cwd"),
        "conversation_id": conv, "event_time": _now_iso(),
        "from_state": prev.get("state"), "to_state": ("vanished" if cur.get("vanished") else "dead"),
        "summary": _summary(cur.get("tail") or ""),
        "newest_reports": fr.get("newest_reports") if fr else None,
        "owner_action_required": True, "classification": "interruption",
        "note": "agent pane died/vanished with in-flight work — NOT a completion",
        "source": "direct_agent_lifecycle",
    }
    return {"kind": "interrupted", "event_type": EVENT_INTERRUPTED, "notify": True,
            "payload": payload,
            "dedup_key": f"dlc:interrupted:{cur['target']}:{conv}"}


# ── pure decision ────────────────────────────────────────────────────────────
def decide(prev: Optional[dict], cur: dict) -> dict:
    """Decide the ONE lifecycle outcome for a direct agent. Returns
    {"metric": <counter>, "event"?: <event>, "completed"?: bool, "reset"?: bool}.

    Ownership (no double-notify): the inline `transition_event` path already
    reliably emits completion / owner-decision / waiting for a direct agent on any
    observed ALIVE transition (it re-checks evidence every tick). This module owns
    ONLY the DEAD/VANISHED pane, which the orchestrator sweep skips before it can
    compute a transition:
      * a pane that finished (fresh report, not killed mid-run) then exited cleanly
        → a COMPLETION the inline path never saw (the ezetta-class miss);
      * a pane that died with in-flight work / no evidence → an INTERRUPTION,
        never a completion (SIGKILL included).
    For an ALIVE pane it stays SILENT — it only RECORDS the observation (and marks
    a recognised completion) so the dead-path can tell a clean finish from a crash.
    Baseline / first observation is ALWAYS silent (an existing idle session is
    never retro-notified).
    """
    now = cur["now_ts"]

    # ── death / vanish — the inline path's blind spot ────────────────────────
    if cur.get("vanished") or not cur.get("alive"):
        if prev is None:
            return {"metric": "baseline_silenced"}       # first sight already dead — no retro alert
        if prev.get("completion_emitted"):
            return {"metric": "dead_after_completion_ignored"}  # already announced complete — benign
        ev = _fresh_report(cur)
        st = prev.get("state") or ""
        killed_midrun = bool(cur.get("tail")) and bool(_ACTIVE_EXEC_RE.search(cur.get("tail") or ""))
        was_active = st in _ACTIVE_STATES
        # Clean finish-then-exit: a fresh report, NOT killed mid-run, and not a
        # pane that simply vanished while still actively working.
        if ev and not killed_midrun and not (cur.get("vanished") and was_active):
            return {"metric": "completion_candidate", "completed": True,
                    "event": _completed_event(cur, ev, from_state=st or "exited", exited=True)}
        # Otherwise, if it had in-flight/unfinished work → interruption (never
        # completion). A long-dead pane with no prior work is just noise.
        if was_active or st == "idle" or st.startswith("waiting") \
                or st in ("externally_blocked", "failed"):
            return {"metric": "dead_candidate", "event": _interrupted_event(cur, prev)}
        return {"metric": "dead_candidate"}

    # ── ALIVE — record only; inline owns every live transition/notification ───
    # A resumed / brand-new conversation re-baselines (prior state must not leak).
    conv_changed = bool(prev and cur.get("conversation_id") and prev.get("conversation_id")
                        and prev["conversation_id"] != cur["conversation_id"])
    ev = completion_evidence(cur)
    if ev:
        # A recognised live completion — record it (silent; inline notifies) so a
        # later clean exit is NOT mislabelled an interruption.
        return {"metric": "completion_recognised", "completed": True, "reset": conv_changed}
    if cur.get("state") == "idle" and prev and prev.get("state") in _ACTIVE_STATES:
        # went quiet with no durable artefact — fail closed (no completion claim).
        return {"metric": "insufficient_evidence_suppressed", "reset": conv_changed}
    if cur.get("state") == "shell_running" or _ACTIVE_EXEC_RE.search(cur.get("tail") or ""):
        return {"metric": "false_idle_debounced" if cur.get("state") == "idle" else "noop",
                "reset": conv_changed}
    return {"metric": "baseline_silenced" if prev is None else "noop", "reset": conv_changed}


def _next_first_idle(prev: Optional[dict], cur: dict) -> Optional[float]:
    """Track when the pane FIRST became idle (for the dwell), reset on any activity."""
    if cur.get("state") != "idle":
        return None
    if prev and prev.get("state") == "idle" and prev.get("first_idle_ts"):
        return prev["first_idle_ts"]
    return cur["now_ts"]


# ── sweep (impure wiring) ────────────────────────────────────────────────────
def sweep(inventory: dict, *, configured_sessions: Optional[Iterable[str]] = None,
          now_ts: Optional[float] = None,
          report_fn: Optional[Callable] = None,
          conversation_fn: Optional[Callable] = None,
          tail_fn: Optional[Callable] = None,
          emit_fn: Optional[Callable] = None) -> dict:
    """One lifecycle sweep over DIRECT agents (sessions NOT in the orchestrator
    config). Emits baseline-completion / interruption events the inline path
    cannot, routed through the durable Commander event log. Restart-safe and
    idempotent: last observation + emitted-completion flag are persisted, and the
    event sink dedups by (agent, event_type, dedup_key)."""
    if not ENABLED:
        return {"enabled": False}
    now_ts = now_ts if now_ts is not None else _now_ts()

    if configured_sessions is None:
        try:
            from core import agent_orchestrator as _orch
            cfg = _orch.load_config()
            configured_sessions = set((cfg.get("sessions") or {}).keys())
        except Exception:  # noqa: BLE001
            configured_sessions = set()
    configured = set(configured_sessions or ())

    if report_fn is None or conversation_fn is None or emit_fn is None:
        from core import agent_control as _ac
        if report_fn is None:
            def report_fn(cwd):  # noqa: E306
                try:
                    return (_ac.agent_report(cwd, limit=5) or {}).get("reports") or []
                except Exception:  # noqa: BLE001
                    return []
        if conversation_fn is None:
            def conversation_fn(cwd):  # noqa: E306
                try:
                    ev = _ac.conversation_evidence(cwd) or {}
                    return (ev.get("latest") or {}).get("conversation_id")
                except Exception:  # noqa: BLE001
                    return None
        if tail_fn is None:
            def tail_fn(target):  # noqa: E306
                try:
                    return _ac._pane_tail(target, 40)
                except Exception:  # noqa: BLE001
                    return ""
        if emit_fn is None:
            emit_fn = _ac.record_commander_event

    agents = inventory.get("agents") or []
    # Only DIRECT agents (a real Claude pane not governed by the orchestrator plan).
    direct = [a for a in agents
              if a.get("is_agent") and (a.get("session") or a["target"].split(":", 1)[0]) not in configured]
    live_targets = {a["target"] for a in direct}

    conn = _db()
    summary = {"enabled": True, "observed": 0, "events": [], "metrics_delta": {}}

    def _count(name, n=1):
        bump_metric(name, n, conn=conn)
        summary["metrics_delta"][name] = summary["metrics_delta"].get(name, 0) + n

    try:
        for a in direct:
            summary["observed"] += 1
            _count("agents_observed")
            cwd = a.get("claude_cwd") or a.get("cwd") or ""
            alive = bool(a.get("alive"))
            state = a.get("state") or "idle"
            # Reports are the completion signal — scan for a live idle pane AND for a
            # dead/exited pane (to tell a clean finish-then-exit from a crash).
            want_reports = cwd and ((alive and state == "idle") or not alive)
            reports = report_fn(cwd) if want_reports else []
            conv = conversation_fn(cwd) if cwd else None
            if alive and tail_fn is not None and not a.get("_tail"):
                a = {**a, "_tail": tail_fn(a["target"])}
            cur = build_observation(a, now_ts=now_ts, reports=reports, conversation_id=conv)
            prev = get_obs(a["target"], conn=conn)

            outcome = decide(prev, cur)
            _emit_outcome(outcome, cur, prev, summary, emit_fn, _count)
            _persist(cur, prev, outcome, conn)

        # Vanished direct agents: a target we recorded ALIVE last sweep but that is
        # gone from the inventory now → interruption if it had in-flight work.
        stored = conn.execute(
            "SELECT target FROM direct_agent_lifecycle WHERE alive=1").fetchall()
        for (target,) in stored:
            if target in live_targets:
                continue
            session = target.split(":", 1)[0]
            if session in configured:
                continue
            prev = get_obs(target, conn=conn)
            if not prev:
                continue
            vcwd = prev.get("cwd") or ""
            vreports = report_fn(vcwd) if vcwd else []
            cur = build_observation({"target": target, "alive": False,
                                     "cwd": vcwd, "state": prev.get("state")},
                                    now_ts=now_ts, vanished=True, reports=vreports,
                                    conversation_id=prev.get("conversation_id"))
            _count("agents_observed")
            outcome = decide(prev, cur)
            _emit_outcome(outcome, cur, prev, summary, emit_fn, _count)
            _persist(cur, prev, outcome, conn)

        # prune ancient dead rows
        conn.execute("DELETE FROM direct_agent_lifecycle WHERE alive=0 AND last_seen_ts < ?",
                     (now_ts - OBS_TTL_SECS,))
        conn.commit()
    finally:
        conn.close()
    return summary


def _emit_outcome(outcome, cur, prev, summary, emit_fn, count) -> None:
    count(outcome["metric"])
    ev = outcome.get("event")
    if not ev:
        return
    try:
        is_new = emit_fn(cur["target"], (cur.get("cwd") or ""), ev["event_type"],
                         ev["payload"], dedup_key=ev["dedup_key"], dedup_window_secs=86400)
    except Exception:  # noqa: BLE001
        count("emit_error")
        return
    if is_new:
        count("delivered")
        if ev["kind"] == "completed":
            count("completions_emitted")
        elif ev["kind"] == "interrupted":
            count("interruptions_emitted")
        summary["events"].append({"target": cur["target"], "event_type": ev["event_type"],
                                  "kind": ev["kind"], "dedup_key": ev["dedup_key"]})
    else:
        count("duplicate_suppressed")


def _persist(cur, prev, outcome, conn) -> None:
    completion_emitted = bool(prev and prev.get("completion_emitted"))
    if outcome.get("reset"):
        completion_emitted = False               # new conversation → fresh baseline
    if outcome.get("completed"):
        completion_emitted = True
    rec = {
        "target": cur["target"],
        "conversation_id": cur.get("conversation_id") or (prev or {}).get("conversation_id"),
        "cwd": cur.get("cwd") or (prev or {}).get("cwd"),
        "state": cur.get("state"),
        "alive": cur.get("alive"),
        "first_idle_ts": _next_first_idle(prev, cur),
        "last_seen_ts": cur["now_ts"],
        "completion_emitted": completion_emitted,
        "last_report_path": (outcome.get("event") or {}).get("payload", {}).get("report_path")
                            or (prev or {}).get("last_report_path"),
    }
    save_obs(rec, conn=conn)
