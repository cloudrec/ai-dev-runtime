"""Context budget / checkpoint / rotation for managed agents.

The critical conversations are already huge (2026-08-03 live: arbitrage2 21.1 MB,
owneros 26.0 MB single-session .jsonl). This module tracks, for every agent in the
commander-autopilot registry, the size of the CURRENT Claude conversation (the latest
.jsonl for the project — a new file starts after /clear, so it is a faithful proxy for
the accumulating context) plus the observable phase state, and — at a SAFE phase
boundary, BEFORE context becomes excessive — writes an ATOMIC, verified checkpoint and
rotates (clears) the conversation, resuming the SAME project from the checkpoint with
no duplicate agent and no duplicate rotation.

SAFETY MODEL
  * ROTATION NEVER RUNS while any of these hold (phase guard, checked immediately
    before acting): agent working / shell command running / a background Fable
    subagent running / a tool-permission dialog open (waiting_owner) / text pending
    in the input line (the /clear-concatenation hazard) / active-execution markers.
  * The checkpoint is written ATOMICALLY (tmp + fsync + rename) and then RE-READ and
    verified (all required sections present, HEAD + NEXT COMMAND non-empty). A failed
    or missing checkpoint REFUSES rotation.
  * /clear and the resume step are delivered ONLY through the canonical lease-gated
    Actuator — confined to CONTROL_PLANE_CANARY_AGENTS. A non-canary agent over
    budget produces an owner-gated `context_rotation_needed` event, never actuation.
  * DUPLICATE PREVENTION: one rotation per (target, old conversation). The ledger is
    durable (restart-safe) and records the old→new conversation-id mapping plus the
    verified-delivery receipts.

THRESHOLDS (env-overridable, read at call time)
  CONTEXT_BUDGET_SOFT_BYTES  default  8_000_000 (~2.0M transcript tokens est. @4 B/tok)
      → at a safe boundary, write + verify a checkpoint (no rotation yet).
  CONTEXT_BUDGET_HARD_BYTES  default 16_000_000 (~4.0M transcript tokens est.)
      → checkpoint + rotate at the next safe boundary (canary-confined actuation).
  CONTEXT_BUDGET_ENABLED (default on) gates the loop; rotation additionally requires
  the Actuator to be enabled and the target allowlisted.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable, Optional

ENABLED = os.getenv("CONTEXT_BUDGET_ENABLED", "1") not in ("0", "false", "no", "")
INTERVAL = int(os.getenv("CONTEXT_BUDGET_INTERVAL_SECS", "120"))
_EST_BYTES_PER_TOKEN = 4


def soft_limit_bytes() -> int:
    return int(os.getenv("CONTEXT_BUDGET_SOFT_BYTES", "8000000"))


def hard_limit_bytes() -> int:
    return int(os.getenv("CONTEXT_BUDGET_HARD_BYTES", "16000000"))


def checkpoint_dir() -> str:
    return os.getenv("CONTEXT_BUDGET_CHECKPOINT_DIR", "/root/ai-dev-runtime/checkpoints")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── persistence ──────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db"), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS context_budget_state (
        target TEXT PRIMARY KEY, conversation_id TEXT, size_bytes INTEGER,
        est_tokens INTEGER, state TEXT, safe_boundary INTEGER, over_soft INTEGER,
        over_hard INTEGER, reasons TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS context_rotation (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, target TEXT,
        old_conversation_id TEXT, new_conversation_id TEXT, checkpoint_path TEXT,
        size_bytes INTEGER, status TEXT, note TEXT)""")
    conn.commit()
    return conn


# ── measurement ──────────────────────────────────────────────────────────────
def measure(cwd: str, *, conv_evidence_fn: Optional[Callable] = None) -> dict:
    """Size of the CURRENT (latest) conversation for a project dir. Read-only."""
    if conv_evidence_fn is None:
        from core import agent_control as ac
        conv_evidence_fn = ac.conversation_evidence
    latest = (conv_evidence_fn(cwd) or {}).get("latest") or {}
    size = int(latest.get("size_bytes") or 0)
    return {
        "conversation_id": latest.get("conversation_id"),
        "size_bytes": size,
        "est_tokens": size // _EST_BYTES_PER_TOKEN,
        "modified_at": latest.get("modified_at"),
        "over_soft": size >= soft_limit_bytes(),
        "over_hard": size >= hard_limit_bytes(),
    }


# ── phase safety ─────────────────────────────────────────────────────────────
def phase(state: str, tail: str, pending: str) -> dict:
    """Is this a SAFE boundary to checkpoint/rotate? NEVER safe while a shell
    command / test / build runs, a background Fable/subagent is active, a
    tool-permission dialog is open, active-execution markers show, or unsubmitted
    text sits in the input line (the /clear-concatenation hazard)."""
    from core.agent_control import _STATE_ACTIVE_RUN_RE
    from core.commander_autopilot import has_background_subagent
    reasons = []
    if state in ("working", "shell_running"):
        reasons.append(f"state_{state}")
    if state == "waiting_owner":
        reasons.append("permission_dialog_open")
    if state == "waiting_input":
        reasons.append("unsubmitted_input_state")
    if state in ("dead", "stale"):
        reasons.append(f"state_{state}")
    if _STATE_ACTIVE_RUN_RE.search(tail or ""):
        reasons.append("active_execution_marker")
    if has_background_subagent(tail or ""):
        reasons.append("background_subagent_running")
    if (pending or "").strip():
        reasons.append("pending_input_line")
    return {"state": state, "safe_boundary": not reasons, "reasons": reasons}


# ── checkpoint ───────────────────────────────────────────────────────────────
REQUIRED_SECTIONS = ("PROJECT", "CURRENT STATE", "COMPLETED", "HEAD", "TESTS",
                     "BACKGROUND JOBS", "PENDING TASKS", "BLOCKERS", "SAFETY GATES",
                     "NEXT COMMAND")


def _git_head(root: str) -> str:
    try:
        import subprocess
        out = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                             capture_output=True, timeout=10)
        sha = out.stdout.decode().strip()
        return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_checkpoint(target: str, entry: dict, *, state: str, tail: str,
                     measurement: dict, head: Optional[str] = None) -> str:
    """Render the checkpoint markdown from OBSERVABLE facts (registry + git + pane)."""
    from core.commander_autopilot import parse_task_footer, has_background_subagent
    tf = parse_task_footer(tail)
    head = head if head is not None else _git_head(entry.get("root", ""))
    tasks = (f"{tf['done']} done, {tf['in_progress']} in progress, {tf['open']} open"
             if tf["present"] else "no task footer visible")
    task_lines = "\n".join(
        ln.strip() for ln in (tail or "").splitlines()
        if re.match(r"\s*[◼◻☐✔]", ln)) or "(none captured)"
    bg = ("a background subagent/Fable is RUNNING — rotation must NOT proceed"
          if has_background_subagent(tail) else "none detected")
    blockers = ("permission dialog / owner decision pending" if state == "waiting_owner"
                else "none observed")
    return f"""# CONTEXT CHECKPOINT — {target}
Generated {_now_iso()} by context_budget (atomic, verified before rotation).

## PROJECT
{entry.get('project', target)} — root {entry.get('root', 'unknown')}
End-state: {entry.get('end_state', '(not documented)')}

## CURRENT STATE
Observable state: {state}. Conversation {measurement.get('conversation_id')} —
{measurement.get('size_bytes')} bytes (~{measurement.get('est_tokens')} est tokens).
Pane tail tip: {(tail or '')[-400:].strip() or '(empty)'}

## COMPLETED
Task footer: {tasks}
{task_lines}

## HEAD
{head}

## TESTS
{entry.get('tests', 'run the project test suite (python -m pytest -q) and record the result')}

## BACKGROUND JOBS
{bg}

## PENDING TASKS
{tf['open'] if tf['present'] else 'unknown'} open, {tf['in_progress'] if tf['present'] else 'unknown'} in progress (see COMPLETED list above for exact items).

## BLOCKERS
{blockers}

## SAFETY GATES
No payments/trading/promotion/credentials/publication/push/deploy/destructive actions.
Actuation only via the lease-gated Actuator, confined to CONTROL_PLANE_CANARY_AGENTS.
Owner gates must not be auto-resolved.

## NEXT COMMAND
{entry.get('next_step', 'continue with the next safe step')}
"""


def write_checkpoint(target: str, content: str) -> str:
    """Atomic write: tmp file + fsync + rename. Returns the final path."""
    session = target.split(":", 1)[0]
    d = os.path.join(checkpoint_dir(), session)
    os.makedirs(d, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = os.path.join(d, f"checkpoint-{ts}.md")
    tmp = final + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final


def verify_checkpoint(path: str) -> dict:
    """Re-read the checkpoint and verify it is COMPLETE and READABLE: every required
    section present, HEAD and NEXT COMMAND non-empty. Rotation must refuse on failure."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError as e:
        return {"ok": False, "reason": f"unreadable:{e}"}
    missing = [s for s in REQUIRED_SECTIONS if f"## {s}" not in text]
    if missing:
        return {"ok": False, "reason": f"missing_sections:{','.join(missing)}"}
    for section in ("HEAD", "NEXT COMMAND"):
        m = re.search(rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
        if not m or not m.group(1).strip():
            return {"ok": False, "reason": f"empty_section:{section}"}
    return {"ok": True, "sections": len(REQUIRED_SECTIONS), "bytes": len(text)}


# ── rotation ─────────────────────────────────────────────────────────────────
def _already_rotated(conn, target: str, conversation_id: str) -> bool:
    r = conn.execute("SELECT 1 FROM context_rotation WHERE target=? AND "
                     "old_conversation_id=? AND status='rotated'",
                     (target, conversation_id or "")).fetchone()
    return bool(r)


def _record_rotation(conn, target, old_conv, new_conv, path, size, status, note="") -> int:
    cur = conn.execute(
        "INSERT INTO context_rotation(ts,target,old_conversation_id,new_conversation_id,"
        "checkpoint_path,size_bytes,status,note) VALUES(?,?,?,?,?,?,?,?)",
        (_now_iso(), target, old_conv or "", new_conv or "", path or "", size, status,
         str(note)[:500]))
    conn.commit()
    return cur.lastrowid


def _actuate(target: str, text: str, kind: str, conversation_id: str, cwd: str,
             ctrl=None, sleep=None) -> dict:
    from core.control_plane import actuator
    from core.control_plane import api as cp
    from core import agent_continuation_watchdog as cw
    lease = cp.acquire_lease(f"agent:{target}", "context_budget", ttl_secs=180)
    return actuator.actuate(target=target, action_text=text, controller="context_budget",
                            conversation_id=conversation_id, kind=kind, cwd=cwd,
                            lease=lease, ctrl=ctrl or cw.Controller(),
                            sleep=sleep or time.sleep)


def write_project_checkpoint_copy(root: str, content: str) -> Optional[str]:
    """Atomic, verified copy of the checkpoint INSIDE the project root, where the
    agent's own sandbox rules allow reading it. LIVE 2026-08-03 cp-canary: the agent
    refused the central /root/ai-dev-runtime/checkpoints/... path (outside its scope)
    and resumed from task.md instead — correct fallback, but the checkpoint content
    was unused. Returns the path, or None when the copy could not be written/verified
    (the resume then references the central path and carries the next command inline)."""
    try:
        if not root or not os.path.isdir(root):
            return None
        p = os.path.join(root, "CONTEXT_CHECKPOINT.md")
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
        return p if verify_checkpoint(p).get("ok") else None
    except OSError:
        return None


def _resume_text(path: str, next_cmd: str = "") -> str:
    nc = (f" the exact next command from the checkpoint is: {next_cmd}." if next_cmd else "")
    return (f"resume the SAME project from the checkpoint file {path}: read it fully first, "
            f"then continue with the exact NEXT COMMAND recorded there;{nc} do not repeat work "
            f"already listed as completed; never start a duplicate agent.")


def rotate(target: str, entry: dict, *, state: str, tail: str, pending: str,
           measurement: Optional[dict] = None, ctrl=None, sleep=None,
           conv_meta_fn: Optional[Callable] = None, force: bool = False,
           wait_new_conv_secs: float = 20.0, conn=None) -> dict:
    """Checkpoint + clear + resume for ONE agent. Every guard re-checked here.
    Returns a result dict with `rotated` bool and `reason` on refusal. Never raises
    for control flow, never touches a pane except through the lease-gated Actuator."""
    own = conn is None
    conn = conn or _db()
    cwd = entry.get("root", "")
    if conv_meta_fn is None:
        def conv_meta_fn():
            return measure(cwd)
    try:
        m = measurement or conv_meta_fn()
        old_conv = m.get("conversation_id") or ""

        # 1) threshold — rotation is for excessive context only (force = explicit
        #    owner/canary exercise with the same safety guards).
        if not m.get("over_hard") and not force:
            return {"rotated": False, "reason": "under_hard_threshold", "measurement": m}
        # 2) phase boundary — NEVER clear during active work of any kind.
        ph = phase(state, tail, pending)
        if not ph["safe_boundary"]:
            _record_rotation(conn, target, old_conv, "", "", m.get("size_bytes", 0),
                             "refused_unsafe_phase", ",".join(ph["reasons"]))
            return {"rotated": False, "reason": "unsafe_phase", "blocking": ph["reasons"]}
        # 3) duplicate prevention — one rotation per conversation, durable.
        if _already_rotated(conn, target, old_conv):
            return {"rotated": False, "reason": "already_rotated_this_conversation"}

        # 4) atomic checkpoint + verification. A failed/missing checkpoint REFUSES.
        content = build_checkpoint(target, entry, state=state, tail=tail, measurement=m)
        try:
            path = write_checkpoint(target, content)
        except OSError as e:
            _record_rotation(conn, target, old_conv, "", "", m.get("size_bytes", 0),
                             "refused_checkpoint_write_failed", e)
            return {"rotated": False, "reason": "checkpoint_write_failed", "error": str(e)}
        v = verify_checkpoint(path)
        if not v.get("ok"):
            _record_rotation(conn, target, old_conv, "", path, m.get("size_bytes", 0),
                             "refused_checkpoint_unverified", v.get("reason"))
            return {"rotated": False, "reason": "checkpoint_unverified",
                    "verify": v, "checkpoint": path}
        # 4b) project-root copy — the agent's sandbox may forbid the central path
        #     (live cp-canary 2026-08-03). Best-effort; the resume references it when
        #     verified, else the central path, and ALWAYS carries the exact next
        #     command inline so a missing/unreadable file cannot lose the handoff.
        proj_copy = write_project_checkpoint_copy(cwd, content)

        # 5) clear via the lease-gated Actuator (canary-confined; non-canary → not_canary).
        res_clear = _actuate(target, "/clear", "context_rotation_clear", old_conv, cwd,
                             ctrl=ctrl, sleep=sleep)
        if not res_clear.get("acted"):
            _record_rotation(conn, target, old_conv, "", path, m.get("size_bytes", 0),
                             f"refused_clear_{res_clear.get('reason')}", res_clear)
            return {"rotated": False, "reason": f"clear_not_delivered:{res_clear.get('reason')}",
                    "checkpoint": path, "actuation": res_clear}

        # 6) confirm the rotation actually happened: a NEW conversation id appears (or
        #    the same id shrank to a fresh file). Poll briefly.
        new_conv, deadline = "", time.time() + wait_new_conv_secs
        while time.time() < deadline:
            m2 = conv_meta_fn()
            cid = m2.get("conversation_id") or ""
            if cid and cid != old_conv:
                new_conv = cid
                break
            if cid == old_conv and m2.get("size_bytes", 0) < max(
                    1, int(0.1 * max(1, m.get("size_bytes", 1)))):
                new_conv = cid
                break
            (sleep or time.sleep)(1)

        # 7) resume the SAME project from the checkpoint (verified delivery). Right after
        #    /clear the pane may briefly still render activity, which trips the Actuator's
        #    false-idle guard — retry a few times while it settles (never more).
        res_resume = None
        resume_msg = _resume_text(proj_copy or path, entry.get("next_step", ""))
        for attempt in range(4):
            res_resume = _actuate(target, resume_msg, "context_rotation_resume",
                                  new_conv or f"post:{old_conv}", cwd, ctrl=ctrl, sleep=sleep)
            if res_resume.get("acted") or res_resume.get("reason") != "target_working":
                break
            (sleep or time.sleep)(3)
        status = "rotated" if res_resume.get("acted") else "rotated_resume_unverified"
        rid = _record_rotation(conn, target, old_conv, new_conv, path,
                               m.get("size_bytes", 0), status,
                               {"clear": res_clear.get("verified"),
                                "resume": res_resume.get("verified")})
        # durable receipt into the CTO inbox + legacy commander surface (best-effort).
        try:
            from core.control_plane.cto import emit
            emit("context_budget", "context_rotated", agent_id=target, severity="info",
                 payload={"rotation_id": rid, "old_conversation": old_conv,
                          "new_conversation": new_conv, "checkpoint": path,
                          "size_bytes": m.get("size_bytes"), "status": status},
                 action_taken=f"context rotated at {m.get('size_bytes')} bytes",
                 dedup_key=f"ctxrot:{target}:{old_conv}")
        except Exception:  # noqa: BLE001
            pass
        try:
            from core import agent_control as ac
            ac.record_commander_event(target, entry.get("project", ""), "context_rotated",
                                      {"rotation_id": rid, "old_conversation": old_conv,
                                       "new_conversation": new_conv, "checkpoint": path},
                                      dedup_key=f"ctxrot:{target}:{old_conv}",
                                      dedup_window_secs=604800)
        except Exception:  # noqa: BLE001
            pass
        return {"rotated": True, "rotation_id": rid, "checkpoint": path,
                "old_conversation": old_conv, "new_conversation": new_conv,
                "status": status, "clear": res_clear, "resume": res_resume}
    finally:
        if own:
            conn.close()


# ── sweep ────────────────────────────────────────────────────────────────────
def _save_state(conn, target, m, ph) -> None:
    conn.execute(
        "INSERT INTO context_budget_state(target,conversation_id,size_bytes,est_tokens,"
        "state,safe_boundary,over_soft,over_hard,reasons,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(target) DO UPDATE SET conversation_id=excluded.conversation_id,"
        "size_bytes=excluded.size_bytes,est_tokens=excluded.est_tokens,state=excluded.state,"
        "safe_boundary=excluded.safe_boundary,over_soft=excluded.over_soft,"
        "over_hard=excluded.over_hard,reasons=excluded.reasons,updated_at=excluded.updated_at",
        (target, m.get("conversation_id") or "", m.get("size_bytes", 0),
         m.get("est_tokens", 0), ph["state"], int(ph["safe_boundary"]),
         int(m.get("over_soft", False)), int(m.get("over_hard", False)),
         ",".join(ph["reasons"]), _now_iso()))
    conn.commit()


def _checkpointed_this_conv(conn, target: str, conv: str) -> bool:
    r = conn.execute("SELECT 1 FROM context_rotation WHERE target=? AND old_conversation_id=? "
                     "AND status IN ('checkpoint_only','rotated')",
                     (target, conv or "")).fetchone()
    return bool(r)


def tick(*, registry: Optional[dict] = None, inventory: Optional[dict] = None,
         tail_fn=None, pending_fn=None, measure_fn=None, ctrl=None, sleep=None,
         rotate_enabled: bool = True, conn=None) -> dict:
    """One pass over the critical-agent registry: measure context + phase for EVERY
    agent (read-only, durable `context_budget_state`), soft-threshold checkpointing at
    a safe boundary, hard-threshold rotation (canary-confined via the Actuator;
    non-canary → owner-gated `context_rotation_needed` event)."""
    from core import commander_autopilot as ap
    registry = registry if registry is not None else ap.load_registry()
    if inventory is None:
        from core import agent_control as ac
        inventory = ac.agent_list()
    if tail_fn is None:
        tail_fn = ap._real_tail
    if pending_fn is None:
        def pending_fn(t):
            try:
                from core import agent_control as ac
                return ac._pane_pending_input(t)
            except Exception:  # noqa: BLE001
                return ""
    own = conn is None
    conn = conn or _db()
    by_target = {a.get("target"): a for a in (inventory.get("agents") or [])}
    results = []
    try:
        for target, entry in registry.items():
            a = by_target.get(target)
            state = (a or {}).get("state") or "dead"
            alive = bool(a and a.get("alive") and a.get("is_agent"))
            tail = tail_fn(target) if alive else ""
            pending = pending_fn(target) if alive else ""
            cwd = (a or {}).get("claude_cwd") or (a or {}).get("cwd") or entry.get("root", "")
            m = measure_fn(cwd) if measure_fn else measure(cwd)
            ph = phase(state, tail, pending)
            _save_state(conn, target, m, ph)
            r = {"target": target, "measurement": m, "phase": ph, "action": "tracked"}
            if alive and m["over_hard"]:
                from core.control_plane import actuator
                if rotate_enabled and target in actuator.CANARY_AGENTS:
                    r["rotation"] = rotate(target, entry, state=state, tail=tail,
                                           pending=pending, measurement=m, ctrl=ctrl,
                                           sleep=sleep, conn=conn)
                    r["action"] = ("rotated" if r["rotation"].get("rotated")
                                   else f"rotation_refused:{r['rotation'].get('reason')}")
                else:
                    # owner-gated: record the need, never actuate a non-canary agent.
                    r["action"] = "rotation_needed_owner_gated"
                    try:
                        from core import agent_control as ac
                        ac.record_commander_event(
                            target, entry.get("project", ""), "context_rotation_needed",
                            {"size_bytes": m["size_bytes"], "est_tokens": m["est_tokens"],
                             "conversation_id": m.get("conversation_id"),
                             "hard_limit": hard_limit_bytes(),
                             "note": "context over hard budget — rotation is owner-gated "
                                     "for this agent (not in CANARY_AGENTS)"},
                            dedup_key=f"ctxneed:{target}:{m.get('conversation_id')}",
                            dedup_window_secs=21600)
                    except Exception:  # noqa: BLE001
                        pass
            elif alive and m["over_soft"] and ph["safe_boundary"] \
                    and not _checkpointed_this_conv(conn, target, m.get("conversation_id")):
                # soft threshold at a safe boundary → checkpoint NOW (before excessive),
                # once per conversation. No actuation involved — pure local write.
                content = build_checkpoint(target, entry, state=state, tail=tail,
                                           measurement=m)
                try:
                    path = write_checkpoint(target, content)
                    v = verify_checkpoint(path)
                    if v.get("ok"):
                        _record_rotation(conn, target, m.get("conversation_id"), "", path,
                                         m["size_bytes"], "checkpoint_only", "soft threshold")
                        r["action"] = "checkpointed"
                        r["checkpoint"] = path
                    else:
                        r["action"] = f"checkpoint_unverified:{v.get('reason')}"
                except OSError as e:
                    r["action"] = f"checkpoint_write_failed:{e}"
            results.append(r)
        return {"evaluated": len(results), "results": results}
    finally:
        if own:
            conn.close()


# ── always-on loop (rotation stays canary-confined by the Actuator) ──────────
async def run_loop() -> None:
    import asyncio
    import logging
    log = logging.getLogger("context_budget")
    if not ENABLED:
        log.info("context budget disabled")
        return
    log.info(f"context budget started (interval {INTERVAL}s; soft {soft_limit_bytes()} B, "
             f"hard {hard_limit_bytes()} B; rotation confined to CANARY_AGENTS)")
    while True:
        try:
            res = await asyncio.to_thread(tick)
            noteworthy = [r for r in res["results"] if r["action"] != "tracked"]
            if noteworthy:
                log.info(f"context budget: {[(r['target'], r['action']) for r in noteworthy]}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"context budget tick error: {e}")
        await asyncio.sleep(INTERVAL)
