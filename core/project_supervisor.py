"""Server-resident project supervisor — the FALLBACK behind ChatGPT review.

THE PRIMARY LOOP IS NOT THIS MODULE
-----------------------------------
An agent finishes a block -> the wake bridge creates a real inbound turn in that
project's ChatGPT conversation -> ChatGPT reviews the report against the approved
roadmap in Git/MD -> ChatGPT sends the next scoped task to the agent. That is the
product, and it works: the composer counts user turns before and after each
submission, so `submitted_and_user_turn_appeared` is proof that a turn landed,
and 796 delivered wakes have been watched with 1.8% needing a re-wake and 0.8%
escalating — 45 in the last 24 hours with neither.

An earlier draft of this file claimed a ChatGPT conversation cannot be driven
event-driven from the server. That was WRONG, and the correction matters: this
supervisor is not a replacement for the review loop. It is the safety net for
when that loop demonstrably fails.

WHEN THIS MODULE MAY ACT
------------------------
Only after the wake path has been given its chance and did not produce progress:
the wake was delivered and the closed-loop watchdog re-woke or escalated it, or
delivery itself failed. Until then the answer is DEFER_TO_REVIEW — the reviewer
owns the decision, and a supervisor that jumps in first would silently replace
product review with roadmap order.

WHAT IT MAY AND MAY NOT DECIDE, WHEN IT DOES ACT
------------------------------------------------
It may decide ORDER and READINESS: which recorded block is next, whether the last
one is genuinely finished, whether a block is safe to run unattended.

It may not decide WHAT TO BUILD. Blocks come from files already in the project's
git tree (PROJECT_PLAN.md, else TASKS.md). A roadmap that is missing, exhausted
or ambiguous produces an OWNER GATE, never an invented task.

EVIDENCE, NOT CLAIMS
--------------------
A block counts as done when the agent leaves a structured handoff AND that
handoff survives checking against git: the commit must exist, and it must
actually touch the files claimed. An unverifiable handoff leaves the block open.

WHAT IT REUSES (no parallel control plane)
------------------------------------------
* `core.os_task_queue` — the ONLY continuation dispatcher.
* `core.agent_control` — busy check, so no duplicate agent and no interrupted turn.
* `core.closed_loop_wake` — whose watchdog decides that review did not happen.
* `core.model_router` — Sonnet/Opus/Fable routing, unchanged.

Enablement is per project and defaults to NOTHING.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

# Per-project opt-in. Empty by default: this never runs anywhere it was not named.
def enabled_projects() -> set:
    raw = os.getenv("PROJECT_SUPERVISOR_PROJECTS", "").strip()
    return {p.strip() for p in raw.split(",") if p.strip()}


# Plan sources, in order of preference. Both are ordinary files in the project's
# own git tree, so the roadmap is reviewable and versioned like everything else.
PLAN_FILES = ("PROJECT_PLAN.md", "TASKS.md")
HANDOFF_PATH = os.path.join(".owner-os", "handoff.json")

_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.+?)\s*$")
_HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$")

# A block is GATED - never run unattended - when its own text or its section
# heading says so. These are deliberately broad: a false gate costs a message to
# the owner, a missed gate costs an unattended agent doing something irreversible.
_GATE_MARKERS = re.compile(
    r"blocked\s+on|owner[- ]only|owner\s+gate|requires\s+owner|needs\s+the\s+owner|"
    r"live\s+browser|live\s+probe|real\s+probe|probe\s+first|deferred|"
    r"payment|billing|invoice|refund|payout|money|charge|"
    r"credential|secret|token|api[_-]?key|password|\.env|"
    r"deploy|production|prod\b|irreversible|migration|drop\s+table|force[- ]push|"
    r"rotate|dns|firewall", re.IGNORECASE)

# Text that describes a CONSTRAINT rather than a unit of work. "Preserve X while
# doing Y" is a rule the next block must respect, not a block to dispatch.
_CONSTRAINT_MARKERS = re.compile(
    r"^\s*(preserve|reuse|keep|maintain|do not|don't|never|always|avoid)\b",
    re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_autopilot (
    project TEXT PRIMARY KEY,
    plan_source TEXT, objective TEXT,
    current_block TEXT, last_decision TEXT, last_reason TEXT,
    gate_reason TEXT, updated_at TEXT, updated_ts REAL,
    last_checkpoint_ts REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_block_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT, block_id TEXT, commit_sha TEXT, tests TEXT,
    files_changed TEXT, risks TEXT, next_recommendation TEXT,
    validated INTEGER DEFAULT 0, validation_error TEXT,
    at TEXT, ts REAL
)
"""


class SupervisorError(Exception):
    """A refusal with an exact reason."""


def _conn(conn=None):
    conn, own = _c(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn, own


# ── the roadmap, read from the project's own files ──────────────────────────

def _git(repo: str, args: list, timeout: int = 30) -> str:
    try:
        out = subprocess.run(["git", "-C", repo] + args, capture_output=True,
                             text=True, timeout=timeout, shell=False)
        return (out.stdout or "").strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def block_id(title: str) -> str:
    """A stable id for a roadmap line, so completion survives re-ordering and
    re-wording of everything around it."""
    import hashlib
    return hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:12]


def parse_plan(repo: str) -> dict:
    """Read the roadmap out of the project's own tree. Never invents a block."""
    for name in PLAN_FILES:
        path = os.path.join(repo, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            return {"ok": False, "reason": f"plan_unreadable:{e}", "source": name}
        blocks, section, section_gated = [], "", False
        for raw in lines:
            h = _HEADING_RE.match(raw)
            if h:
                section = h.group(2).strip()
                section_gated = bool(_GATE_MARKERS.search(section))
                continue
            m = _CHECKBOX_RE.match(raw)
            if not m:
                continue
            done = m.group(1).lower() == "x"
            title = m.group(2).strip()
            gated = section_gated or bool(_GATE_MARKERS.search(title))
            constraint = bool(_CONSTRAINT_MARKERS.match(title))
            reason = ""
            if section_gated:
                reason = f"section:{section}"
            elif gated:
                reason = "text_marks_a_gate"
            elif constraint:
                reason = "constraint_not_a_work_block"
            blocks.append({"id": block_id(title), "title": title, "done": done,
                           "section": section, "gated": gated,
                           "constraint": constraint, "gate_reason": reason})
        return {"ok": True, "source": name, "blocks": blocks,
                "sha": _git(repo, ["log", "-1", "--format=%H", "--", name]),
                "counts": {"total": len(blocks),
                           "done": sum(1 for b in blocks if b["done"]),
                           "open": sum(1 for b in blocks if not b["done"]),
                           "actionable": sum(1 for b in blocks if not b["done"]
                                             and not b["gated"] and not b["constraint"])}}
    return {"ok": False, "reason": "no_plan_file", "searched": list(PLAN_FILES)}


def completed_block_ids(project: str, conn=None) -> set:
    """Blocks with VALIDATED evidence banked. The plan file says what work
    exists; this ledger says what is finished and proven.

    Both are needed. Relying on the file alone re-dispatched a block forever
    when the agent completed it without ticking its checkbox - the supervisor
    verified the commit, advanced, and then picked the same line again. Relying
    on the ledger alone would let the roadmap and the code drift apart.
    """
    conn, own = _conn(conn)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT block_id FROM project_block_evidence "
            "WHERE project=? AND validated=1", (project,))}
    finally:
        if own:
            conn.close()


def next_block(plan: dict, completed: Optional[set] = None) -> dict:
    """The next block that may run UNATTENDED, or an explicit refusal.

    Order is the roadmap's own order. The first open block that is neither gated,
    a constraint, nor already proven complete wins; anything else is named, so a
    stall is explainable rather than mysterious.
    """
    if not plan.get("ok"):
        return {"found": False, "reason": plan.get("reason", "no_plan")}
    completed = completed or set()
    open_blocks = [b for b in plan["blocks"]
                   if not b["done"] and b["id"] not in completed]
    if not open_blocks:
        return {"found": False, "reason": "roadmap_complete"}
    for b in open_blocks:
        if not b["gated"] and not b["constraint"]:
            return {"found": True, "block": b}
    return {"found": False, "reason": "all_open_blocks_gated",
            "blocked": [{"title": b["title"], "why": b["gate_reason"]}
                        for b in open_blocks[:8]]}


# ── the agent's handoff, checked against git ────────────────────────────────

HANDOFF_FIELDS = ("block_id", "commit", "tests", "files_changed", "risks",
                  "next_recommendation")


def read_handoff(repo: str) -> Optional[dict]:
    path = os.path.join(repo, HANDOFF_PATH)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"_malformed": True}


def validate_handoff(repo: str, handoff: Optional[dict], expect_block: str = "") -> dict:
    """Does the claim survive contact with git?

    A handoff is a claim. The commit it names must exist in this repository and
    must actually contain the files it says were changed. Anything less leaves
    the block OPEN - an agent that reports success it cannot evidence has not
    finished, and advancing on its word is how a roadmap silently desynchronises
    from the code.
    """
    if handoff is None:
        return {"valid": False, "reason": "no_handoff"}
    if handoff.get("_malformed"):
        return {"valid": False, "reason": "handoff_not_json"}
    missing = [f for f in HANDOFF_FIELDS if f not in handoff]
    if missing:
        return {"valid": False, "reason": f"handoff_missing_fields:{','.join(missing)}"}
    if expect_block and str(handoff.get("block_id")) != str(expect_block):
        return {"valid": False,
                "reason": f"handoff_for_another_block:{handoff.get('block_id')}"}
    sha = str(handoff.get("commit") or "").strip()
    if not re.match(r"^[0-9a-fA-F]{7,40}$", sha):
        return {"valid": False, "reason": "commit_not_a_sha"}
    if _git(repo, ["cat-file", "-t", sha]) != "commit":
        return {"valid": False, "reason": f"commit_not_in_repo:{sha[:12]}"}
    claimed = [str(f).strip() for f in (handoff.get("files_changed") or []) if str(f).strip()]
    if not claimed:
        return {"valid": False, "reason": "no_files_changed_claimed"}
    actual = set(_git(repo, ["show", "--name-only", "--format=", sha]).splitlines())
    unbacked = [f for f in claimed if f not in actual]
    if unbacked:
        return {"valid": False,
                "reason": f"files_not_in_commit:{','.join(unbacked[:4])}"}
    tests = handoff.get("tests")
    if not isinstance(tests, dict) or "passed" not in tests:
        return {"valid": False, "reason": "tests_evidence_missing"}
    if tests.get("failed"):
        return {"valid": False, "reason": f"tests_failed:{tests.get('failed')}"}
    return {"valid": True, "commit": sha, "files": claimed, "tests": tests}


# ── durable state ───────────────────────────────────────────────────────────

def state(project: str, conn=None) -> dict:
    conn, own = _conn(conn)
    try:
        r = conn.execute("SELECT project, plan_source, objective, current_block, "
                         "last_decision, last_reason, gate_reason, updated_at, "
                         "last_checkpoint_ts FROM project_autopilot WHERE project=?",
                         (project,)).fetchone()
        if not r:
            return {"project": project, "known": False}
        keys = ("project", "plan_source", "objective", "current_block", "last_decision",
                "last_reason", "gate_reason", "updated_at", "last_checkpoint_ts")
        return {"known": True, **dict(zip(keys, r))}
    finally:
        if own:
            conn.close()


def _save_state(project: str, conn, **fields) -> None:
    cols = ("plan_source", "objective", "current_block", "last_decision",
            "last_reason", "gate_reason", "last_checkpoint_ts")
    existing = conn.execute("SELECT project FROM project_autopilot WHERE project=?",
                            (project,)).fetchone()
    if not existing:
        conn.execute("INSERT INTO project_autopilot (project, updated_at, updated_ts) "
                     "VALUES (?,?,?)", (project, now_iso(), now_ts()))
    sets, vals = [], []
    for c in cols:
        if c in fields:
            sets.append(f"{c}=?")
            vals.append(fields[c])
    sets += ["updated_at=?", "updated_ts=?"]
    vals += [now_iso(), now_ts(), project]
    conn.execute(f"UPDATE project_autopilot SET {','.join(sets)} WHERE project=?", vals)
    conn.commit()


def record_evidence(project: str, block: str, validation: dict, handoff: dict,
                    conn=None) -> int:
    conn, own = _conn(conn)
    try:
        cur = conn.execute(
            "INSERT INTO project_block_evidence (project,block_id,commit_sha,tests,"
            "files_changed,risks,next_recommendation,validated,validation_error,at,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project, block, str(handoff.get("commit") or "")[:40],
             json.dumps(handoff.get("tests") or {})[:2000],
             json.dumps(handoff.get("files_changed") or [])[:4000],
             json.dumps(handoff.get("risks") or [])[:2000],
             str(handoff.get("next_recommendation") or "")[:1000],
             int(bool(validation.get("valid"))),
             "" if validation.get("valid") else str(validation.get("reason"))[:200],
             now_iso(), now_ts()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def evidence_log(project: str, limit: int = 20, conn=None) -> list:
    conn, own = _conn(conn)
    try:
        rows = conn.execute(
            "SELECT block_id, commit_sha, validated, validation_error, "
            "next_recommendation, at FROM project_block_evidence WHERE project=? "
            "ORDER BY id DESC LIMIT ?", (project, int(limit))).fetchall()
        return [{"block_id": r[0], "commit": r[1], "validated": bool(r[2]),
                 "validation_error": r[3], "next_recommendation": r[4], "at": r[5]}
                for r in rows]
    finally:
        if own:
            conn.close()


# ── the decision ────────────────────────────────────────────────────────────

CONTINUE, OWNER_GATE, AWAIT_AGENT, IDLE = "continue", "owner_gate", "await_agent", "idle"
DEFER_TO_REVIEW = "defer_to_review"

# How long ChatGPT review gets before the fallback is even considered. Deliberately
# longer than the closed-loop SLO: the watchdog re-wakes once inside that window,
# and the fallback must not race the retry that is meant to fix things.
REVIEW_GRACE_SECS = int(os.getenv("PROJECT_SUPERVISOR_REVIEW_GRACE_SECS", "1800"))


def review_failed(project: str, *, now: Optional[float] = None, conn=None) -> dict:
    """Has the ChatGPT review loop been given its chance and failed?

    Fallback is allowed only on evidence, never on impatience:
      * the closed-loop watchdog ESCALATED a delivered wake for this project
        (re-woken once, still no progress), or
      * the most recent wake for it FAILED to deliver, or
      * a wake was delivered and nothing has moved for REVIEW_GRACE_SECS.

    No wake at all yet is NOT a failure - it means the block just finished and
    the reviewer has not been asked. Returning False there is what keeps review
    primary.
    """
    now = now if now is not None else now_ts()
    conn, own = _conn(conn)
    try:
        def _q(sql, args):
            """A table that does not exist yet means the wake path has not run
            here, which is ABSENCE OF EVIDENCE, not a broken read. Treating it as
            an error would have made the reason misleading on a fresh install."""
            try:
                return conn.execute(sql, args).fetchone()
            except Exception as e:  # noqa: BLE001
                if "no such table" in str(e):
                    return None
                raise

        row = _q("SELECT event_id, delivered_ts, rewoken, escalated, resolved "
                 "FROM wake_loop_watch WHERE project_id=? "
                 "ORDER BY delivered_ts DESC LIMIT 1", (project,))
        last_fail = _q("SELECT delivered, ts FROM wake_delivery WHERE route_key=? "
                       "ORDER BY id DESC LIMIT 1", (project,))
        if last_fail and not last_fail[0]:
            return {"failed": True, "reason": "last_wake_delivery_failed"}
        if not row:
            return {"failed": False, "reason": "no_wake_watched_yet"}
        event_id, delivered_ts, rewoken, escalated, resolved = row
        if escalated:
            return {"failed": True, "reason": f"review_escalated_without_progress:{event_id}"}
        if resolved:
            return {"failed": False, "reason": "review_produced_progress"}
        age = now - float(delivered_ts or 0)
        if age > REVIEW_GRACE_SECS:
            return {"failed": True,
                    "reason": f"no_progress_{int(age)}s_after_wake:{event_id}"}
        return {"failed": False,
                "reason": f"review_in_progress_{int(REVIEW_GRACE_SECS - age)}s_grace"}
    except Exception as e:  # noqa: BLE001 — unknown means DEFER, never act
        return {"failed": False, "reason": f"review_state_unreadable:{type(e).__name__}"}
    finally:
        if own:
            conn.close()


def decide(project: str, repo: str, *, agent_busy: bool, conn=None) -> dict:
    """What should happen next for this project. Pure: reads, never acts.

    The order matters. A busy agent is left alone (no duplicate work, no
    interrupting a turn). An unfinished-but-claimed block is checked against git
    before anything advances. And when the roadmap has nothing unattended-safe
    left, that is an OWNER GATE with the reason named - not a smaller task
    invented to keep the loop busy.
    """
    plan = parse_plan(repo)
    if not plan.get("ok"):
        return {"action": OWNER_GATE, "reason": plan.get("reason"),
                "detail": "no roadmap file in the project tree; the supervisor "
                          "will not author one"}
    st = state(project, conn=conn)
    current = (st.get("current_block") or "") if st.get("known") else ""

    if agent_busy:
        return {"action": AWAIT_AGENT, "reason": "agent_working",
                "current_block": current, "plan": plan["counts"]}

    handoff = read_handoff(repo)
    if current:
        validation = validate_handoff(repo, handoff, expect_block=current)
        if not validation["valid"]:
            return {"action": AWAIT_AGENT, "reason": validation["reason"],
                    "current_block": current, "plan": plan["counts"],
                    "detail": "the block stays open until its handoff checks out "
                              "against git"}
        # The block just verified counts as complete immediately, even though
        # its evidence row is written by tick() - otherwise the very next
        # selection would hand the agent the same block back.
        done_ids = completed_block_ids(project, conn=conn) | {current}
        review = review_failed(project, conn=conn)
        if not review["failed"]:
            return {"action": DEFER_TO_REVIEW, "reason": review["reason"],
                    "completed_block": current, "validation": validation,
                    "plan": plan["counts"],
                    "detail": "ChatGPT review owns the next task; the supervisor "
                              "only steps in after that loop demonstrably fails"}
        nxt = next_block(plan, completed=done_ids)
        return {"action": CONTINUE if nxt.get("found") else OWNER_GATE,
                "fallback_reason": review["reason"],
                "reason": "block_verified" if nxt.get("found") else nxt.get("reason"),
                "completed_block": current, "validation": validation,
                "block": nxt.get("block"), "blocked": nxt.get("blocked"),
                "plan": plan["counts"]}

    nxt = next_block(plan, completed=completed_block_ids(project, conn=conn))
    if not nxt.get("found"):
        return {"action": OWNER_GATE, "reason": nxt.get("reason"),
                "blocked": nxt.get("blocked"), "plan": plan["counts"],
                "detail": "every open roadmap block is gated, a constraint, or the "
                          "roadmap is complete; the supervisor will not invent work"}
    return {"action": CONTINUE, "reason": "next_roadmap_block",
            "block": nxt["block"], "plan": plan["counts"]}


# ── the prompt handed to the agent ──────────────────────────────────────────

def compose_prompt(project: str, block: dict, plan: dict) -> str:
    """The exact text sent to the agent: the recorded block, and the rules that
    keep it inside what was approved. Deliberately quotes the roadmap line
    verbatim - the agent is being told to do a RECORDED thing, not a
    paraphrased one."""
    return (
        f"Owner OS project supervisor — next approved block for {project}.\n\n"
        f"Block (verbatim from {plan.get('source')}): {block['title']}\n\n"
        "Rules for this block:\n"
        "- Do only this block. Do not start the next one.\n"
        "- It comes from the project's own recorded roadmap; if it turns out to be "
        "ambiguous or to need a product decision, STOP and say so — do not decide it.\n"
        "- Anything touching money, credentials, production, or an irreversible "
        "change is an owner gate: stop and report instead.\n"
        "- Run the project's tests and commit your work.\n"
        f"- Tick this block's checkbox in {plan.get('source')} in the same commit, "
        "so the roadmap file stays truthful about what is done.\n"
        f"- When finished, write {HANDOFF_PATH} with: block_id "
        f"(\"{block['id']}\"), commit (the sha), tests ({{\"passed\": n, "
        "\"failed\": n, \"command\": \"...\"}), files_changed (list), risks (list), "
        "next_recommendation (string).\n"
        "  That file is how completion is verified — a commit that does not contain "
        "the files you list will not be accepted as done.\n"
    )


# ── acting on it ────────────────────────────────────────────────────────────

def tick(project: str, repo: str, *, target: str, ctrl=None, conn=None,
         dispatch: bool = True) -> dict:
    """One supervision step for one project.

    `ctrl` injects the side effects (agent busy check, continuation enqueue,
    checkpoint wake) so the decision core stays testable without tmux, a queue
    or a browser.
    """
    ctrl = ctrl or _default_ctrl()
    if project not in enabled_projects():
        return {"acted": False, "reason": "project_not_enabled", "project": project}

    busy = bool(ctrl.agent_busy(target))
    decision = decide(project, repo, agent_busy=busy, conn=conn)
    conn2, own = _conn(conn)
    try:
        action = decision["action"]
        if action == CONTINUE:
            plan = parse_plan(repo)
            block = decision["block"]
            if decision.get("completed_block"):
                record_evidence(project, decision["completed_block"],
                                decision["validation"], read_handoff(repo) or {},
                                conn=conn2)
            if not dispatch:
                _save_state(project, conn2, last_decision=action,
                            last_reason=decision["reason"], plan_source=plan.get("source"))
                return {"acted": False, "reason": "dispatch_disabled", **decision}
            prompt = compose_prompt(project, block, plan)
            sent = ctrl.enqueue_continuation(target=target, text=prompt,
                                             project=project, repo=repo)
            _save_state(project, conn2, current_block=block["id"],
                        last_decision=action, last_reason=decision["reason"],
                        gate_reason="", plan_source=plan.get("source"))
            return {"acted": True, "action": action, "block": block,
                    "dispatch": sent, "plan": decision.get("plan")}
        if action == OWNER_GATE:
            _save_state(project, conn2, last_decision=action,
                        last_reason=decision["reason"],
                        gate_reason=str(decision.get("reason"))[:200])
            # A gate is precisely what wake is FOR.
            ctrl.checkpoint(project=project, kind="owner_gate",
                            reason=decision.get("reason", ""),
                            detail=json.dumps(decision.get("blocked") or
                                              decision.get("detail") or "")[:400])
            return {"acted": True, "action": action, **decision}
        _save_state(project, conn2, last_decision=action,
                    last_reason=decision.get("reason", ""))
        return {"acted": False, "action": action, **decision}
    finally:
        if own:
            conn2.close()


class _default_ctrl:
    """Real side effects, each delegating to the module that already owns it."""

    @staticmethod
    def agent_busy(target: str) -> bool:
        try:
            from core import agent_control
            st = agent_control.agent_status(target)
            return (st.get("state") or "") in ("working", "shell_running")
        except Exception:  # noqa: BLE001 — unknown state is treated as busy
            return True

    @staticmethod
    def enqueue_continuation(*, target: str, text: str, project: str, repo: str) -> dict:
        from core import os_task_queue
        task = os_task_queue.enqueue(target, text, project=project,
                                     kind="continuation")
        return os_task_queue.advance(target, cwd=repo)

    @staticmethod
    def checkpoint(*, project: str, kind: str, reason: str, detail: str = "") -> dict:
        """Wake as a CHECKPOINT: a gate, a failure, a milestone — not the loop."""
        try:
            from core.control_plane.cto import emit
            return emit("project_supervisor", "project_owner_gate",
                        project_id=project, severity="warning",
                        owner_action_required=True,
                        payload={"kind": kind, "reason": reason, "detail": detail},
                        action_taken=f"supervisor paused {project}: {reason}",
                        dedup_key=f"supervisor:{project}:{reason}",
                        dedup_window_secs=3600)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:120]}


SUPERVISOR_INTERVAL_SECS = int(os.getenv("PROJECT_SUPERVISOR_INTERVAL_SECS", "60"))


def project_targets() -> dict:
    """project -> (agent target, repo path), taken from the control plane's own
    agent registry. Nothing is configured twice: if discovery knows the agent's
    session and cwd, the supervisor knows where to send a block."""
    out = {}
    try:
        from core.control_plane.api import _c as _cc
        conn, own = _cc(None)
        try:
            for target, cwd, project in conn.execute(
                    "SELECT target, cwd, project_id FROM agent "
                    "WHERE COALESCE(cwd,'') <> '' AND COALESCE(target,'') <> ''"):
                session = str(target).split(":")[0]
                if session in enabled_projects():
                    out[session] = (target, cwd)
        finally:
            if own:
                conn.close()
    except Exception:  # noqa: BLE001
        return {}
    return out


async def run_loop(log=None, sleep=None) -> None:
    """The continuous loop. Does nothing at all unless a project is named in
    PROJECT_SUPERVISOR_PROJECTS, and even then only ticks projects whose agent
    the registry can locate."""
    import asyncio
    log = log or (lambda level, msg: None)
    sleep = sleep or asyncio.sleep
    if not enabled_projects():
        log("info", "project supervisor: no projects enabled (dormant)")
        return
    log("info", f"project supervisor started for {sorted(enabled_projects())} "
                f"(interval {SUPERVISOR_INTERVAL_SECS}s)")
    last = {}
    while True:
        try:
            for project, (target, repo) in project_targets().items():
                out = await asyncio.to_thread(tick, project, repo, target=target)
                key = (out.get("action"), out.get("reason"))
                if key != last.get(project):
                    log("info", f"project supervisor [{project}]: "
                                f"{out.get('action')} — {out.get('reason')}")
                    last[project] = key
        except Exception as e:  # noqa: BLE001 — a supervisor must not kill the daemon
            log("warning", f"project supervisor tick error: {type(e).__name__}: {e}")
        await sleep(SUPERVISOR_INTERVAL_SECS)


def status(conn=None) -> dict:
    """Everything the owner needs to see at a glance, per enabled project."""
    out = []
    for project in sorted(enabled_projects()):
        st = state(project, conn=conn)
        out.append({**st, "recent_evidence": evidence_log(project, limit=5, conn=conn)})
    return {"enabled_projects": sorted(enabled_projects()), "projects": out,
            "note": "wake is a checkpoint channel; this supervisor is authoritative "
                    "between checkpoints"}
