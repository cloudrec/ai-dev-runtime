"""Owner OS deterministic task queue — the ONLY source of continuations.

Why this exists: for two weeks autonomy depended on reading a tmux pane and deciding whether
the pixels represented a queued instruction. That is not a control path, it is a guess, and
it failed in both directions — a dim recall ghost read as staged input (duplicate pokes), and
dim staged input read as a ghost (`continue with slice 2` sat ~40 minutes until the owner
submitted it by hand).

The contract here:

  * A continuation exists because a ROW exists. Never because text is visible in a pane.
  * States: queued -> submitted -> acknowledged -> working -> done | failed | owner_blocked.
  * Submission goes through the one lease-gated actuator and records send time, target pane,
    task id, idempotency key and delivery evidence.
  * Acknowledgement comes from the conversation TRANSCRIPT — the task's exact text appearing
    as a submitted user message after the send timestamp — never from prompt appearance.
  * No acknowledgement inside the timeout: retry exactly once with the SAME idempotency key,
    then mark failed and notify the owner. A stall is never silent.
  * `/clear` and agent restart restore the active task from this ledger.

Screen reading may still be used for diagnosis. It may never decide whether a task exists or
whether to press Enter.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# How long to wait for the transcript to show our text before retrying / failing.
ACK_TIMEOUT_SECS = int(os.getenv("OS_TASK_ACK_TIMEOUT_SECS", "120"))
# A task may be sent at most twice: the original and exactly one retry.
MAX_ATTEMPTS = int(os.getenv("OS_TASK_MAX_ATTEMPTS", "2"))

QUEUED, SUBMITTED, ACKNOWLEDGED = "queued", "submitted", "acknowledged"
WORKING, DONE, FAILED, OWNER_BLOCKED = "working", "done", "failed", "owner_blocked"
TERMINAL = (DONE, FAILED, OWNER_BLOCKED)
ACTIVE = (SUBMITTED, ACKNOWLEDGED, WORKING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS os_task (
    id TEXT PRIMARY KEY,
    seq INTEGER,
    target TEXT,
    project TEXT,
    text TEXT,
    kind TEXT,
    state TEXT,
    idem TEXT,
    attempts INTEGER,
    conversation_id TEXT,
    created_at TEXT, created_ts REAL,
    submitted_at TEXT, submitted_ts REAL,
    ack_at TEXT, ack_ts REAL,
    done_at TEXT,
    evidence TEXT,
    last_error TEXT
)
"""


def _conn(conn=None):
    conn, own = _c(conn)
    conn.execute(_SCHEMA)
    return conn, own


def _row(r) -> dict:
    return dict(r) if r is not None else {}


# ── transcript: the acknowledgement source of truth ──────────────────────────
def transcript_messages(cwd: str, limit_files: int = 1) -> list:
    """Every submitted user / assistant message in the project's newest transcript.

    Returns [{"type": "user"|"assistant", "text": str, "ts": float}] in file order. This is
    what the agent actually RECEIVED and produced, which is why acknowledgement is anchored
    here rather than in a rendered prompt line.
    """
    import glob
    out = []
    try:
        proj = "/root/.claude/projects/" + (cwd or "").replace("/", "-")
        files = sorted(glob.glob(proj + "/*.jsonl"), key=os.path.getmtime)[-limit_files:]
        for path in files:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    t = d.get("type")
                    if t not in ("user", "assistant"):
                        continue
                    m = d.get("message") or {}
                    c = m.get("content")
                    txt = c if isinstance(c, str) else " ".join(
                        x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    ts = 0.0
                    raw = d.get("timestamp") or ""
                    if raw:
                        try:
                            from datetime import datetime
                            ts = datetime.fromisoformat(
                                raw.replace("Z", "+00:00")).timestamp()
                        except Exception:  # noqa: BLE001
                            ts = 0.0
                    out.append({"type": t, "text": (txt or "").strip(), "ts": ts})
    except Exception:  # noqa: BLE001
        return out
    return out


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip()


def find_ack(cwd: str, text: str, since_ts: float) -> Optional[dict]:
    """Did the agent RECEIVE this exact task text after we sent it?

    Matching is whitespace-normalised because a pasted multiline command can arrive with its
    line breaks collapsed; the content is what matters, not the layout.
    """
    want = _norm(text)
    if not want:
        return None
    for i, m in enumerate(transcript_messages(cwd)):
        if m["type"] != "user":
            continue
        got = _norm(m["text"])
        if not got:
            continue
        # `since_ts - 5` absorbs clock skew between our send and the transcript stamp.
        if (got == want or want in got) and (m["ts"] == 0.0 or m["ts"] >= since_ts - 5):
            return {"index": i, "ts": m["ts"]}
    return None


def turn_finished(cwd: str, ack_ts: float, target: str = "") -> bool:
    """Has the agent actually FINISHED the acknowledged task?

    "The last transcript entry is an assistant message" is NOT completion: Claude Code writes
    assistant entries throughout a turn (each tool call is one), so that test fires while the
    work is still running. Live: task e8702015 was acknowledged at 00:12:41 and marked done
    at 00:12:42 — one second — and its artefact was never written. A false `done` is worse
    than a slow one: it releases the next task and reports success for work that never
    happened.

    Completion therefore requires BOTH the transcript settling on an assistant message AND
    the pane no longer executing. The pane check is corroboration only — it can delay a
    `done`, never invent a task or decide that one exists.
    """
    msgs = transcript_messages(cwd)
    if not msgs:
        return False
    after = [m for m in msgs if m["ts"] == 0.0 or m["ts"] >= ack_ts - 5]
    if not any(m["type"] == "assistant" for m in after):
        return False
    if msgs[-1]["type"] != "assistant":
        return False
    if target:
        try:
            from core import agent_control as ac
            from core.commander_autopilot import is_progressing
            ok, tail = ac.pane_capture(target, 12)
            if ok and is_progressing(ac.agent_status(target).get("state") or "", tail):
                return False        # still executing — not done yet
        except Exception:  # noqa: BLE001
            return False            # cannot corroborate → do not claim completion
    return True


# ── queue operations ─────────────────────────────────────────────────────────
def enqueue(target: str, text: str, *, project: str = "", kind: str = "continuation",
            conn=None) -> dict:
    """Add a task. This — not a pane — is what makes a continuation exist."""
    conn, own = _conn(conn)
    try:
        tid = uuid.uuid4().hex[:16]
        seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM os_task").fetchone()
               or [1])[0]
        idem = f"ostask:{tid}"
        conn.execute(
            "INSERT INTO os_task (id,seq,target,project,text,kind,state,idem,attempts,"
            "conversation_id,created_at,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, seq, target, project, text, kind, QUEUED, idem, 0, "",
             now_iso(), now_ts()))
        conn.commit()
        return get(tid, conn=conn)
    finally:
        if own:
            conn.close()


def get(task_id: str, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        return _row(conn.execute("SELECT * FROM os_task WHERE id=?", (task_id,)).fetchone())
    finally:
        if own:
            conn.close()


def _list(where: str, args=(), conn=None) -> list:
    conn, own = _conn(conn)
    try:
        conn.row_factory = __import__("sqlite3").Row
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM os_task WHERE {where} ORDER BY seq", args)]
    finally:
        if own:
            conn.close()


def active_task(target: str, conn=None) -> Optional[dict]:
    rows = _list("target=? AND state IN (?,?,?)",
                 (target, SUBMITTED, ACKNOWLEDGED, WORKING), conn=conn)
    return rows[0] if rows else None


def next_queued(target: str, conn=None) -> Optional[dict]:
    rows = _list("target=? AND state=?", (target, QUEUED), conn=conn)
    return rows[0] if rows else None


def pending_for(target: str, conn=None) -> list:
    return _list("target=? AND state NOT IN (?,?,?)",
                 (target, DONE, FAILED, OWNER_BLOCKED), conn=conn)


def set_state(task_id: str, state: str, *, conn=None, **fields) -> dict:
    conn, own = _conn(conn)
    try:
        sets = ["state=?"]
        args = [state]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        args.append(task_id)
        conn.execute(f"UPDATE os_task SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        return get(task_id, conn=conn)
    finally:
        if own:
            conn.close()


def _write_task_file(task: dict, cwd: str) -> str:
    """Persist the task's text where the AGENT can read it, inside its own project only."""
    d = os.path.join(cwd or ".", ".owner-os-tasks")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"task_{task['id']}.md")
    body = (f"# Owner OS task {task['id']}\n\n"
            f"Queued {task.get('created_at', '')} for {task['target']}.\n"
            f"Do exactly what this says, then stop. Nothing else.\n\n"
            f"{task['text']}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


# ── the single controlled submission path ────────────────────────────────────
def submit(task: dict, *, cwd: str, ctrl=None, conn=None, now: Optional[float] = None,
           robust: bool = False) -> dict:
    """Send ONE task through the lease-gated actuator and record the evidence.

    Records, per requirement: exact send timestamp, target pane, task id, idempotency key,
    and the delivery evidence returned by the actuator. A retry reuses the SAME idempotency
    key so a duplicate can never be issued under a different identity.
    """
    from core.control_plane import actuator as act
    from core.control_plane import api as cp
    now = now if now is not None else now_ts()
    target = task["target"]
    if target not in act.CANARY_AGENTS:
        return {"acted": False, "reason": "not_canary", "task_id": task["id"]}

    # The task's own words are OWNER content, and the safety classifier — deny-by-default —
    # correctly refuses arbitrary prose (live: `owner_approval_required` on the first ledger
    # task). Bypassing it would remove the last wall, so instead the text is written to a
    # durable file inside the project's own directory and the pane receives the closed-form
    # grounded pointer. The agent reads the file; the control plane never types free text.
    task_file = _write_task_file(task, cwd)
    send_text = act.build_owner_task_step(f"task_{task['id']}", task_file)

    lease = cp.acquire_lease(f"agent:{target}", "os_task_queue", ttl_secs=120)
    conv = ""
    try:
        from core import agent_control as ac
        conv = ((ac.conversation_evidence(cwd) or {}).get("latest") or {}).get(
            "conversation_id") or ""
    except Exception:  # noqa: BLE001
        conv = ""

    out = act.actuate(target=target, action_text=send_text, controller="os_task_queue",
                      conversation_id=conv, kind=task["idem"], lease=lease, cwd=cwd,
                      ctrl=ctrl)
    acted = bool(out.get("acted"))
    if acted and robust:
        # The first attempt was delivered and "verified" by pane heuristics, yet the
        # transcript never showed it: the Enter did not take on wrapped text and the line
        # sat in the prompt. The retry clears the line and re-delivers through the
        # multiline-safe path instead of trusting another bare Enter.
        try:
            from core import agent_continuation_watchdog as cw
            c = ctrl or cw.Controller()
            out["robust_submit"] = bool(c.robust_submit(target, send_text))
        except Exception as e:  # noqa: BLE001
            out["robust_submit_error"] = str(e)[:120]
    evidence = {"sent_ts": now, "sent_at": now_iso(), "target": target,
                "task_id": task["id"], "idempotency_key": task["idem"],
                "conversation_id": conv, "task_file": task_file,
                "sent_text": send_text, "actuator": {k: out.get(k) for k in
                                                      ("acted", "reason", "verified",
                                                       "idkey", "attempts")},
                "verify": out.get("verify")}
    if not acted:
        cp.release_lease(f"agent:{target}", (lease or {}).get("lease_id"))
    # A refusal never reached the pane, so it must NOT consume the retry budget. Live: five
    # policy refusals (`owner_approval_required`) drove attempts to 7 before the real
    # delivery was even tried, exhausting a budget meant for delivery failures.
    prior = int(task.get("attempts") or 0)
    set_state(task["id"], SUBMITTED if acted else task["state"], conn=conn,
              attempts=prior + 1 if acted else prior,
              submitted_at=now_iso(), submitted_ts=now,
              conversation_id=conv,
              evidence=json.dumps(evidence)[:2000],
              last_error="" if acted else str(out.get("reason") or "")[:200])
    return {"acted": acted, "reason": out.get("reason"), "task_id": task["id"],
            "idempotency_key": task["idem"], "evidence": evidence}


def _notify_owner(task: dict, reason: str, detail: str) -> Optional[str]:
    """An explicit, durable owner notification. A failed task is never a silent stall."""
    try:
        from core.control_plane import api as cp
        from core.control_plane.cto import emit
        g = cp.open_gate(agent_id=task["target"],
                         reason=f"task {task['id']} {reason}: {task['text'][:100]}",
                         kind="os_task_failed",
                         correlation_id=f"ostask:{task['id']}")
        emit("os_task_queue", "task_failed", agent_id=task["target"], severity="high",
             owner_action_required=True,
             payload={"task_id": task["id"], "reason": reason, "detail": detail[:200],
                      "attempts": task.get("attempts"), "text": task["text"][:200]},
             action_taken=f"task {reason} — owner notified",
             dedup_key=f"ostaskfail:{task['id']}")
        return (g or {}).get("id")
    except Exception:  # noqa: BLE001
        return None


def advance(target: str, *, cwd: str, ctrl=None, conn=None,
            now: Optional[float] = None) -> dict:
    """One deterministic step of the state machine for `target`. Reads no pane to decide."""
    now = now if now is not None else now_ts()
    task = active_task(target, conn=conn)

    if task is None:
        nxt = next_queued(target, conn=conn)
        if nxt is None:
            return {"action": "idle", "reason": "no_queued_task"}
        res = submit(nxt, cwd=cwd, ctrl=ctrl, conn=conn, now=now)
        return {"action": "submitted" if res["acted"] else "submit_refused",
                "task_id": nxt["id"], "reason": res.get("reason"),
                "idempotency_key": nxt["idem"]}

    # An active task: has the AGENT acknowledged it, per the transcript?
    # Acknowledgement matches what was actually SENT (the grounded pointer naming this task
    # id), which is what appears in the transcript — not the task's raw prose.
    ack = find_ack(cwd, f"task_{task['id']}", float(task.get("submitted_ts") or 0))
    if ack:
        if task["state"] == SUBMITTED:
            task = set_state(task["id"], ACKNOWLEDGED, conn=conn,
                             ack_at=now_iso(), ack_ts=ack["ts"] or now)
        if turn_finished(cwd, float(task.get("ack_ts") or now), target=target):
            set_state(task["id"], DONE, conn=conn, done_at=now_iso())
            return {"action": "done", "task_id": task["id"]}
        if task["state"] != WORKING:
            set_state(task["id"], WORKING, conn=conn)
        return {"action": "working", "task_id": task["id"]}

    # Not acknowledged. Bounded wait, then exactly one retry on the same idempotency key.
    waited = now - float(task.get("submitted_ts") or now)
    if waited < ACK_TIMEOUT_SECS:
        return {"action": "awaiting_ack", "task_id": task["id"], "waited_secs": int(waited)}
    if int(task.get("attempts") or 0) < MAX_ATTEMPTS:
        res = submit(task, cwd=cwd, ctrl=ctrl, conn=conn, now=now, robust=True)
        return {"action": "retried", "task_id": task["id"],
                "attempt": int(task.get("attempts") or 0) + 1,
                "idempotency_key": task["idem"], "acted": res["acted"]}
    gate = _notify_owner(task, "not acknowledged", f"no transcript ack after "
                                                   f"{int(waited)}s and {task['attempts']} attempts")
    set_state(task["id"], FAILED, conn=conn, done_at=now_iso(),
              last_error=f"no_ack_after_{int(waited)}s")
    return {"action": "failed", "task_id": task["id"], "gate_id": gate,
            "reason": "ack_timeout"}


def restore_after_reset(target: str, *, cwd: str, conn=None) -> dict:
    """`/clear` or an agent restart wipes the pane, never the ledger.

    A task already acknowledged in the OLD conversation stays done/working — the agent read
    it. A task sent but never acknowledged is put back to `queued` so the normal path
    re-sends it with its original idempotency key.
    """
    task = active_task(target, conn=conn)
    if task is None:
        return {"action": "nothing_active"}
    try:
        from core import agent_control as ac
        conv = ((ac.conversation_evidence(cwd) or {}).get("latest") or {}).get(
            "conversation_id") or ""
    except Exception:  # noqa: BLE001
        conv = ""
    if conv and task.get("conversation_id") and conv != task["conversation_id"]:
        if task["state"] == SUBMITTED and not find_ack(
                cwd, f"task_{task['id']}", float(task.get("submitted_ts") or 0)):
            set_state(task["id"], QUEUED, conn=conn,
                      last_error="conversation reset before acknowledgement")
            return {"action": "requeued", "task_id": task["id"], "new_conversation": conv}
    return {"action": "kept", "task_id": task["id"], "state": task["state"]}
