"""Native Claude Code `/goal` policy for managed sessions.

Routine continuation belongs INSIDE Claude: a session with a `/goal <condition>` keeps
working across turns until the condition holds, with no external "continue". Owner OS only
records which sessions have a goal policy, detects whether that goal is active in the
session's own transcript, and installs/refreshes it on an explicitly allowlisted session.

Rules:
  * Goals come ONLY from `config/native_goals.yaml` (explicit per-session condition). Nothing
    is ever invented, inferred or widened. A session absent from the file gets nothing.
  * DISABLED BY DEFAULT (`NATIVE_GOALS_ENABLED=0`): `install` refuses; detection is read-only.
  * HARD DENY beats config: `payorch-ha-fresh` (and anything in NATIVE_GOALS_DENY) can never
    receive a goal, whatever the file says.
  * Install = typing the slash command into the session's own prompt (`agent_control.
    agent_send`), because a slash command is only executed when it is the session's own
    input — a cross-session message is read literally and would not run it. This is the one
    keystroke path left, used for goal installation only, never for coordination.
  * Exactly once per (session, condition): a durable ledger row is latched BEFORE the send;
    a replay finds the latch and does nothing. A changed condition (new hash) installs once
    more; re-installing the SAME condition after it was cleared needs its ledger row removed
    deliberately (an unknown-outcome send is never repeated automatically).
  * Only into an idle session located through `claude agents --json` + an unambiguous tmux
    join; `agent_send` itself refuses a mid-turn pane.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Callable, Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

CONFIG = os.getenv("NATIVE_GOALS_CONFIG",
                   os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "config", "native_goals.yaml"))
HARD_DENY = frozenset({"payorch-ha-fresh"})
MAX_CONDITION_CHARS = 600
SOURCE = "native_goals"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_goal (
    session TEXT, condition_hash TEXT, condition TEXT, target TEXT, session_id TEXT,
    latched_ts REAL, latched_at TEXT, finished INTEGER DEFAULT 0, delivered INTEGER,
    evidence TEXT, PRIMARY KEY (session, condition_hash));
"""


def enabled() -> bool:
    return os.getenv("NATIVE_GOALS_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def denied(session: str) -> bool:
    extra = {s.strip() for s in os.getenv("NATIVE_GOALS_DENY", "").split(",") if s.strip()}
    base = (session or "").split(":")[0]
    return base in HARD_DENY or base in extra


def load_config(path: Optional[str] = None) -> dict:
    """{session: {condition, enabled}} — only well-formed, enabled, non-denied entries."""
    import yaml
    try:
        with open(path or CONFIG) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    out = {}
    for name, ent in (raw.get("sessions") or {}).items():
        if not isinstance(ent, dict) or not ent.get("enabled") or denied(str(name)):
            continue
        cond = " ".join(str(ent.get("condition") or "").split())
        if not cond or len(cond) > MAX_CONDITION_CHARS or cond.lower() == "clear":
            continue
        out[str(name)] = {"condition": cond}
    return out


def condition_hash(cond: str) -> str:
    return hashlib.sha256(cond.encode()).hexdigest()[:16]


def _transcript(cwd: str, session_id: str) -> str:
    enc = (cwd or "").replace("/", "-").replace(".", "-")
    return os.path.join(os.path.expanduser("~/.claude/projects"), enc, f"{session_id}.jsonl")


def goal_state(cwd: str, session_id: str, *, path: Optional[str] = None) -> dict:
    """Goal state from this session's own transcript (read-only; only goal records are
    parsed, never conversation text). Claude Code 2.1.310 records:
      * `/goal <cond>` typed as input → a `user` (or `system/local_command`) record whose
        content carries `<command-name>/goal</command-name><command-args>…`;
      * each Stop-hook evaluation → an `attachment` of type `goal_status` with `met`, and
        `failed: true` when the evaluator gives up (it bounds its own iterations);
      * `/goal clear` → a `system/local_command` record with args `clear`.
    Returns {active, condition, met, failed, iterations}."""
    st = {"active": False, "condition": None, "met": False, "failed": False,
          "iterations": None}
    p = path or _transcript(cwd, session_id)
    try:
        fh = open(p, encoding="utf-8", errors="replace")
    except OSError:
        return st
    with fh:
        for line in fh:
            if "/goal</command-name>" not in line and '"goal_status"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            att = d.get("attachment") or {}
            if att.get("type") == "goal_status":
                if att.get("met") is True or att.get("failed") is True:
                    st.update(active=False, met=att.get("met") is True,
                              failed=att.get("failed") is True,
                              iterations=att.get("iterations"))
                continue
            content = d.get("content") if d.get("type") == "system" else \
                (d.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            content = content or ""
            if "<command-name>/goal</command-name>" not in content or \
                    d.get("type") not in ("user", "system"):
                continue
            args = content.split("<command-args>", 1)[-1].split("</command-args>", 1)[0] \
                if "<command-args>" in content else ""
            args = " ".join(args.split())
            if args.lower() in ("", "clear"):
                st.update(active=False, condition=None)
            else:
                st = {"active": True, "condition": args, "met": False, "failed": False,
                      "iterations": None}
    return st


def active_goal(cwd: str, session_id: str, *, path: Optional[str] = None) -> Optional[str]:
    """The goal condition currently in force, or None (never set, cleared, met or given up)."""
    st = goal_state(cwd, session_id, path=path)
    return st["condition"] if st["active"] else None


def status(listing: dict, config: Optional[dict] = None) -> list:
    """Per configured session: located?, goal active?, matches config? (read-only)."""
    from core import native_agents as na
    rows = []
    for name, ent in (config if config is not None else load_config()).items():
        s = na.find(name, listing)
        gs = goal_state(s["cwd"], s["session_id"]) if s and s.get("session_id") else {}
        act = gs.get("condition") if gs.get("active") else None
        rows.append({"session": name, "located": bool(s), "native_name": (s or {}).get("name"),
                     "tmux_target": (s or {}).get("tmux_target"),
                     "state": (s or {}).get("state"), "goal_active": bool(act),
                     "goal_matches_config": act == ent["condition"],
                     "goal_met": bool(gs.get("met")), "goal_failed": bool(gs.get("failed")),
                     "source": listing.get("source")})
    return rows


def install(name: str, *, listing: dict, config: Optional[dict] = None,
            send: Optional[Callable] = None, conn=None) -> dict:
    """Install / refresh the configured goal on ONE allowlisted session. Idempotent."""
    if not enabled():
        return {"ok": False, "reason": "native_goals_disabled"}
    if denied(name):
        return {"ok": False, "reason": "session_hard_denied"}
    cfg = config if config is not None else load_config()
    ent = cfg.get(name)
    if not ent:
        return {"ok": False, "reason": "no_goal_policy_for_session"}
    from core import native_agents as na
    if listing.get("source") != na.SOURCE_NATIVE:
        return {"ok": False, "reason": "native_state_unavailable"}
    s = na.find(name, listing)
    if not s or not s.get("tmux_target") or not s.get("session_id"):
        return {"ok": False, "reason": "session_not_located_unambiguously"}
    if s["state"] != "idle":
        return {"ok": False, "reason": f"session_not_idle:{s['state']}"}
    cond = ent["condition"]
    if active_goal(s["cwd"], s["session_id"]) == cond:
        return {"ok": True, "reason": "goal_already_active", "sent": False}
    h = condition_hash(cond)
    conn, own = _c(conn)
    try:
        conn.executescript(_SCHEMA)
        cur = conn.execute(
            "INSERT OR IGNORE INTO native_goal (session,condition_hash,condition,target,"
            "session_id,latched_ts,latched_at) VALUES (?,?,?,?,?,?,?)",
            (name, h, cond, s["tmux_target"], s["session_id"], now_ts(), now_iso()))
        conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "reason": "goal_install_already_latched", "sent": False}
        if send is None:
            from core import agent_control as ac

            def send(target, text, key):
                return ac.agent_send(target, text, idempotency_key=key, actor=SOURCE,
                                     source=SOURCE)
        try:
            res = send(s["tmux_target"], f"/goal {cond}", f"goal:{name}:{h}")
        except Exception as e:  # noqa: BLE001 — outcome unknown: latched, never resent
            res = {"delivered": False, "error": type(e).__name__}
        ok = bool(res.get("submitted") or res.get("delivered"))
        conn.execute("UPDATE native_goal SET finished=1, delivered=?, evidence=? WHERE "
                     "session=? AND condition_hash=?",
                     (1 if ok else 0, json.dumps(res, default=str)[:2000], name, h))
        conn.commit()
        return {"ok": ok, "reason": "goal_sent" if ok else "goal_send_failed", "sent": True,
                "target": s["tmux_target"], "result": res}
    finally:
        if own:
            conn.close()
