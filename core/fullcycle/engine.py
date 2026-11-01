"""The full-cycle engine: one persisted, restart-safe loop per project.

    spec ──seed──▶ task (contract CREATED)
                    │  gates.check_dispatch (EVERY dispatch)   refused ─▶ OWNER_DECISION
                    │  first attempt: clean git tree           dirty   ─▶ OWNER_DECISION
                    ▼
                  WORKING ── adapter.poll ──┬─ crashed ─▶ restart + redispatch (attempt+1)
                    │                       ├─ permission prompt ─▶ OWNER_DECISION
                    │                       └─ result "blocked" ─▶ BLOCKED ─▶ GPT answers
                    ▼ result "done"
                  AGENT_DONE ─▶ VERIFYING: git diff ⊂ scope, task checks exit 0
                    │                 fail ─▶ VERIFICATION_FAILED ─▶ redispatch with output
                    ▼ pass
               review requested ── event `task_completed` ──▶ wake bridge ──▶ GPT chat
                    │
         GPT: submit_decision(approve | reject, next_tasks?)   (idempotent per key)
                    ▼
            VERIFIED_DONE ─▶ local commit ─▶ next task ─▶ … ─▶ spec checks ─▶ project done

Persistence: the task's lifecycle is its `core.task_contract` (fail-closed transitions,
immutable history); this module adds the dispatch intent (`redispatch`), the per-attempt
dispatch latch, the review inbox, the commit latch and the project latch, all in the
control-plane DB. Every step can be re-run after a crash:
  * a (re)dispatch is first recorded as an intent with its attempt and handle; the send
    is retried from the intent until it succeeds, is refused for good (owner gate) or
    fails too often (owner gate) — no path leaves a task waiting on nothing;
  * the adapter's submit is idempotent per key (at-least-once on a crash mid-send);
  * a verification records its attempt, and only a passing verification of the CURRENT
    attempt is ever sent for review;
  * a review row is unique per (task, attempt, kind); its event is emitted once;
  * a recorded decision is applied by the next tick, and applying is a no-op when the
    contract has already moved;
  * an approved task is committed by the next tick if the process died before it;
  * one engine per project, by a lease renewed before every step.

Checks are commands the ENGINE runs: those of tasks the spec author wrote run as
written; those of tasks the reviewer added must pass gates.check_argv (a test runner
over project files only), so a review decision cannot make the engine run anything.

What this engine never does: answer an agent's permission dialog, push, deploy, answer
an owner gate, or bind a ChatGPT route.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Callable, Optional

from core import task_contract as tc
from core.control_plane import api as cp
from core.control_plane.store import now_iso, now_ts
from core.fullcycle import gates
from core.fullcycle import spec as specs
from core.fullcycle.executor import (CRASHED, DONE, UNKNOWN, WAITING_PERMISSION,
                                     ExecutorAdapter, TaskEnvelope)
from core.fullcycle.hosts import ExecutionHost, LocalHost

SOURCE = "fullcycle"
REVIEW_EVENT = "task_completed"          # a completion-class trigger in core.wake_bridge
OWNER_EVENT = "owner_decision_required"
MAX_ATTEMPTS = int(os.getenv("FULLCYCLE_MAX_ATTEMPTS", "3"))
MAX_DISPATCH_FAILURES = 5
CHECK_TIMEOUT = int(os.getenv("FULLCYCLE_CHECK_TIMEOUT", "300"))
CHECK_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
# longer than the worst tick (every check of a task at CHECK_TIMEOUT), renewed per step
LEASE_TTL = int(os.getenv("FULLCYCLE_LEASE_TTL", "900"))
# D1: an attempt with no result this long after its send, or whose agent status could not
# be read this many polls in a row, is reported to the tech lead ONCE (a "blocked" review
# carrying `stalled`). The engine never kills, restarts or resends on its own.
ATTEMPT_TIMEOUT = int(os.getenv("FULLCYCLE_ATTEMPT_TIMEOUT", "7200"))
UNKNOWN_POLL_LIMIT = int(os.getenv("FULLCYCLE_UNKNOWN_POLL_LIMIT", "20"))
OWNER_RESUME = ("resume", "retry", "approve")
OWNER_CANCEL = ("cancel", "stop")

# agent_control._busy_for_send: the agent is mid-turn, the send would only queue
_BUSY_REFUSALS = ("refused:working", "refused:shell_running",
                  "refused:queued_messages_pending")
# refusals that will not change by retrying: they need the owner
_PERMANENT_REFUSALS = ("hard_denied", "not_allowlisted", "denied", "owner_proof",
                       "unknown target", "no such")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_task (
    id TEXT PRIMARY KEY, project_id TEXT, key TEXT, seq INTEGER, spec_version INTEGER,
    body TEXT, contract_id TEXT, attempt INTEGER DEFAULT 0, handle TEXT DEFAULT '',
    feedback TEXT DEFAULT '', verification TEXT DEFAULT '', gate_id TEXT DEFAULT '',
    dispatch_failures INTEGER DEFAULT 0, last_reason TEXT DEFAULT '',
    origin TEXT DEFAULT 'spec', redispatch INTEGER DEFAULT 0, committed INTEGER DEFAULT 0,
    created_ts REAL, updated_ts REAL, unknown_polls INTEGER DEFAULT 0,
    UNIQUE (project_id, key)
);
CREATE TABLE IF NOT EXISTS fc_dispatch (
    idem_key TEXT PRIMARY KEY, task_id TEXT, attempt INTEGER, status TEXT,
    reason TEXT DEFAULT '', ts REAL
);
CREATE TABLE IF NOT EXISTS fc_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, task_id TEXT, attempt INTEGER,
    kind TEXT, status TEXT DEFAULT 'pending', event_id INTEGER, payload TEXT,
    acked_ts REAL, acked_by TEXT, decision TEXT, decision_key TEXT UNIQUE,
    decided_by TEXT, decision_body TEXT, applied INTEGER DEFAULT 0,
    created_ts REAL, decided_ts REAL,
    UNIQUE (project_id, task_id, attempt, kind)
);
CREATE TABLE IF NOT EXISTS fc_project_done (
    project_id TEXT PRIMARY KEY, event_id INTEGER, evidence TEXT, ts REAL
)
"""

REVIEW_DECISIONS = {"review": ("approve", "reject"),
                    "blocked": ("answer", "cancel"),
                    "project": ("add_tasks", "escalate")}


class EngineError(RuntimeError):
    pass


_MIGRATIONS = (("fc_task", "unknown_polls", "INTEGER DEFAULT 0"),)


def connect(conn=None):
    conn, own = specs.conn_with_schema(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for table, col, decl in _MIGRATIONS:          # additive only: old rows get the default
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    return conn, own


def _j(v) -> str:
    return json.dumps(v, default=str)


# ── review inbox: the GPT side ──────────────────────────────────────────────
def pending_reviews(project_id: Optional[str] = None, conn=None) -> list:
    conn, own = connect(conn)
    try:
        q = ("SELECT id, project_id, task_id, attempt, kind, event_id, payload, acked_ts "
             "FROM fc_review WHERE status='pending'")
        args: list = []
        if project_id:
            q += " AND project_id=?"
            args.append(project_id)
        rows = conn.execute(q + " ORDER BY id", args).fetchall()
        return [{"review_id": r[0], "project_id": r[1], "task_id": r[2], "attempt": r[3],
                 "kind": r[4], "event_id": r[5], "payload": json.loads(r[6] or "{}"),
                 "acked": r[7] is not None, "decisions": list(REVIEW_DECISIONS[r[4]])}
                for r in rows]
    finally:
        if own:
            conn.close()


def ack_review(review_id: int, *, by: str, conn=None) -> dict:
    """The reviewer saw the wake. Idempotent: the first ack is kept."""
    conn, own = connect(conn)
    try:
        conn.execute("UPDATE fc_review SET acked_ts=?, acked_by=? WHERE id=? AND acked_ts IS NULL",
                     (now_ts(), by, review_id))
        conn.commit()
        r = conn.execute("SELECT acked_ts, acked_by FROM fc_review WHERE id=?",
                         (review_id,)).fetchone()
        if not r:
            raise EngineError(f"no such review {review_id}")
        return {"review_id": review_id, "acked_ts": r[0], "acked_by": r[1]}
    finally:
        if own:
            conn.close()


def submit_decision(review_id: int, decision: str, *, key: str, by: str,
                    feedback: str = "", next_tasks: Optional[list] = None,
                    conn=None) -> dict:
    """Record the reviewer's decision. Recording is the latch; the engine's next tick
    applies it. The same key returns the same answer (a duplicate wake that makes the
    reviewer decide twice changes nothing); a second, different key on a decided review
    is refused."""
    if not key or not key.strip():
        raise EngineError("decision key required")
    conn, own = connect(conn)
    try:
        seen = conn.execute("SELECT id, decision FROM fc_review WHERE decision_key=?",
                            (key,)).fetchone()
        if seen:
            return {"ok": seen[0] == review_id, "duplicate": True, "review_id": seen[0],
                    "decision": seen[1]}
        r = conn.execute("SELECT kind, status, project_id FROM fc_review WHERE id=?",
                         (review_id,)).fetchone()
        if not r:
            raise EngineError(f"no such review {review_id}")
        kind, status, project_id = r
        if status != "pending":
            return {"ok": False, "duplicate": False, "reason": "review_already_decided",
                    "review_id": review_id}
        if decision not in REVIEW_DECISIONS[kind]:
            raise EngineError(f"{kind} review takes {REVIEW_DECISIONS[kind]}, not {decision!r}")
        if decision in ("reject", "answer") and not feedback.strip():
            raise EngineError(f"{decision} needs feedback for the agent")
        tasks = [specs.validate_task(t, f"next_tasks[{i}]")
                 for i, t in enumerate(next_tasks or [])]
        if decision == "add_tasks" and not tasks:
            raise EngineError("add_tasks needs next_tasks")
        cur = conn.execute(
            "UPDATE fc_review SET status='decided', decision=?, decision_key=?, decided_by=?,"
            " decision_body=?, decided_ts=? WHERE id=? AND status='pending'",
            (decision, key, by, _j({"feedback": feedback, "next_tasks": tasks}), now_ts(),
             review_id))
        conn.commit()
        if cur.rowcount != 1:
            return {"ok": False, "duplicate": False, "reason": "review_already_decided",
                    "review_id": review_id}
        return {"ok": True, "duplicate": False, "review_id": review_id, "decision": decision}
    finally:
        if own:
            conn.close()


# ── engine ──────────────────────────────────────────────────────────────────
class LeaseLost(RuntimeError):
    pass


def _offer_to_wake_bridge(eid: int, type_: str, severity: str, owner: bool, *,
                          project_id: str, agent_id: str, correlation_id: str, conn) -> None:
    """What cto.emit does after recording: a raw append_event is never seen by the wake
    bridge, so a review would wait in the inbox while the reviewer's chat sleeps.
    Best-effort, like there: recording the event must never fail because of the bridge."""
    try:
        from core import wake_bridge as wb
        if not (wb._enabled()[0] and wb.is_significant(
                event_type=type_, severity=severity,
                owner_action_required=owner)["significant"]):
            return
        d = wb.should_wake(event_id=eid, severity=severity, event_type=type_,
                           correlation_id=correlation_id, owner_action_required=owner,
                           project_id=project_id, agent_id=agent_id, conn=conn)
        wb.record(d, event_id=eid, severity=severity, event_type=type_,
                  correlation_id=correlation_id, project_id=project_id, agent_id=agent_id,
                  conn=conn)
    except Exception:  # noqa: BLE001
        pass


_TASK_COLS = ("id", "key", "seq", "spec_version", "body", "contract_id", "attempt", "handle",
              "feedback", "verification", "gate_id", "dispatch_failures", "last_reason",
              "origin", "redispatch", "committed", "unknown_polls")


class Engine:
    def __init__(self, project_id: str, adapter: ExecutorAdapter, *,
                 host: Optional[ExecutionHost] = None, holder: Optional[str] = None,
                 lease_ttl: int = LEASE_TTL, max_attempts: int = MAX_ATTEMPTS,
                 commit_on_approve: bool = True, conn=None,
                 clock: Callable[[], float] = now_ts):
        self.project_id, self.adapter = project_id, adapter
        self.host = host or getattr(adapter, "host", None) or LocalHost()
        # unique per process unless the caller names one: two engines started with the
        # defaults must NOT share a lease
        self.holder = holder or f"fullcycle-engine:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.lease_ttl, self.max_attempts = lease_ttl, max_attempts
        self.commit_on_approve, self.clock = commit_on_approve, clock
        self.conn, _ = connect(conn)
        p = specs.get_project(project_id, conn=self.conn)
        if not p:
            raise EngineError(f"no such project {project_id!r}")
        self.project = p
        self.root = p["root"]
        self.log: list = []
        self._fence = 0

    # -- small helpers --------------------------------------------------------
    def _note(self, what: str, **kw) -> None:
        self.log.append({"what": what, **kw})

    def _event(self, type_: str, entity_id: str, payload: dict, *, owner: bool = False,
               dedup_key: str = "", severity: str = "high") -> int:
        # the executor's session as agent_id: the wake companion's canary filter
        # (WAKE_BRIDGE_ONLY_AGENTS) offers only wakes whose agent is listed
        agent = getattr(self.adapter, "target", "") or ""
        corr = f"fullcycle:{self.project_id}"
        eid = cp.append_event(SOURCE, type_, entity_type="fullcycle_task",
                              entity_id=entity_id, payload=payload,
                              project_id=self.project_id, agent_id=agent, severity=severity,
                              owner_action_required=owner, dedup_key=dedup_key,
                              dedup_window_secs=10 ** 9 if dedup_key else 0,
                              correlation_id=corr, conn=self.conn)
        _offer_to_wake_bridge(eid, type_, severity, owner, project_id=self.project_id,
                              agent_id=agent, correlation_id=corr, conn=self.conn)
        return eid

    def _lead(self) -> None:
        """Renew the lease; raise if another engine holds it."""
        lease = cp.acquire_lease(f"fullcycle:{self.project_id}", self.holder,
                                 ttl_secs=self.lease_ttl, now=self.clock(), conn=self.conn)
        if not lease:
            raise LeaseLost(self.holder)
        self._fence, self._lease_id = lease["fence_token"], lease["lease_id"]

    def release(self) -> bool:
        """Give the lease back (a worker shutting down), so its successor need not wait
        out LEASE_TTL. A no-op when this engine holds none."""
        lid, self._lease_id = getattr(self, "_lease_id", ""), ""
        return bool(lid) and cp.release_lease(f"fullcycle:{self.project_id}", lid,
                                              conn=self.conn)

    def _tasks(self) -> list:
        rows = self.conn.execute(
            f"SELECT {', '.join(_TASK_COLS)} FROM fc_task WHERE project_id=? ORDER BY seq",
            (self.project_id,)).fetchall()
        out = []
        for r in rows:
            t = dict(zip(_TASK_COLS, r))
            t["body"] = json.loads(t["body"])
            t["handle"] = json.loads(t["handle"]) if t["handle"] else {}
            t["verification"] = json.loads(t["verification"]) if t["verification"] else {}
            t["state"] = tc.get(t["contract_id"], conn=self.conn)["state"]
            out.append(t)
        return out

    def _set(self, task: dict, **cols) -> None:
        """Persist and mirror into the in-memory task (the two must never drift)."""
        task.update({k: v for k, v in cols.items()})
        db = {k: (_j(v) if k in ("handle", "verification") and not isinstance(v, str) else v)
              for k, v in cols.items()}
        db["updated_ts"] = self.clock()
        sets = ", ".join(f"{k}=?" for k in db)
        self.conn.execute(f"UPDATE fc_task SET {sets} WHERE id=?", (*db.values(), task["id"]))
        self.conn.commit()

    def _move(self, task: dict, to: str, evidence: Optional[dict] = None) -> None:
        if task["state"] == to:
            return
        tc.transition(task["contract_id"], to, by=SOURCE, evidence=evidence, conn=self.conn)
        self._note("transition", task=task["key"], frm=task["state"], to=to)
        task["state"] = to

    def _trusted(self, task: dict) -> bool:
        return task["origin"] == "spec"

    # -- seeding --------------------------------------------------------------
    def add_task(self, t: dict, *, spec_version: int, origin: str) -> Optional[str]:
        """Idempotent per (project, key): a task key that exists is never re-created."""
        t = specs.validate_task(t)
        if self.conn.execute("SELECT 1 FROM fc_task WHERE project_id=? AND key=?",
                             (self.project_id, t["key"])).fetchone():
            return None
        spec = specs.get_spec(self.project_id, spec_version, conn=self.conn)["body"]
        contract = tc.create({
            "goal": t["goal"], "acceptance_criteria": t["acceptance_criteria"],
            "scope": t["scope"] or spec["scope"], "do_not_touch": spec["do_not_touch"],
            "tests_required": True, "push_allowed": False, "deploy_allowed": False,
            "expected_report": "result JSON at the envelope's result_path"},
            agent_ref=self.adapter.name, project=self.project_id, by=origin, conn=self.conn)
        seq = (self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM fc_task WHERE project_id=?",
                                 (self.project_id,)).fetchone()[0] or 0) + 1
        tid = f"{self.project_id}:{t['key']}"
        self.conn.execute(
            "INSERT INTO fc_task (id, project_id, key, seq, spec_version, body, contract_id,"
            " origin, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, self.project_id, t["key"], seq, spec_version, _j(t), contract["id"], origin,
             self.clock(), self.clock()))
        self.conn.commit()
        self._note("task_created", task=t["key"], origin=origin)
        return tid

    def _seed(self) -> None:
        """Tasks of the latest spec. A task's origin is WHO first put its key into the
        spec — so a reviewer-added task re-seeded after a crash stays untrusted."""
        versions = self.conn.execute(
            "SELECT version, body, by FROM fc_spec WHERE project_id=? ORDER BY version",
            (self.project_id,)).fetchall()
        if not versions:
            raise EngineError("project has no spec")
        first_by: dict = {}
        for _, body, by in versions:
            for t in json.loads(body)["tasks"]:
                first_by.setdefault(t["key"], by)
        latest_v, latest_body, _ = versions[-1]
        for t in json.loads(latest_body)["tasks"]:
            by = first_by[t["key"]]
            self.add_task(t, spec_version=latest_v,
                          origin=by if by.startswith("review:") else "spec")

    # -- dispatch -------------------------------------------------------------
    def _envelope(self, task: dict, attempt: int) -> TaskEnvelope:
        c = tc.get(task["contract_id"], conn=self.conn)["contract"]
        base = os.path.join(self.root, ".fullcycle")
        return TaskEnvelope(
            task_id=task["id"], attempt=attempt, project_id=self.project_id,
            workdir=self.root, goal=c["goal"],
            acceptance_criteria=tuple(c["acceptance_criteria"]), scope=tuple(c["scope"]),
            do_not_touch=tuple(c["do_not_touch"]), feedback=task["feedback"] or "",
            envelope_path=os.path.join(base, "tasks", f"{task['key']}-{attempt}.json"),
            result_path=os.path.join(base, "results", f"{task['key']}-{attempt}.json"))

    def _request_dispatch(self, task: dict, attempt: int, *, feedback: Optional[str] = None) -> None:
        """Record the intent first; `_try_dispatch` carries it out now or on a later tick."""
        cols = {"redispatch": attempt, "dispatch_failures": 0}
        if feedback is not None:
            cols["feedback"] = feedback[:4000]
        self._set(task, **cols)
        self._try_dispatch(task)

    def _refusal(self, task: dict, attempt: int) -> Optional[str]:
        c = tc.get(task["contract_id"], conn=self.conn)["contract"]
        r = gates.check_dispatch(root=self.root, executor=self.project["executor"],
                                 contract=c, checks=task["body"]["checks"],
                                 trusted_checks=self._trusted(task))
        if r:
            return f"permission_denied:{r}"
        g = self._git_refusal()
        if g:
            return f"permission_denied:{g}"
        never_sent = not self.conn.execute(
            "SELECT 1 FROM fc_dispatch WHERE idem_key=?",
            (f"fullcycle:{task['id']}:{attempt}",)).fetchone()
        if attempt == 1 and never_sent:
            # (once the first send may have gone out, changes can be the agent's own)
            dirty = self._changed_files()
            if dirty is None:
                return "permission_denied:git_status_unavailable"
            if dirty:
                # the first attempt must start from a clean tree: everything changed
                # afterwards is then provably the agent's, and nothing of anyone else's
                # can be verified as its work or restored away
                return "dirty_worktree:" + ",".join(dirty[:5])
        return None

    # A task refused at dispatch for a reason of ITS OWN (an untrusted check, a push or
    # deploy power) that never reached the agent is "parked": nothing of it is in the tree
    # and the agent is idle, so it must not hold up independent tasks. Project-wide
    # refusals (root, executor, dirty tree) and anything that reached the agent are not.
    _TASK_SCOPED_REFUSALS = ("permission_denied:check_", "permission_denied:push_requires_owner",
                             "permission_denied:deploy_requires_owner")

    def _parked(self, task: dict) -> bool:
        # (a resumed one keeps this status, with its intent pending, until a send is
        # recorded: so it waits for the task in flight and that task never waits for it)
        if task["state"] != tc.OWNER_DECISION:
            return False
        if not str(task["last_reason"] or "").startswith(self._TASK_SCOPED_REFUSALS):
            return False
        return not self.conn.execute(          # any dispatch record = may have reached it
            "SELECT 1 FROM fc_dispatch WHERE task_id=?", (task["id"],)).fetchone()

    def _in_flight(self, task: dict) -> bool:
        return (task["state"] not in tc.TERMINAL and task["state"] != tc.CREATED
                and not self._parked(task))

    def _try_dispatch(self, task: dict) -> bool:
        attempt = task["redispatch"]
        others = [t["key"] for t in self._tasks() if t["id"] != task["id"] and self._in_flight(t)]
        if others:
            # single executor: a resumed parked task waits for the task in flight; the
            # intent (redispatch) stays and is carried out by a later tick
            self._note("dispatch_waiting_for_other_task", task=task["key"], others=others)
            return False
        if not self._stall_fence_open(task, attempt):
            return False
        refusal = self._refusal(task, attempt)
        if refusal:
            self._set(task, redispatch=0)
            self._owner(task, refusal)
            return False
        key = f"fullcycle:{task['id']}:{attempt}"
        env = self._envelope(task, attempt)
        self.conn.execute("INSERT OR IGNORE INTO fc_dispatch VALUES (?,?,?,?,?,?)",
                          (key, task["id"], attempt, "sending", "", self.clock()))
        self.conn.commit()
        # attempt + handle BEFORE the send: a crash after it then polls the right result
        self._set(task, attempt=attempt, verification="",
                  handle={"task_id": task["id"], "attempt": attempt, "key": key,
                          "envelope": env.envelope_path, "result": env.result_path})
        r = self.adapter.submit(env, key)
        if not r["ok"] and r["reason"] == "agent_not_running" and self.adapter.owns_agent:
            self._note("agent_restart", task=task["key"], why="not_running_at_dispatch",
                       result=self.adapter.restart(r["handle"]))
            r = self.adapter.submit(env, key)
        self.conn.execute("UPDATE fc_dispatch SET status=?, reason=?, ts=? WHERE idem_key=?",
                          ("sent" if r["ok"] else "failed", r["reason"], self.clock(), key))
        self.conn.commit()
        if r["ok"]:
            self._move(task, tc.WORKING)
            self._set(task, handle=r["handle"], redispatch=0, dispatch_failures=0,
                      last_reason=r["reason"])
            self._note("dispatched", task=task["key"], attempt=attempt, already=r["already"])
            return True
        reason = r["reason"]
        if reason in _BUSY_REFUSALS:
            # mid-turn: agent_control refuses rather than queue; not a failure, retry
            # next tick without counting towards MAX_DISPATCH_FAILURES
            self._set(task, last_reason=reason)
            self._note("dispatch_waiting_for_idle", task=task["key"], reason=reason)
            return False
        if any(p in reason for p in _PERMANENT_REFUSALS):
            self._set(task, redispatch=0)
            self._owner(task, f"dispatch_refused:{reason}")
            return False
        n = task["dispatch_failures"] + 1
        self._set(task, dispatch_failures=n, last_reason=reason)
        self._note("dispatch_deferred", task=task["key"], reason=reason, failures=n)
        if n >= MAX_DISPATCH_FAILURES:
            self._set(task, redispatch=0)
            self._block_for_owner(task, f"dispatch_failed_{n}x:{reason}")
        return False

    # -- owner / gpt hand-offs ------------------------------------------------
    def _open_gate(self, task: dict, reason: str, extra: Optional[dict] = None) -> None:
        g = cp.open_gate(work_item_id=task["id"], agent_id=self.adapter.name,
                         reason=reason, kind="fullcycle", conn=self.conn)
        self._set(task, gate_id=g["id"], last_reason=reason)
        self._event(OWNER_EVENT, task["id"], {"reason": reason, "gate_id": g["id"],
                                              "task": task["key"], **(extra or {})},
                    owner=True, dedup_key=f"fullcycle:owner:{g['id']}")

    def _owner(self, task: dict, reason: str, extra: Optional[dict] = None) -> None:
        """Only the owner can lift this: a gate in the control plane + an owner event."""
        self._move(task, tc.OWNER_DECISION)
        if not task["gate_id"]:
            self._open_gate(task, reason, extra)
        self._note("owner_gate", task=task["key"], reason=reason)

    def _block_for_owner(self, task: dict, reason: str) -> None:
        self._move(task, tc.BLOCKED)
        if not task["gate_id"]:
            self._open_gate(task, reason)
        self._note("blocked_for_owner", task=task["key"], reason=reason)

    def _request_review(self, task: dict, kind: str, payload: dict) -> int:
        """Unique per (task, attempt, kind); the event goes out only on first insert."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fc_review (project_id, task_id, attempt, kind, payload,"
            " created_ts) VALUES (?,?,?,?,?,?)",
            (self.project_id, task["id"], task["attempt"], kind, _j(payload), self.clock()))
        self.conn.commit()
        rid = self.conn.execute(
            "SELECT id FROM fc_review WHERE project_id=? AND task_id=? AND attempt=? AND kind=?",
            (self.project_id, task["id"], task["attempt"], kind)).fetchone()[0]
        if cur.rowcount == 1:
            eid = self._event(REVIEW_EVENT, task["id"], {
                "fullcycle": kind, "review_id": rid, "project": self.project_id,
                "task": task["key"], "attempt": task["attempt"], **payload,
                "decide_with": {"review_id": rid, "decisions": list(REVIEW_DECISIONS[kind])}},
                dedup_key=f"fullcycle:review:{rid}")
            self.conn.execute("UPDATE fc_review SET event_id=? WHERE id=?", (eid, rid))
            self.conn.commit()
            self._note("review_requested", task=task["key"], kind=kind, review_id=rid,
                       event_id=eid)
        return rid

    def _review_for(self, task: dict, kind: str) -> Optional[int]:
        r = self.conn.execute("SELECT id FROM fc_review WHERE project_id=? AND task_id=? AND"
                              " attempt=? AND kind=?",
                              (self.project_id, task["id"], task["attempt"], kind)).fetchone()
        return r[0] if r else None

    # -- verification ---------------------------------------------------------
    def _git(self, *args) -> tuple:
        return self.host.run(["git", "-C", self.root, *args], timeout=60)

    def _git_refusal(self) -> Optional[str]:
        """Full Cycle verifies work only from git evidence, so its project root must be a
        readable git work tree with a HEAD. Anything else refuses the dispatch (fail
        closed); other Owner OS project kinds are not affected."""
        rc, out, err = self._git("rev-parse", "--is-inside-work-tree")
        if rc != 0:
            if "not a git repository" in (err or "").lower():
                return "project_root_not_git"
            return "git_unavailable:" + (err or out or "").strip()[-120:]
        if out.strip() != "true":
            return "project_root_not_git"
        rc, out, err = self._git("rev-parse", "--verify", "--quiet", "HEAD")
        if rc != 0:
            return "git_head_unavailable:" + (err or out or "no commit").strip()[-120:]
        return None

    def _changed_files(self) -> Optional[list]:
        """Paths changed against HEAD, both sides of every move (renames are not
        detected, so a moved protected file shows as its deletion), NUL-separated so no
        name is ever mis-parsed. None when the workdir is not a git repo. Gitignored
        files are not seen — a stated limit."""
        rc, out, _ = self._git("status", "--porcelain=v1", "-z", "--no-renames", "-uall")
        if rc != 0:
            return None
        files = []
        for entry in out.split("\0"):
            if len(entry) > 3:
                path = entry[3:]
                if path != ".fullcycle" and not path.startswith(".fullcycle/"):
                    files.append(path)
        return files

    def _run_checks(self, checks: list, *, trusted: bool) -> list:
        out = []
        for argv in checks:
            refused = gates.check_argv(argv, root=self.root, trusted=trusted)
            if refused:
                out.append({"argv": argv, "rc": None, "passed": False, "output": refused})
                continue
            # no bytecode cache: a rewrite of the same size within the same second
            # otherwise runs the STALE .pyc and fails a correct fix (seen in testing)
            rc, so, se = self.host.run(argv, cwd=self.root, timeout=CHECK_TIMEOUT,
                                       env=CHECK_ENV)
            out.append({"argv": argv, "rc": rc, "passed": rc == 0,
                        "output": (so + se)[-2000:]})
        return out

    def _verify(self, task: dict, result: dict) -> dict:
        c = tc.get(task["contract_id"], conn=self.conn)["contract"]
        changed = self._changed_files()
        claimed = [f for f in (result.get("changed_files") or []) if isinstance(f, str)]
        if changed is None:
            # no git evidence: the agent's own claim is never verification (fail closed);
            # the check outputs are still collected for the feedback
            changed, source = [], "unavailable"
            violations = ["changed_files_unverifiable"]
        else:
            source = "git"
            violations = gates.check_changes(changed, scope=c["scope"],
                                             do_not_touch=c["do_not_touch"])
        checks = self._run_checks(task["body"]["checks"], trusted=self._trusted(task))
        regression = self._regression_checks(task)
        passed = (not violations and bool(checks) and all(x["passed"] for x in checks)
                  and all(r["passed"] for r in regression))
        reasons = violations + [f"check_failed:{' '.join(x['argv'])}"
                                for x in checks if not x["passed"]]
        reasons += [f"regression:{r['task']}:{' '.join(r['argv'])}"
                    for r in regression if not r["passed"]]
        if not checks:
            reasons.append("task_has_no_checks")
        return {"attempt": task["attempt"], "passed": passed, "changed_files": changed,
                "changed_source": source, "claimed_changed_files": claimed[:200],
                "violations": violations, "checks": checks, "regression": regression, "reasons": reasons,
                "summary": result.get("summary", "")}

    def _regression_checks(self, task: dict) -> list:
        """The checks of every task already VERIFIED_DONE, re-run on this task's tree: a
        task that breaks earlier verified work fails its own verification (and is told
        which task and check it broke) instead of reaching review. Each check keeps the
        trust of the task that owns it. FULLCYCLE_REGRESSION_CHECKS=0 turns this off."""
        if os.getenv("FULLCYCLE_REGRESSION_CHECKS", "1") == "0":
            return []
        out = []
        for t in self._tasks():
            if t["id"] == task["id"] or t["state"] != tc.VERIFIED_DONE:
                continue
            # LEASE_TTL covers one task's checks, not the whole project's: renew per task
            # (LeaseLost leaves the task VERIFYING; the next holder re-verifies)
            self._lead()
            for x in self._run_checks(t["body"]["checks"], trusted=self._trusted(t)):
                out.append({"task": t["key"], **x})
        return out

    def _restore_violations(self, v: dict) -> list:
        """A tracked file the task may not touch is put back as it was at HEAD; left in
        place it would fail every later attempt. Safe only because the first attempt
        starts from a clean tree (`_refusal`) and approved work is committed before the
        next task starts — so every change in the tree is this task's agent's. Untracked
        files are never deleted; the agent is told to remove them."""
        if v.get("changed_source") != "git":
            return []
        out = []
        for viol in v["violations"]:
            kind, _, path = viol.partition(":")
            if kind == "outside_workdir" or not path:
                continue
            if self._git("ls-files", "--error-unmatch", "--", path)[0] == 0 and \
                    self._git("checkout", "HEAD", "--", path)[0] == 0:
                out.append(path)
        if out:
            self._note("restored_outside_scope", files=out)
        return out

    def _finish_verification(self, task: dict, v: dict) -> None:
        if not v["passed"]:
            # the contract moves FIRST: a crash after it can never send a failed
            # verification to review
            self._move(task, tc.VERIFICATION_FAILED, evidence={"verification": v})
            feedback = "Automatic verification failed: " + "; ".join(v["reasons"])
            restored = self._restore_violations(v)
            if restored:
                feedback += ("\nOwner OS restored these files from HEAD because they are "
                             "outside your scope: " + ", ".join(restored))
            for x in v["checks"]:
                if not x["passed"]:
                    feedback += f"\n$ {' '.join(x['argv'])} (rc={x['rc']})\n{x['output'][-800:]}"
            for x in v.get("regression", []):
                if not x["passed"]:
                    feedback += (f"\nYour change broke the already verified task "
                                 f"'{x['task']}':\n$ {' '.join(x['argv'])} (rc={x['rc']})\n"
                                 f"{x['output'][-800:]}")
            self._set(task, verification=v, feedback=feedback[:4000])
            self._retry(task, why="verification_failed")
            return
        self._set(task, verification=v)
        self._request_review(task, "review", self._review_payload(task))

    def _review_payload(self, task: dict) -> dict:
        v = task["verification"]
        return {"title": task["body"]["title"], "goal": task["body"]["goal"],
                "acceptance_criteria": task["body"]["acceptance_criteria"],
                "summary": v.get("summary", ""), "changed_files": v.get("changed_files", []),
                "checks": [{"argv": x["argv"], "passed": x["passed"]} for x in v["checks"]]}

    def _retry(self, task: dict, *, why: str, feedback: Optional[str] = None) -> None:
        if task["attempt"] >= self.max_attempts:
            self._block_for_owner(task, f"max_attempts_{self.max_attempts}:{why}")
            return
        self._request_dispatch(task, task["attempt"] + 1, feedback=feedback)

    # -- one task step --------------------------------------------------------
    def _step(self, task: dict) -> None:
        if task["redispatch"]:
            self._try_dispatch(task)
            return
        st = task["state"]
        if st == tc.CREATED:
            self._request_dispatch(task, 1)
        elif st == tc.WORKING:
            self._step_working(task)
        elif st == tc.AGENT_DONE:
            self._verify_current(task)
        elif st == tc.VERIFYING:
            v = task["verification"]
            if v and v.get("attempt") == task["attempt"] and v.get("passed"):
                if not self._review_for(task, "review"):
                    self._request_review(task, "review", self._review_payload(task))
            else:
                self._verify_current(task)       # crashed before the result was recorded
        elif st == tc.VERIFICATION_FAILED:
            self._retry(task, why="verification_failed")
        elif st == tc.BLOCKED and not task["gate_id"]:
            if not self._review_for(task, "blocked"):
                # crashed between BLOCKED and the review request
                result = self.adapter.collect(task["handle"]) or {}
                self._request_review(task, "blocked", {
                    "question": str(result.get("question") or result.get("summary")
                                    or "the agent reported blocked")[:4000]})
        elif st == tc.OWNER_DECISION and not task["gate_id"]:
            self._open_gate(task, task["last_reason"] or "owner_decision_without_gate")

    def _verify_current(self, task: dict) -> None:
        self._move(task, tc.VERIFYING)
        self._finish_verification(task, self._verify(task, self.adapter.collect(task["handle"]) or {}))

    # -- D1: a silent attempt ---------------------------------------------------------
    def _stall_review(self, task: dict, attempt: int) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT id, status, payload, decision FROM fc_review WHERE project_id=? AND"
            " task_id=? AND attempt=? AND kind='blocked'",
            (self.project_id, task["id"], attempt)).fetchone()
        if not r:
            return None
        payload = json.loads(r[2] or "{}")
        return ({**payload, "id": r[0], "status": r[1], "decision": r[3]}
                if payload.get("stalled") else None)

    def _blocked_review(self, task: dict, payload: dict) -> None:
        """This attempt's "blocked" review. If a stall alert already holds the slot and is
        still pending, the agent's own report replaces its content and the tech lead is
        told once more (the slot is unique per task/attempt/kind)."""
        stall = self._stall_review(task, task["attempt"])
        if not (stall and stall["status"] == "pending"):
            self._request_review(task, "blocked", payload)
            return
        payload = {**payload, "after_stall": stall.get("stalled")}
        self.conn.execute("UPDATE fc_review SET payload=? WHERE id=?", (_j(payload), stall["id"]))
        self.conn.commit()
        self._event(REVIEW_EVENT, task["id"], {
            "fullcycle": "blocked", "review_id": stall["id"], "project": self.project_id,
            "task": task["key"], "attempt": task["attempt"], **payload,
            "decide_with": {"review_id": stall["id"],
                            "decisions": list(REVIEW_DECISIONS["blocked"])}},
            dedup_key=f"fullcycle:review:{stall['id']}:report")

    def _blocked_payload(self, task: dict, attempt: int) -> dict:
        r = self.conn.execute("SELECT payload FROM fc_review WHERE project_id=? AND task_id=?"
                              " AND attempt=? AND kind='blocked'",
                              (self.project_id, task["id"], attempt)).fetchone()
        return json.loads(r[0] or "{}") if r else {}

    def _sent_ts(self, task: dict) -> Optional[float]:
        r = self.conn.execute("SELECT ts FROM fc_dispatch WHERE idem_key=? AND status='sent'",
                              (f"fullcycle:{task['id']}:{task['attempt']}",)).fetchone()
        return r[0] if r else None

    def _stall_alert(self, task: dict, reason: str, **detail) -> None:
        """At most once per attempt: the review row is unique per (task, attempt, kind)
        and its event is emitted only on first insert."""
        if self._review_for(task, "blocked"):
            return
        self._request_review(task, "blocked", {
            "stalled": reason, **detail,
            "question": (f"Attempt {task['attempt']} of task '{task['key']}' has produced no "
                         f"result ({reason}). Owner OS will not kill, restart or resend while "
                         "that attempt may still be running. Answer to retry once the agent is "
                         "provably idle (counts towards the attempt limit), or cancel the task.")})
        self._note("attempt_stalled", task=task["key"], attempt=task["attempt"], reason=reason)

    def _stall_fence_open(self, task: dict, attempt: int) -> bool:
        """A resend after a stall alert goes out only when the previous attempt is proven
        over: its result is checked first (a late result is verified instead of resent),
        then the agent must be provably idle. Anything else waits — never a second
        envelope beside a possibly running one."""
        prev = task["handle"] or {}
        if prev.get("attempt") != attempt - 1:
            return True
        if self._blocked_payload(task, attempt - 1).get("context_exhausted"):
            return self._clean_session_ready(task, prev, attempt)
        if not self._stall_review(task, attempt - 1):
            return True
        obs = self.adapter.poll(prev)
        if obs.state == DONE:
            # the stalled attempt finished after all: drop the resend, verify its result
            self._set(task, redispatch=0, last_reason="late_result_after_stall")
            if task["state"] != tc.WORKING:
                # e.g. BLOCKED at the attempt limit and resumed by the owner: back to
                # WORKING, so the next poll collects and verifies that result (F1)
                self._move(task, tc.WORKING, evidence={"late_result_after_stall": attempt - 1})
            self._note("late_result_after_stall", task=task["key"], attempt=attempt - 1)
            return False
        quiet = True if obs.state == CRASHED else self.adapter.quiescent(prev)
        if quiet is not True:
            self._note("resend_fenced", task=task["key"], attempt=attempt,
                       observed=obs.state, quiescent=quiet)
            return False
        return True

    def _clean_session_ready(self, task: dict, prev: dict, attempt: int) -> bool:
        """D2: the next attempt after a context exhaustion goes to a clean session, and only
        after a safe stop. An adapter that owns its agent starts the fresh session itself;
        for any other (live) agent the clean session was confirmed through the owner gate
        `clean_session_required` before this resend was requested."""
        quiet = self.adapter.quiescent(prev)
        if quiet is not True:
            self._note("resend_fenced", task=task["key"], attempt=attempt,
                       observed="context_exhausted", quiescent=quiet)
            return False
        if self.adapter.owns_agent:
            rs = self.adapter.restart(prev)
            self._note("clean_session", task=task["key"], attempt=attempt, result=rs)
            if not rs.get("ok"):
                self._set(task, redispatch=0)
                self._block_for_owner(task, "clean_session_failed")
                return False
        return True

    def _step_working(self, task: dict) -> None:
        obs = self.adapter.poll(task["handle"])
        stall = self._stall_review(task, task["attempt"])
        if stall and stall.get("decision") == "cancel":
            quiet = True if obs.state in (DONE, CRASHED) else self.adapter.quiescent(task["handle"])
            if quiet is True:
                self._move(task, tc.CANCELLED, evidence={"stall_cancelled": stall["id"]})
            else:
                self._note("stall_cancel_fenced", task=task["key"], observed=obs.state,
                           quiescent=quiet)
            return
        if obs.state != UNKNOWN and task.get("unknown_polls"):
            self._set(task, unknown_polls=0)
        if obs.state == DONE:
            result = self.adapter.collect(task["handle"]) or {}
            if result.get("status") == "blocked":
                self._move(task, tc.BLOCKED)
                self._blocked_review(task, {"question": str(
                    result.get("question") or result.get("summary"))[:4000]})
                return
            if result.get("status") == "context_exhausted":
                # D2: the attempt ended at a safe stop with a compact handoff; the routine
                # decision (continue in a clean session, or cancel) is the tech lead's
                self._move(task, tc.BLOCKED)
                self._blocked_review(task, {
                    "context_exhausted": True, "handoff": result.get("handoff", ""),
                    "handoff_problem": result.get("handoff_problem", ""),
                    "summary": result.get("summary", ""),
                    "question": ("The agent ran out of context and stopped with the handoff "
                                 "below. Answer to continue the task in a clean session (the "
                                 "handoff and your answer become its instructions; counts "
                                 "towards the attempt limit), or cancel.")})
                return
            self._move(task, tc.AGENT_DONE)
            self._verify_current(task)
        elif obs.state == CRASHED:
            self._note("agent_crashed", task=task["key"], attempt=task["attempt"],
                       detail=obs.detail)
            if not self.adapter.owns_agent:
                self._block_for_owner(task, "agent_crashed_not_owned")
                return
            if task["attempt"] >= self.max_attempts:
                self._block_for_owner(task, f"max_attempts_{self.max_attempts}_after_crash")
                return
            rs = self.adapter.restart(task["handle"])
            self._note("agent_restart", task=task["key"], result=rs)
            if rs.get("ok"):
                self._request_dispatch(task, task["attempt"] + 1, feedback=(
                    "The previous attempt ended because the agent process exited. "
                    "Continue from the current state of the workdir."))
        elif obs.state == WAITING_PERMISSION:
            gate = gates.check_permission_prompt(obs.detail.get("command", ""), root=self.root)
            self._owner(task, "agent_permission_prompt", {"permission": gate})
        else:                                            # WORKING or UNKNOWN: still waiting
            if obs.state == UNKNOWN:
                n = int(task.get("unknown_polls") or 0) + 1
                self._set(task, unknown_polls=n)
                if n >= UNKNOWN_POLL_LIMIT:
                    self._stall_alert(task, "agent_status_unknown", unknown_polls=n,
                                      detail=str(obs.detail)[:300])
                    return
            sent = self._sent_ts(task)
            if sent is not None and self.clock() - sent >= ATTEMPT_TIMEOUT:
                self._stall_alert(task, "attempt_timeout",
                                  elapsed_s=int(self.clock() - sent), timeout_s=ATTEMPT_TIMEOUT)

    # -- decisions --------------------------------------------------------------
    def _apply_decisions(self) -> None:
        rows = self.conn.execute(
            "SELECT id, task_id, attempt, kind, decision, decision_key, decided_by,"
            " decision_body FROM fc_review WHERE project_id=? AND status='decided' AND"
            " applied=0 ORDER BY id", (self.project_id,)).fetchall()
        for rid, task_id, attempt, kind, decision, key, by, body in rows:
            self._lead()
            body = json.loads(body or "{}")
            task = next((t for t in self._tasks() if t["id"] == task_id), None)
            review = {"review_id": rid, "by": by, "key": key, "decision": decision,
                      "feedback": body.get("feedback", "")}
            applied = True
            if kind == "project":
                self._apply_project_decision(rid, decision, body)
            elif task is None or task["attempt"] != attempt:
                applied = False                         # stale: nothing to apply to
            elif kind == "review" and task["state"] == tc.VERIFYING:
                if decision == "approve":
                    self._move(task, tc.VERIFIED_DONE, evidence={
                        "tests": task["verification"]["checks"], "review": review})
                else:
                    self._move(task, tc.VERIFICATION_FAILED, evidence={"review": review})
                    self._set(task, feedback="Reviewer rejected: " + review["feedback"])
            elif kind == "blocked" and task["state"] == tc.WORKING and \
                    self._stall_review(task, attempt):
                if decision == "answer":
                    # resend through the fence (_stall_fence_open): never beside a running
                    # attempt; bounded by max_attempts like every other retry
                    self._retry(task, why="stalled", feedback=(
                        "Your previous attempt produced no result and was stopped. Continue "
                        "from the current state of the workdir. Tech lead: " + review["feedback"]))
                else:
                    # not CANCELLED yet: the task stays in flight (so no other task is sent
                    # to the agent) until _step_working proves the attempt over
                    self._note("stall_cancel_pending", task=task["key"])
            elif kind == "blocked" and task["state"] == tc.BLOCKED and \
                    self._blocked_payload(task, attempt).get("context_exhausted"):
                if decision == "answer":
                    p = self._blocked_payload(task, attempt)
                    feedback = ("You are continuing in a clean session after the previous one "
                                "ran out of context. Handoff from the previous session:\n"
                                + (p.get("handoff") or "(none was provided)")
                                + "\n\nTech lead: " + review["feedback"])
                    if task["attempt"] >= self.max_attempts:
                        self._block_for_owner(task, f"max_attempts_{self.max_attempts}:"
                                                    "context_exhausted")
                    elif self.adapter.owns_agent:
                        self._request_dispatch(task, task["attempt"] + 1, feedback=feedback)
                    else:
                        # a clean session for a live agent is a session-lifecycle action the
                        # engine does not hold a grant for: the owner provides/confirms it
                        self._set(task, feedback=feedback[:4000])
                        self._owner(task, "clean_session_required",
                                    {"handoff": p.get("handoff", ""),
                                     "instructions": "Start a clean session for this agent "
                                     "(e.g. /clear at a safe boundary), then answer resume."})
                else:
                    self._move(task, tc.CANCELLED, evidence={"review": review})
            elif kind == "blocked" and task["state"] == tc.BLOCKED:
                if decision == "answer":
                    self._retry(task, why="blocked_answered",
                                feedback="Answer from the tech lead: " + review["feedback"])
                else:
                    self._move(task, tc.CANCELLED)
            else:
                applied = False
            # tasks a decision proposes only count when the decision itself took effect
            if applied and kind != "project" and body.get("next_tasks"):
                self._add_tasks(body["next_tasks"], origin=f"review:{rid}")
            self.conn.execute("UPDATE fc_review SET applied=1 WHERE id=?", (rid,))
            self.conn.commit()
            self._note("decision_applied" if applied else "decision_stale",
                       review_id=rid, decision=decision)

    def _add_tasks(self, tasks: list, *, origin: str) -> int:
        cur = specs.get_spec(self.project_id, conn=self.conn)
        body = dict(cur["body"])
        have = {t["key"] for t in body["tasks"]}
        new = [t for t in tasks if t["key"] not in have]
        if new:
            body["tasks"] = body["tasks"] + new
            cur = specs.put_spec(self.project_id, body, by=origin, conn=self.conn)
        created = 0
        for t in tasks:
            created += bool(self.add_task(t, spec_version=cur["version"], origin=origin))
        return created

    def _apply_project_decision(self, rid: int, decision: str, body: dict) -> None:
        added = 0
        if decision == "add_tasks":
            added = self._add_tasks(body.get("next_tasks") or [], origin=f"review:{rid}")
        if not added:
            self._event(OWNER_EVENT, f"{self.project_id}:__project__",
                        {"reason": "project_acceptance_escalated" if decision != "add_tasks"
                         else "project_add_tasks_added_nothing", "review_id": rid,
                         "feedback": body.get("feedback", "")},
                        owner=True, dedup_key=f"fullcycle:escalate:{rid}")

    def _commit_pending(self) -> None:
        """Commit every approved task not yet committed — also after a crash between the
        approval and the commit. Nothing staged means it was committed already."""
        for task in self._tasks():
            if task["state"] != tc.VERIFIED_DONE or task["committed"]:
                continue
            if not self.commit_on_approve:
                # the owner commits; until then the dirty tree holds the next task
                self._set(task, committed=1)
                continue
            # a git error here must never mark work committed that is not (fail closed)
            # (`add` + `reset` rather than an exclude pathspec: naming a gitignored
            # .fullcycle in a pathspec makes `git add` exit 1 although it staged everything)
            rc_add, out_add, err_add = self._git("add", "-A", "--", ".")
            if rc_add == 0:
                rc_add, out_add, err_add = self._git("reset", "-q", "--", ".fullcycle")
            rc_diff = self._git("diff", "--cached", "--quiet")[0] if rc_add == 0 else None
            if rc_add != 0 or rc_diff not in (0, 1):
                detail = (out_add + err_add).strip()[-500:] if rc_add != 0 else "git diff failed"
                self._set(task, last_reason=f"commit_failed:{detail[-200:]}")
                self._event(OWNER_EVENT, task["id"], {"reason": "commit_failed",
                                                      "task": task["key"], "detail": detail},
                            owner=True, dedup_key=f"fullcycle:commit_failed:{task['id']}")
                self._note("commit_failed", task=task["key"])
                continue
            if rc_diff == 0:
                self._set(task, committed=1)
                continue
            rc, out, err = self.host.run(
                ["git", "-C", self.root, "-c", "user.name=Owner OS fullcycle",
                 "-c", "user.email=fullcycle@localhost", "commit", "-q", "--no-verify", "-m",
                 f"fullcycle: {task['key']} (attempt {task['attempt']}) — {task['body']['title']}"],
                timeout=60)
            if rc == 0:
                self._set(task, committed=1)
                self._note("committed", task=task["key"])
            else:
                self._set(task, last_reason=f"commit_failed:{(out + err).strip()[-200:]}")
                self._event(OWNER_EVENT, task["id"], {"reason": "commit_failed",
                                                      "task": task["key"],
                                                      "detail": (out + err).strip()[-500:]},
                            owner=True, dedup_key=f"fullcycle:commit_failed:{task['id']}")
                self._note("commit_failed", task=task["key"])

    # -- owner gates answered ----------------------------------------------------
    def _owner_answers(self) -> None:
        for task in self._tasks():
            if not task["gate_id"] or task["state"] not in (tc.OWNER_DECISION, tc.BLOCKED):
                continue
            r = self.conn.execute("SELECT state, answer FROM owner_gate WHERE id=?",
                                  (task["gate_id"],)).fetchone()
            if not r or r[0] != "answered":
                continue
            self._lead()
            answer = (r[1] or "").strip().lower()
            self.conn.execute("UPDATE owner_gate SET state='resolved' WHERE id=?",
                              (task["gate_id"],))
            self.conn.commit()
            reason = task["last_reason"]
            self._set(task, gate_id="")
            if answer in OWNER_CANCEL:
                self._move(task, tc.CANCELLED)
            elif answer in OWNER_RESUME:
                if reason == "agent_permission_prompt" and task["handle"]:
                    # the owner dealt with the dialog in the pane: keep watching the
                    # SAME attempt; a new envelope would orphan the work in progress
                    self._move(task, tc.WORKING)
                else:
                    # gates are checked again in _try_dispatch: an owner "resume" is
                    # not a waiver of a denied root or an untested executor
                    self._request_dispatch(task, task["attempt"] + 1)
            else:
                self._open_gate(task, f"unrecognised_owner_answer:{answer[:40]} "
                                      f"(reply one of {OWNER_RESUME + OWNER_CANCEL}); "
                                      f"was: {reason}")
                self._set(task, last_reason=reason)
            self._note("owner_answer_applied", task=task["key"], answer=answer)

    # -- project completion ------------------------------------------------------
    def _project_step(self, tasks: list) -> Optional[str]:
        if self.conn.execute("SELECT 1 FROM fc_project_done WHERE project_id=?",
                             (self.project_id,)).fetchone():
            return "done"
        if not tasks or any(t["state"] not in tc.TERMINAL for t in tasks):
            return None
        if not any(t["state"] == tc.VERIFIED_DONE for t in tasks):
            self._event(OWNER_EVENT, f"{self.project_id}:__project__",
                        {"reason": "project_has_no_verified_task"}, owner=True,
                        dedup_key=f"fullcycle:no_verified:{self.project_id}:{len(tasks)}")
            return "no_verified_task"
        if any(t["state"] == tc.VERIFIED_DONE and not t["committed"] for t in tasks):
            return None
        s = specs.get_spec(self.project_id, conn=self.conn)
        checks = self._run_checks(s["body"]["checks"], trusted=True)
        if checks and not all(c["passed"] for c in checks):
            n = self.conn.execute("SELECT COUNT(*) FROM fc_review WHERE project_id=? AND"
                                  " kind='project'", (self.project_id,)).fetchone()[0]
            pending = self.conn.execute("SELECT 1 FROM fc_review WHERE project_id=? AND"
                                        " kind='project' AND status='pending'",
                                        (self.project_id,)).fetchone()
            last = self.conn.execute("SELECT payload FROM fc_review WHERE project_id=? AND"
                                     " kind='project' ORDER BY id DESC LIMIT 1",
                                     (self.project_id,)).fetchone()
            seen = json.loads(last[0]).get("task_count") if last else None
            # one request per plan size: a decision that added nothing does not re-fire
            # every tick; it has already raised an owner event
            if not pending and seen != len(tasks):
                pseudo = {"id": f"{self.project_id}:__project__", "key": "__project__",
                          "attempt": n + 1}
                self._request_review(pseudo, "project", {
                    "spec_version": s["version"], "task_count": len(tasks),
                    "failed_checks": [{"argv": c["argv"], "output": c["output"][-800:]}
                                      for c in checks if not c["passed"]]})
            return "acceptance_failed"
        evidence = {"spec_version": s["version"], "spec_sha": s["sha"], "checks": checks,
                    "tasks": [{"key": t["key"], "state": t["state"], "attempts": t["attempt"]}
                              for t in tasks]}
        eid = self._event(REVIEW_EVENT, f"{self.project_id}:__project__", {
            "fullcycle": "project_completed", "project": self.project_id, **evidence},
            dedup_key=f"fullcycle:project_done:{self.project_id}")
        self.conn.execute("INSERT OR IGNORE INTO fc_project_done VALUES (?,?,?,?)",
                          (self.project_id, eid, _j(evidence), self.clock()))
        self.conn.commit()
        self._note("project_completed", event_id=eid)
        return "done"

    def _next_task(self, tasks: list) -> None:
        """Single executor: the next task starts only when nothing else is in flight."""
        if any(self._in_flight(t) for t in tasks):
            return
        state = {t["key"]: t["state"] for t in tasks}
        for t in tasks:
            if t["state"] != tc.CREATED:
                continue
            deps = t["body"]["depends_on"]
            dead = [d for d in deps if state.get(d) == tc.CANCELLED]
            if dead:
                self._owner(t, f"dependency_cancelled:{','.join(dead)}")
                return
            if all(state.get(d) == tc.VERIFIED_DONE for d in deps):
                self._lead()
                self._step(t)
                return

    # -- the tick ------------------------------------------------------------------
    def tick(self) -> dict:
        self.log = []
        try:
            self._lead()
            self._seed()
            self._apply_decisions()
            self._commit_pending()
            self._owner_answers()
            for t in self._tasks():
                if t["state"] not in tc.TERMINAL and t["state"] != tc.CREATED:
                    self._lead()
                    self._step(t)
            self._next_task(self._tasks())
            tasks = self._tasks()
            project = self._project_step(tasks)
        except LeaseLost:
            return {"skipped": "lease_held", "log": self.log,
                    "holder": cp.lease_holder(f"fullcycle:{self.project_id}", conn=self.conn)}
        return {"project": project, "tasks": {t["key"]: t["state"] for t in tasks},
                "log": self.log, "fence": self._fence}

    def status(self) -> dict:
        tasks = self._tasks()
        return {"project": self.project_id,
                "done": bool(self.conn.execute("SELECT 1 FROM fc_project_done WHERE"
                                               " project_id=?", (self.project_id,)).fetchone()),
                "tasks": [{"key": t["key"], "state": t["state"], "attempt": t["attempt"],
                           "origin": t["origin"], "last_reason": t["last_reason"]}
                          for t in tasks],
                "pending_reviews": pending_reviews(self.project_id, conn=self.conn)}


def project_status(project_id: str, conn=None) -> dict:
    """Read-only view for the reviewer: spec version, every task's contract state,
    pending reviews. Needs no adapter, so it can be served by the API process."""
    conn, own = connect(conn)
    try:
        p = specs.get_project(project_id, conn=conn)
        if not p:
            raise EngineError(f"no such project {project_id!r}")
        s = specs.get_spec(project_id, conn=conn)
        rows = conn.execute("SELECT key, contract_id, attempt, origin, last_reason FROM fc_task"
                            " WHERE project_id=? ORDER BY seq", (project_id,)).fetchall()
        done = conn.execute("SELECT event_id FROM fc_project_done WHERE project_id=?",
                            (project_id,)).fetchone()
        return {"project": p, "spec_version": s["version"] if s else None,
                "spec_sha": s["sha"] if s else None, "done": bool(done),
                "tasks": [{"key": r[0], "state": tc.get(r[1], conn=conn)["state"],
                           "attempt": r[2], "origin": r[3], "last_reason": r[4]}
                          for r in rows],
                "pending_reviews": pending_reviews(project_id, conn=conn)}
    finally:
        if own:
            conn.close()


# ── "this agent's work is finished" — for the watchers ──────────────────────────
# agent_watch and the closed-loop wake watchdog judge an agent by its pane: at rest without
# finish wording is `work_stopped_incomplete`, and a delivered wake with no pane movement
# afterwards is `wake_loop_no_progress` → `wake_loop_stalled`. For an agent whose Full Cycle
# project is DONE that is false: the engine's verified, reviewer-approved record says the
# work finished, and idleness is the expected end state (TaskBoard 2026-10-30, events
# 76026/76047/76065 after project_completed 76022).
SETTLE_GRACE_SECS = 300   # the agent's own closing turn may end just after the approval


def attempt_in_flight_for_agent(target: str, conn=None) -> bool:
    """D2b: may a Full Cycle attempt be running on `target`'s session right now? Then its
    pane must not be rotated (/clear) or have a handoff written into its tree by another
    controller. True for a task whose contract is WORKING, or with a send pending, whose
    last recorded handle names that session. No Full Cycle tables → False; any error →
    True (fail closed: a rotation is deferred, never forced). Read-only."""
    session = (target or "").strip().split(":", 1)[0]
    if not session:
        return False
    try:
        conn, own = cp._c(conn)
    except Exception:  # noqa: BLE001
        return True
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='fc_task'").fetchone():
            return False
        for cid, handle, redispatch in conn.execute(
                "SELECT contract_id, handle, redispatch FROM fc_task WHERE "
                "COALESCE(handle,'')<>''"):
            try:
                h = json.loads(handle)
            except ValueError:
                continue
            if str(h.get("target") or "").split(":", 1)[0] != session:
                continue
            if redispatch:
                return True
            c = tc.get(cid, conn=conn)
            if not c or c["state"] == tc.WORKING:
                return True
        return False
    except Exception:  # noqa: BLE001 — unreadable state: defer the rotation
        return True
    finally:
        if own:
            conn.close()


def settled_project_for_agent(target: str, conn=None,
                              now: Optional[float] = None) -> Optional[str]:
    """The Full Cycle project `target` worked for, when nothing is left for that agent:

      * every project that dispatched to its session has a `fc_project_done` row,
      * every task of those projects is terminal and no review is pending or decided
        but not yet applied,
      * nothing happened on the agent after completion (past a short grace for its own
        closing turn): no Owner OS delivery to it, no new turn of its own
        (`agent_turn_stopped`, idle notifications excluded) and no movement of its pane
        (agent_watch's `digest_since`) — the last also covers the owner typing into it.

    None otherwise — an agent unknown to Full Cycle, an active or failed project, new
    reviewer tasks, or any later work on the agent — so every other detection stays
    exactly as it was. Read-only; any error answers None (detection preserved)."""
    session = (target or "").strip().split(":", 1)[0]
    if not session or session.startswith("session"):
        return None
    conn, own = cp._c(conn)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='fc_project_done'").fetchone():
            return None
        if not conn.execute("SELECT 1 FROM fc_project_done LIMIT 1").fetchone():
            return None                   # the cheap common answer for the hot paths
        projects = set()
        for pid, handle in conn.execute(
                "SELECT project_id, handle FROM fc_task WHERE COALESCE(handle,'')<>''"):
            try:
                h = json.loads(handle)
            except ValueError:
                continue
            if str(h.get("target") or "").split(":", 1)[0] == session:
                projects.add(pid)
        if not projects:
            return None
        done_ts, keys = 0.0, set()
        for pid in sorted(projects):
            done = conn.execute("SELECT ts FROM fc_project_done WHERE project_id=?",
                                (pid,)).fetchone()
            if not done:
                return None
            for (cid,) in conn.execute("SELECT contract_id FROM fc_task WHERE project_id=?",
                                       (pid,)):
                c = tc.get(cid, conn=conn)
                if not c or c["state"] not in tc.TERMINAL:
                    return None
            if conn.execute("SELECT 1 FROM fc_review WHERE project_id=? AND (status="
                            "'pending' OR (status='decided' AND COALESCE(applied,0)=0))",
                            (pid,)).fetchone():
                return None
            done_ts = max(done_ts, float(done[0] or 0))
            keys.add(pid)
            p = specs.get_project(pid, conn=conn)
            if p and p.get("root"):
                from core import wake_routes
                keys.add(wake_routes.normalize_key(os.path.basename(p["root"].rstrip("/"))))
        since = done_ts + SETTLE_GRACE_SECS
        keys.discard("")
        # idle_prompt notifications are recorded as agent_turn_stopped too, and fire
        # whenever a pane merely sits at the prompt: not new work
        if conn.execute(
                "SELECT 1 FROM event WHERE type='agent_turn_stopped' AND ts_epoch>? AND "
                "COALESCE(json_extract(payload,'$.notification_type'),'')<>'idle_prompt' "
                "AND project_id IN (%s) LIMIT 1" % ",".join("?" * len(keys)),
                (since, *sorted(keys))).fetchone():
            return None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                        "name='agent_watch_state'").fetchone() and conn.execute(
                "SELECT 1 FROM agent_watch_state WHERE (target=? OR substr(target,1,?)=?) "
                "AND digest_since>? LIMIT 1",
                (session, len(session) + 1, session + ":", since)).fetchone():
            return None
        if _delivered_to_since(session, done_ts):      # deliberate: no grace
            return None
        return sorted(projects)[0]
    except Exception:  # noqa: BLE001 — unknown: never suppress a detection
        return None
    finally:
        if own:
            conn.close()


def _delivered_to_since(session: str, since: float) -> bool:
    """Did Owner OS type anything into this agent after `since` (agent_control's
    delivery record)? Unreadable counts as yes: it must never hide later work."""
    import sqlite3
    path = os.getenv("AGENT_CONTROL_DB", "/root/ai-dev-runtime/agent_control.db")
    if not os.path.exists(path):
        return False                      # no delivery record exists at all
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='deliveries'").fetchone():
                return False
            return bool(c.execute(
                "SELECT 1 FROM deliveries WHERE (target=? OR substr(target,1,?)=?) AND "
                "created_ts>? LIMIT 1",
                (session, len(session) + 1, session + ":", since)).fetchone())
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return True
