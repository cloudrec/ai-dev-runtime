"""Commander Autopilot — production internal autonomy for critical projects.

Goal (owner priority): eliminate manual pokes. Each tick the autopilot evaluates every
registered critical agent — pane state, unfinished tasks, background subagents, and last proven
progress — and, when an agent is idle/waiting with documented pre-approved SAFE unfinished work,
delivers the exact next step to the EXISTING agent and confirms the transition to working, with
no duplicate. This runs independently of any ability to message ChatGPT (internal autonomy).

SAFETY MODEL (defence in depth):
  1. The next-step text must classify `autonomous_safe` — deny-by-default on destructive / live /
     payment / trading / traffic-or-DB promotion / credential / publication / push / deploy / ssh
     (core.control_plane.actuator.classify_action → core.agent_continuation_watchdog._FORBIDDEN_RE).
  2. Delivery is routed through the canonical lease-gated Actuator, which is CONFINED to
     CONTROL_PLANE_CANARY_AGENTS. An agent not in that allowlist is EVALUATED ONLY (read-only);
     enabling its live actuation is an explicit owner gate. So no scope expansion happens here.
  3. The Actuator adds a lease + monotonic fence (restart-safe, no duplicate), a false-idle guard
     (a working/shell/subagent pane is never poked), and idempotency (a verified action is never
     re-issued).

A background Fable/subagent counts as WORKING (never poked). Watchdogs flag a stuck shell, an
agent death (never creates a duplicate), and a false completion claimed on a single report.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

_CFG_PATH = os.getenv("COMMANDER_AUTOPILOT_CONFIG",
                      "/root/ai-dev-runtime/config/commander_autopilot.yaml")

# an agent in one of these observable states, with unfinished work, is a poke candidate.
# waiting_owner is EXCLUDED (2026-08-03 fix): that pane may be showing a tool-permission
# dialog, and delivering text onto a dialog is exactly the forbidden interaction — an
# owner question is the supervisor's/owner's job, never an autopilot poke.
POKE_STATES = ("idle", "waiting_input")
# these observable states mean the agent is already making progress → never poke.
PROGRESS_STATES = ("working", "shell_running")

# a background subagent that is RUNNING = work in progress (owner: count as working).
_SUBAGENT_RUNNING_RE = re.compile(
    r"((sub[- ]?agent|fable|background (task|agent|shell|job))\b.{0,40}"
    r"(running|active|in[- ]progress|working|spawned)"
    r"|·\s*\d+\s+agents?\s+(running|working|active)"
    # the REAL Claude Code render while a Task-tool/Fable subagent runs (live
    # mess-qa-automation pane 2026-08-03): "✻ Waiting for 1 background agent to finish"
    r"|waiting for \d+ background agents?\b"
    r"|spawn(ed|ing) \d+ (sub)?agents?)", re.I)
# Claude Code task footer: "N tasks (X done, Y in progress, Z open)".
_TASK_FOOTER_RE = re.compile(
    r"(\d+)\s+tasks?\s*\((\d+)\s+done,\s*(\d+)\s+in progress,\s*(\d+)\s+open\)", re.I)


def load_registry(path: str = None) -> dict:
    """Load the persistent critical-project registry. Pure read of the yaml config."""
    import yaml
    path = path or _CFG_PATH
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("agents", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def parse_task_footer(tail: str) -> dict:
    """Parse the Claude Code task footer. Returns counts + has_unfinished (open or in-progress)."""
    m = _TASK_FOOTER_RE.search(tail or "")
    if not m:
        return {"present": False, "done": 0, "in_progress": 0, "open": 0, "has_unfinished": None}
    done, inprog, opn = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return {"present": True, "done": done, "in_progress": inprog, "open": opn,
            "has_unfinished": (opn + inprog) > 0}


def has_background_subagent(tail: str) -> bool:
    return bool(_SUBAGENT_RUNNING_RE.search(tail or ""))


def is_progressing(state: str, tail: str) -> bool:
    """True when the agent is already doing real work — including a running background subagent
    or a live active-execution marker — so it must NOT be poked.

    2026-08-03 fix: uses the canonical live-status-region active markers instead of the
    watchdog's _ACTIVE_EXEC_RE over the last 400 chars — the bare ✻/✽ glyph there matched
    PAST-tense spinners ("✻ Baked for 4s"), silently suppressing every legitimate poke
    whose tail tip retained a finished-spinner line."""
    from core.agent_control import _STATE_ACTIVE_RUN_RE, live_status_region
    if state in PROGRESS_STATES:
        return True
    if has_background_subagent(tail):
        return True
    # live active-execution markers only (last spinner line → end): a past-tense spinner
    # or a stale marker higher in scrollback must not read as live work.
    if _STATE_ACTIVE_RUN_RE.search(live_status_region(tail or "")):
        return True
    return False


def classify_safety(step_text: str) -> str:
    """autonomous_safe | owner_approval_required | prohibited (deny-by-default)."""
    from core.control_plane import actuator
    return actuator.classify_action(step_text or "")


def end_state_met(reg_entry: dict, tail: str) -> bool:
    """Best-effort: the documented end-state is only 'met' when there is NO unfinished task
    footer. A `completed` claim with open tasks is a FALSE completion (single-report claim)."""
    tf = parse_task_footer(tail)
    if tf["present"]:
        return not tf["has_unfinished"]
    return False   # no evidence of completion → do not accept a bare 'completed' tail


def evaluate(target: str, *, state: str, tail: str = "", conv_age_secs: Optional[float] = None,
             registry: Optional[dict] = None, now: Optional[float] = None) -> dict:
    """Assess one registered agent. Read-only. Returns the decision + rationale + the would-be
    next step and its safety class. Decisions: poke | skip_progressing | skip_no_work |
    skip_unsafe | skip_not_registered | watchdog_dead | watchdog_stuck_shell |
    watchdog_false_completion."""
    registry = registry if registry is not None else load_registry()
    entry = registry.get(target)
    if entry is None:
        return {"target": target, "decision": "skip_not_registered", "state": state}
    tf = parse_task_footer(tail)
    background = has_background_subagent(tail)
    progressing = is_progressing(state, tail)
    step = entry.get("next_step", "")
    safety = classify_safety(step)
    base = {"target": target, "state": state, "background_subagent": background,
            "progressing": progressing, "tasks": tf, "next_step": step, "safety": safety,
            "conv_age_secs": conv_age_secs, "end_state": entry.get("end_state", ""),
            "live_actuation": bool(entry.get("live_actuation", False))}

    # watchdogs first
    if state == "dead":
        # PHASE 2 (B): a REGISTERED session may be revived in place — same tmux
        # session/pane, same approved conversation, proven single-pane-per-cwd first.
        # Everything else keeps v1 behaviour: record, never duplicate, wait for the owner.
        try:
            from core import session_recovery as sr
            reg = sr.load_registry()
            if target in (reg.get("sessions") or {}):
                res = sr.recover(target, registry=reg)
                return {**base, "decision": "watchdog_dead_recovery",
                        "recovery": res,
                        "note": f"registered session recovery: {res.get('reason')}"}
        except Exception as e:  # noqa: BLE001
            return {**base, "decision": "watchdog_dead",
                    "note": f"recovery unavailable ({e}); recorded, NO duplicate created"}
        return {**base, "decision": "watchdog_dead",
                "note": "agent pane dead — recorded; NO duplicate created"}
    if state == "shell_running" and conv_age_secs is not None and conv_age_secs > 1800:
        return {**base, "decision": "watchdog_stuck_shell",
                "note": "shell running but no proven progress > 30m — flagged"}
    if state == "completed" and not end_state_met(entry, tail):
        return {**base, "decision": "watchdog_false_completion",
                "note": "completed claimed but end-state not met (open tasks) — not accepted"}

    if progressing:
        return {**base, "decision": "skip_progressing"}
    # FAIL-CLOSED dialog gate (RU/EN, 2026-08-03): a visible system/tool-permission or
    # confirmation dialog means a HUMAN answer is required, even when the state
    # classifier read the pane as idle/waiting_input — never a poke candidate. The
    # Actuator re-checks this at delivery time; this makes the DECISION honest too.
    from core.agent_continuation_watchdog import pane_shows_dialog
    if pane_shows_dialog(tail):
        return {**base, "decision": "skip_dialog_open",
                "note": "pane shows a permission/confirmation dialog — awaiting a human "
                        "answer; never auto-poked"}
    # UNOBSERVABLE-PANE GUARD (M2, 2026-08-04 targeted review): `_pane_tail` returns ""
    # when capture-pane fails, and every tail-based guard above — active markers,
    # background subagent, end-state, dialog — then reads "" as "clear". Poking a pane
    # nobody can read is a blind keystroke. Unobservable ⇒ never a poke candidate.
    if not (tail or "").strip():
        return {**base, "decision": "skip_unobservable_pane",
                "note": "pane tail empty — capture failed or pane unreadable; "
                        "tail-based guards cannot be evaluated"}
    if state not in POKE_STATES:
        return {**base, "decision": "skip_other_state"}
    # ZERO-HUMAN-PING classification (2026-08-04). An agent parked on "ready to continue
    # on request" / "next block available" is NOT finished — it is work waiting for a
    # ping, and supplying that ping is precisely this loop's job. A model/session limit
    # is a WAIT, not a failure and not a terminal state. Only a verified completion or a
    # real external dependency stops the loop.
    from core import continuation_signals as sig
    from core import project_state as ps
    cls = sig.classify(tail)
    base = {**base, "situation": cls["class"], "situation_reason": cls["reason"]}

    # PHASE 2 (A): a DURABLE terminal marker outranks whatever is on screen. v1 read
    # terminal from the visible pane, so a finished project was resumed again the moment
    # its completion text scrolled out of the capture window. The marker is reopened only
    # by a material project signal (git HEAD, report fingerprint, owner command, new
    # queued task, freshness deadline) — never by pane scroll.
    root = entry.get("root", "")
    stored = ps.get_state(target, root) if root else None
    if stored:
        chg = ps.material_change(stored, cwd=root)
        if not chg["reopen"]:
            return {**base, "decision": "terminal_sticky",
                    "terminal_status": stored["status"],
                    "note": f"durable {stored['status']} from {stored['decided_at']} "
                            f"({stored['reason']}); {chg['reason']}"}
        ps.reopen(target, root, chg["reason"])
        base = {**base, "reopened": chg["reason"]}
    if cls["class"] == "model_limit":
        return {**base, "decision": "skip_model_limit",
                "note": "model/session limit — the SAME session resumes after reset; "
                        "not a technical failure"}
    if cls["class"] == "external_block":
        if root:
            ps.record_terminal(target, root, status="terminal_blocked", reason=cls["reason"],
                               evidence=tail[-400:], report_path=entry.get("report", ""))
        return {**base, "decision": "terminal_external_block", "note": cls["reason"]}
    if cls["class"] == "terminal":
        if root:
            ps.record_terminal(target, root, status="terminal_pass", reason=cls["reason"],
                               evidence=tail[-400:], report_path=entry.get("report", ""))
        return {**base, "decision": "terminal_pass", "note": cls["reason"]}

    # idle/waiting with a task footer showing ZERO unfinished work = the documented
    # end-state is met — report it as such (registry end-state logic), don't poke.
    if tf["present"] and tf["has_unfinished"] is False and not sig.awaiting_ping(tail):
        return {**base, "decision": "end_state_met",
                "note": "task footer shows no open/in-progress work — end-state met"}
    # idle/waiting — is there unfinished pre-approved work? A pane parked awaiting a ping
    # counts as unfinished even with no task footer at all.
    has_work = (tf["has_unfinished"] is True
                or sig.awaiting_ping(tail)
                or (tf["has_unfinished"] is None and bool(step)))
    if not has_work:
        return {**base, "decision": "skip_no_work"}
    if safety != "autonomous_safe":
        return {**base, "decision": "skip_unsafe",
                "note": f"next step is not autonomous_safe ({safety}) — owner gate, not auto-poked"}
    return {**base, "decision": "poke"}


# unchanged repeated decisions are re-recorded at most once per this window, so the
# per-minute loop cannot grow the ledger unboundedly (~7200 identical rows/day pre-fix).
_RECORD_DEDUP_SECS = int(os.getenv("COMMANDER_AUTOPILOT_RECORD_DEDUP_SECS", "3600"))


def _record_run(target: str, decision: str, detail: dict, conn=None) -> None:
    conn, own = _c(conn)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS autopilot_run ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, target TEXT, decision TEXT, "
                     "detail TEXT)")
        prev = conn.execute("SELECT decision, ts FROM autopilot_run WHERE target=? "
                            "ORDER BY id DESC LIMIT 1", (target,)).fetchone()
        # A DELIVERED poke is always recorded: dedupe compares only the decision string,
        # so a verified delivery following a refused attempt (both "poke") would otherwise
        # leave no ledger row at all (2026-08-03 live: the verified 21:23:49Z canary poke
        # was invisible in autopilot_run — only cp_action/event carried it).
        if prev and prev[0] == decision and not detail.get("delivered"):
            from datetime import datetime
            try:
                age = now_ts() - datetime.fromisoformat(prev[1]).timestamp()
                if age < _RECORD_DEDUP_SECS:
                    return                       # identical consecutive decision — deduped
            except Exception:  # noqa: BLE001 — unparsable ts: record rather than drop
                pass
        import json
        conn.execute("INSERT INTO autopilot_run(ts,target,decision,detail) VALUES(?,?,?,?)",
                     (now_iso(), target, decision, json.dumps(detail, default=str)[:2000]))
        conn.commit()
    finally:
        if own:
            conn.close()


def deliver_next_step(target: str, step_text: str, *, conversation_id: str = "", cwd: str = "",
                      ctrl=None, sleep=None, registry: Optional[dict] = None) -> dict:
    """Deliver the next step to the EXISTING agent via the lease-gated Actuator (scope-confined
    to CANARY_AGENTS; a non-canary target returns not_canary = read-only/owner-gated). The
    Actuator supplies the lease+fence (no duplicate, restart-safe), the false-idle guard, and
    verified-submission with a receipt."""
    from core.control_plane import actuator
    from core.control_plane import api as cp
    from core import agent_continuation_watchdog as cw
    import time as _t
    # hard pre-gate: never deliver an unsafe step, even if somehow requested.
    if classify_safety(step_text) != "autonomous_safe":
        return {"acted": False, "reason": "unsafe_step_blocked"}
    # 2026-08-04 review: `live_actuation` in config/commander_autopilot.yaml was PARSED into
    # the assessment but never enforced — the per-project owner gate was decorative, and the
    # env allowlist (CONTROL_PLANE_CANARY_AGENTS) was the only real gate. Adding an agent to
    # that env var would then actuate it even with `live_actuation: false` still in the
    # registry. Both gates must hold. Checked only for targets INSIDE the allowlist so the
    # non-canary path keeps returning the actuator's `not_canary` refusal unchanged;
    # deny-by-default for an allowlisted target that is not in the registry at all.
    if target in actuator.CANARY_AGENTS:
        entry = (registry if registry is not None else load_registry()).get(target)
        if not (entry or {}).get("live_actuation"):
            return {"acted": False, "reason": "registry_live_actuation_disabled",
                    "note": "target is in CANARY_AGENTS but the registry does not grant "
                            "live_actuation — owner gate, evaluate only"}
    ctrl = ctrl or cw.Controller()
    sleep = sleep or _t.sleep
    # 2026-08-04: the idempotency key is (target, conversation, step_hash), so the SAME
    # documented next step was deliverable only ONCE PER CONVERSATION — ever. Live on
    # arbitrage2: the loop correctly decided `poke` on a fresh idle cycle and the actuator
    # answered `already_verified` from a delivery hours earlier, so the session could
    # never be resumed again. A repeated meta-instruction ("continue the next safe step")
    # is not a one-shot action; it is legitimately re-deliverable once the agent has
    # actually moved on. Qualify the key with a PROGRESS FINGERPRINT of the pane body:
    # new work since the last poke ⇒ a new cycle ⇒ deliverable; an unchanged pane keeps
    # the old key and stays deduped, so a stuck agent is still never spammed.
    conv_key = conversation_id
    try:
        snap = ctrl.snapshot(target, cwd) or {}
        body = cw._body_text(snap.get("tail") or "")
        if body:
            import hashlib
            conv_key = f"{conversation_id}|p{hashlib.sha256(body.encode()).hexdigest()[:12]}"
    except Exception:  # noqa: BLE001
        pass
    lease = cp.acquire_lease(f"agent:{target}", "commander_autopilot", ttl_secs=120)
    out = actuator.actuate(target=target, action_text=step_text, controller="commander_autopilot",
                           conversation_id=conv_key, kind="autopilot_next_step",
                           cwd=cwd, lease=lease, ctrl=ctrl, sleep=sleep)
    # 2026-08-04: RELEASE THE LEASE WHEN WE DID NOT ACT. The autopilot acquires a lease
    # every tick; holding it for the full TTL after a refusal STARVED the continuation
    # watchdog, which then got `stale_or_no_lease` and could never submit a queued line.
    # Live symptom: the canary sat at waiting_input with the owner's text queued while the
    # autopilot re-leased it (fence 487) and deferred each tick because the pending line
    # held DIFFERENT text. A refusal must not own the agent.
    if not out.get("acted"):
        try:
            cp.release_lease(f"agent:{target}", (lease or {}).get("lease_id"))
        except Exception:  # noqa: BLE001
            pass
    return out


def _real_tail(target: str) -> str:
    """Live pane tail for a registered agent (read-only, redacted, bounded)."""
    try:
        from core import agent_control as ac
        return ac._pane_tail(target, 40)
    except Exception:  # noqa: BLE001
        return ""


def _real_conv(cwd: str):
    """(conversation_id, mtime_epoch) of the latest Claude conversation for `cwd`.
    Read-only; (None, None) when unknown."""
    try:
        from datetime import datetime
        from core import agent_control as ac
        latest = (ac.conversation_evidence(cwd) or {}).get("latest") or {}
        cid = latest.get("conversation_id")
        mt = latest.get("modified_at")
        return cid, (datetime.fromisoformat(mt).timestamp() if mt else None)
    except Exception:  # noqa: BLE001
        return None, None


def _governor_blocker_seen(conn, target: str, stage: str, fingerprint: str) -> bool:
    """A blocker is recorded ONCE per (target, stage, missing-fields) — an owner gate that
    reopens every 60s is noise, not signal."""
    conn, own = _c(conn)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS governor_blocker (
            target TEXT, stage TEXT, fingerprint TEXT, first_seen TEXT, last_seen TEXT,
            fields TEXT, PRIMARY KEY (target, stage, fingerprint))""")
        row = conn.execute("SELECT 1 FROM governor_blocker WHERE target=? AND stage=? "
                           "AND fingerprint=?", (target, stage, fingerprint)).fetchone()
        conn.execute("UPDATE governor_blocker SET last_seen=? WHERE target=? AND stage=? "
                     "AND fingerprint=?", (now_iso(), target, stage, fingerprint))
        conn.commit()
        return bool(row)
    finally:
        if own:
            conn.close()


def _record_governor_blocker(conn, target: str, stage: str, fields: list,
                             reason: str = "NEEDS_OWNER_PAYLOAD", detail: str = "") -> None:
    import hashlib
    import json as _j
    fp = hashlib.sha256(_j.dumps(sorted(map(str, fields))).encode()).hexdigest()[:16]
    if _governor_blocker_seen(conn, target, stage, fp):
        return
    c2, own = _c(conn)
    try:
        c2.execute("INSERT OR REPLACE INTO governor_blocker VALUES (?,?,?,?,?,?)",
                   (target, stage, fp, now_iso(), now_iso(), _j.dumps(fields)[:2000]))
        c2.commit()
    finally:
        if own:
            c2.close()
    # The gate must describe the ACTUAL blocker. Hard-coding owner_payload_missing put a
    # refused opaque paste into the owner's gate list as "NEEDS_OWNER_PAYLOAD at -", which
    # is simply wrong and buries the real payload gaps.
    kind = ("owner_payload_missing" if reason == "NEEDS_OWNER_PAYLOAD"
            else f"governor_{reason}"[:60])
    where = stage if stage and stage != "-" else target
    try:
        from core.control_plane import api as cp
        from core.control_plane.cto import emit
        cp.open_gate(agent_id=target, reason=f"{reason} at {where}",
                     kind=kind, correlation_id=f"gov:{target}:{where}")
        emit("continuation_governor", reason.lower(), agent_id=target,
             severity="warn", owner_action_required=True,
             payload={"stage": stage, "missing_fields": fields[:20],
                      "detail": detail[:200]},
             action_taken=f"blocked — {reason}",
             dedup_key=f"govblock:{target}:{where}:{fp}")
    except Exception:  # noqa: BLE001
        pass


# A stage may be nudged at most once per cooldown, and only a few times in total. Without
# this the `stage_not_started` path re-delivered every 60s tick (observed live: two
# governor_advanced for stage_a_write_note 70s apart) — exactly-once violated, and a real
# project would be nudged endlessly for a stage it had not begun.
GOVERNOR_STAGE_COOLDOWN_SECS = int(os.getenv("GOVERNOR_STAGE_COOLDOWN_SECS", "600"))
GOVERNOR_STAGE_MAX_ATTEMPTS = int(os.getenv("GOVERNOR_STAGE_MAX_ATTEMPTS", "3"))


def _stage_delivery_gate(conn, target: str, stage: str, *, now: Optional[float] = None) -> dict:
    """May the governor nudge this (target, stage) now? Records the attempt if yes."""
    now = now if now is not None else now_ts()
    conn, own = _c(conn)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS governor_stage_delivery (
            target TEXT, stage TEXT, attempts INTEGER, last_ts REAL, last_at TEXT,
            PRIMARY KEY (target, stage))""")
        r = conn.execute("SELECT attempts,last_ts FROM governor_stage_delivery "
                         "WHERE target=? AND stage=?", (target, stage)).fetchone()
        attempts = int(r[0]) if r else 0
        last_ts = float(r[1]) if r and r[1] else 0.0
        if attempts >= GOVERNOR_STAGE_MAX_ATTEMPTS:
            return {"allow": False, "reason": "stage_nudge_cap_reached", "attempts": attempts}
        if last_ts and (now - last_ts) < GOVERNOR_STAGE_COOLDOWN_SECS:
            return {"allow": False, "reason": "stage_nudge_cooldown",
                    "attempts": attempts,
                    "wait_secs": int(GOVERNOR_STAGE_COOLDOWN_SECS - (now - last_ts))}
        conn.execute("INSERT OR REPLACE INTO governor_stage_delivery VALUES (?,?,?,?,?)",
                     (target, stage, attempts + 1, now, now_iso()))
        conn.commit()
        return {"allow": True, "reason": "stage_nudge_allowed", "attempts": attempts + 1}
    finally:
        if own:
            conn.close()


def _governor_pass(target: str, *, state: str, tail: str, cwd: str, ctrl,
                   conv: str, evaluate_only: bool, conn) -> Optional[dict]:
    """Consult the continuation governor. Returns a result dict to record, or None to let
    the ordinary autopilot evaluation proceed."""
    try:
        from core import continuation_governor as cg
    except Exception:  # noqa: BLE001
        return None
    cfg = cg.load_config()
    if target not in cfg:
        return None
    # NEVER govern a pane that is actually progressing. `govern()` only sees `state`, but a
    # pane can be working via a background subagent or a live active-execution marker while
    # its state reads idle — governing there would raise a blocker (or submit) over real
    # work in flight. The autopilot's own progress detector is the authority.
    if is_progressing(state, tail):
        return None
    # Read the input line through the INJECTED controller when one is supplied. Going
    # straight to agent_control here meant the governor read the live tmux pane even under
    # test, making suite runs depend on whatever a real canary happened to be showing.
    pending = ""
    try:
        if ctrl is not None and hasattr(ctrl, "snapshot"):
            pending = (ctrl.snapshot(target, cwd) or {}).get("pending") or ""
        else:
            from core import agent_control as ac
            pending = ac.pending_input_text(target, tail) or ""
    except Exception:  # noqa: BLE001
        pending = ""
    d = cg.govern(target, state=state, pending=pending, tail=tail, config=cfg)
    base = {"target": target, "state": state, "governor": d}

    if d["action"] == "blocker":
        fields = d.get("blocker_fields") or []
        reason = str(d.get("reason") or "governor_blocker")
        # a refused paste carries no stage/fields — record what it DOES have so the row is
        # actionable instead of an empty "-"
        detail = ""
        if not fields:
            det = d.get("detail") or {}
            detail = f"{det.get('evidence','')}:{(det.get('text') or '')[:80]}"
        _record_governor_blocker(conn, target, str(d.get("stage") or "-"), list(fields),
                                 reason=reason, detail=detail)
        return {**base, "decision": "governor_blocker", "note": reason,
                "blocker_fields": fields[:10], "blocker_detail": detail}

    if d["action"] == "submit_queued" and not evaluate_only:
        # Press Enter on the owner's OWN queued line. Never re-sends text.
        try:
            from core import agent_continuation_watchdog as cw
            from core.control_plane import api as cp
            from core.control_plane import actuator as act
            if target not in act.CANARY_AGENTS:
                return {**base, "decision": "governor_submit_owner_gated"}
            lease = cp.acquire_lease(f"agent:{target}", "continuation_governor", ttl_secs=120)
            c = ctrl or cw.Controller()
            out = cw.deliver_and_verify(c, target=target, cwd=cwd, action="submit",
                                        step_text=d.get("expected_pending", ""),
                                        expected_pending=d.get("expected_pending", ""))
            ok = bool((out.get("verify") or {}).get("ok"))
            if not ok:
                cp.release_lease(f"agent:{target}", (lease or {}).get("lease_id"))
            return {**base, "decision": "governor_submitted" if ok else "governor_submit_unverified",
                    "verify": out.get("verify"), "delivered": ok}
        except Exception as e:  # noqa: BLE001
            return {**base, "decision": "governor_submit_error", "note": str(e)[:120]}

    if d["action"] == "advance_queue":
        # The governor must NEVER type the queue's rich instruction into a pane. That text
        # is domain content for the AGENT to read from its durable queue; relaying it would
        # mean the governor authoring/echoing arbitrary instructions, and the safety
        # classifier correctly refuses it (live: `governor_step_unsafe` on the canary when
        # the queue said "append one dated line to reports/ACCEPTANCE_A.md").
        # So: deliver only the project's own classifier-safe continuation nudge; the agent
        # reads the queue itself and does the stage named by the pointer.
        queue_step = str(d.get("step_text") or "").strip()
        reg_entry = (load_registry() or {}).get(target) or {}
        step = str(reg_entry.get("next_step") or "").strip()
        if classify_safety(step) != "autonomous_safe":
            from core.agent_continuation_watchdog import DEFAULT_CONTINUATION
            step = DEFAULT_CONTINUATION
        if not queue_step or evaluate_only:
            return {**base, "decision": "governor_advance_available",
                    "next_stage": d.get("next_stage"),
                    "note": "next stage grounded in the durable queue; nothing delivered "
                            "(no instruction text, or evaluate-only)"}
        # DELIVER the queue's own instruction, exactly once. Reporting it without
        # delivering left the agent with neither an autopilot poke (short-circuited here)
        # nor a governor step — i.e. a stall introduced by the governor itself.
        if classify_safety(step) != "autonomous_safe":
            return {**base, "decision": "governor_step_unsafe",
                    "note": f"queue instruction is not autonomous_safe: {step[:80]}"}
        try:
            from core.control_plane import actuator as act
            if target not in act.CANARY_AGENTS:
                return {**base, "decision": "governor_advance_owner_gated",
                        "next_stage": d.get("next_stage")}
            gate = _stage_delivery_gate(conn, target, str(d.get("next_stage") or "-"))
            if not gate["allow"]:
                return {**base, "decision": "governor_advance_suppressed",
                        "next_stage": d.get("next_stage"), "note": gate["reason"],
                        "attempts": gate.get("attempts")}
            out = deliver_next_step(target, step, conversation_id=conv, cwd=cwd, ctrl=ctrl)
            return {**base,
                    "decision": ("governor_advanced" if out.get("acted")
                                 else "governor_advance_refused"),
                    "next_stage": d.get("next_stage"), "actuation": out,
                    "delivered": bool(out.get("acted")),
                    "delivered_text": step, "queue_step": queue_step[:120],
                    "note": "delivered a classifier-safe continuation nudge; the stage "
                            "content stays in the durable queue for the agent to read"}
        except Exception as e:  # noqa: BLE001
            return {**base, "decision": "governor_advance_error", "note": str(e)[:120]}
    return None


def tick(*, inventory: Optional[dict] = None, registry: Optional[dict] = None,
         ctrl=None, evaluate_only: bool = False, conv_age_fn=None, now: Optional[float] = None,
         tail_fn=None, conv_fn=None, conn=None) -> dict:
    """One autopilot pass over the registry. For each registered agent: evaluate, then (unless
    evaluate_only) deliver the safe next step to a poke candidate via the Actuator. Returns the
    per-agent assessments + actions. Read-only for any agent whose actuation is owner-gated
    (not in CANARY_AGENTS → actuator returns not_canary).

    PRODUCTION CONTRACT (2026-08-03 fix): the real `agent_list()` inventory carries NO
    `_tail` / `claude_conversation` keys, so the tick FETCHES the live pane tail and the
    latest conversation id itself (injectable via tail_fn / conv_fn). Pre-fix, every agent
    evaluated with tail=""/conversation_id="" — background-subagent detection, task-footer
    logic, per-conversation dedupe and the stuck-shell watchdog were all dead code live."""
    registry = registry if registry is not None else load_registry()
    if inventory is None:
        from core import agent_control as ac
        inventory = ac.agent_list()
    tail_fn = tail_fn or _real_tail
    conv_fn = conv_fn or _real_conv
    by_target = {a.get("target"): a for a in (inventory.get("agents") or [])}
    results = []
    for target, entry in registry.items():
        a = by_target.get(target)
        state = (a or {}).get("state") or "dead"        # not present → treat as dead (watchdog)
        tail = (a or {}).get("_tail")
        if tail is None and a is not None and a.get("alive") and a.get("is_agent"):
            tail = tail_fn(target)
        tail = tail or ""
        cwd = (a or {}).get("claude_cwd") or (a or {}).get("cwd") or entry.get("root", "")
        conv = (a or {}).get("claude_conversation")
        conv_mtime = None
        if not conv and a is not None:
            conv, conv_mtime = conv_fn(cwd)
        conv = conv or ""
        if conv_age_fn:
            conv_age = conv_age_fn(target)
        elif conv_mtime is not None:
            conv_age = (now if now is not None else now_ts()) - conv_mtime
        else:
            conv_age = None
        # PHASE 3 (wired 2026-08-05): the continuation governor runs BEFORE the ordinary
        # evaluation for governed projects. It only ever submits what the owner already
        # queued, or reports a blocker the project's own queue records — it authors
        # nothing. A `skip` falls straight through to the existing logic.
        gov = _governor_pass(target, state=state, tail=tail, cwd=cwd, ctrl=ctrl,
                             conv=conv, evaluate_only=evaluate_only, conn=conn)
        if gov is not None:
            _record_run(target, gov["decision"], gov, conn=conn)
            results.append(gov)
            continue

        ev = evaluate(target, state=state, tail=tail, conv_age_secs=conv_age,
                      registry=registry, now=now)
        action = None
        if ev["decision"] == "poke" and not evaluate_only:
            action = deliver_next_step(target, ev["next_step"], conversation_id=conv,
                                       cwd=entry.get("root", ""), ctrl=ctrl,
                                       registry=registry)
            ev["actuation"] = action
            ev["delivered"] = bool(action.get("acted"))
            ev["actuation_reason"] = action.get("reason")
            if not action.get("acted") and action.get("reason") == "not_canary":
                ev["decision"] = "poke_owner_gated"      # would poke, but live actuation gated
        _record_run(target, ev["decision"], ev, conn=conn)
        results.append(ev)
    return {"evaluated": len(results), "results": results,
            "poked": sum(1 for r in results if r.get("delivered")),
            "owner_gated": sum(1 for r in results if r["decision"] == "poke_owner_gated")}


ENABLED = os.getenv("COMMANDER_AUTOPILOT_ENABLED", "0") not in ("0", "false", "no", "")
INTERVAL = int(os.getenv("COMMANDER_AUTOPILOT_INTERVAL_SECS", "60"))


async def run_loop() -> None:
    """Per-minute autopilot loop. DISABLED by default (COMMANDER_AUTOPILOT_ENABLED) — enabling
    it live is an owner gate. Even enabled, actuation stays confined to CANARY_AGENTS."""
    import asyncio
    import logging
    log = logging.getLogger("commander_autopilot")
    if not ENABLED:
        log.info("commander autopilot disabled (owner gate)")
        return
    log.info(f"commander autopilot started (interval {INTERVAL}s; actuation confined to CANARY_AGENTS)")
    while True:
        try:
            res = await asyncio.to_thread(tick)
            if res["poked"] or res["owner_gated"]:
                log.info(f"autopilot: poked={res['poked']} owner_gated={res['owner_gated']} "
                         f"evaluated={res['evaluated']}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"autopilot tick error: {e}")
        await asyncio.sleep(INTERVAL)
