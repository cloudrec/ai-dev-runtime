"""The full-cycle engine: one persisted, restart-safe loop per project.

    spec ──seed──▶ task (contract CREATED)
                    │  gates.check_dispatch      refused ─▶ OWNER_DECISION (owner gate)
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
immutable history); this module adds the per-attempt dispatch latch, the review inbox
and the project latch, all in the control-plane DB. Every step can be re-run after a
crash: the dispatch latch is written before the send and the adapter's submit is
idempotent per key; a review row is unique per (task, attempt, kind) and its event is
emitted only when the row is first inserted; a recorded decision is applied by the next
tick, and applying is a no-op when the contract has already moved. One engine at a time
per project, by a fenced lease.

What this engine never does: answer an agent's permission dialog, push, deploy, answer
an owner gate, or bind a ChatGPT route.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from core import task_contract as tc
from core.control_plane import api as cp
from core.control_plane.store import now_iso, now_ts
from core.fullcycle import gates
from core.fullcycle import spec as specs
from core.fullcycle.executor import (CRASHED, DONE, WAITING_PERMISSION, ExecutorAdapter,
                                     TaskEnvelope)
from core.fullcycle.hosts import ExecutionHost, LocalHost

SOURCE = "fullcycle"
REVIEW_EVENT = "task_completed"          # a completion-class trigger in core.wake_bridge
OWNER_EVENT = "owner_decision_required"
MAX_ATTEMPTS = int(os.getenv("FULLCYCLE_MAX_ATTEMPTS", "3"))
MAX_DISPATCH_FAILURES = 5
CHECK_TIMEOUT = int(os.getenv("FULLCYCLE_CHECK_TIMEOUT", "300"))
CHECK_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}

# refusals that will not change by retrying: they need the owner
_PERMANENT_REFUSALS = ("hard_denied", "not_allowlisted", "denied", "owner_proof",
                       "unknown target", "no such")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fc_task (
    id TEXT PRIMARY KEY, project_id TEXT, key TEXT, seq INTEGER, spec_version INTEGER,
    body TEXT, contract_id TEXT, attempt INTEGER DEFAULT 0, handle TEXT DEFAULT '',
    feedback TEXT DEFAULT '', verification TEXT DEFAULT '', gate_id TEXT DEFAULT '',
    dispatch_failures INTEGER DEFAULT 0, last_reason TEXT DEFAULT '',
    origin TEXT DEFAULT 'spec', created_ts REAL, updated_ts REAL,
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


def connect(conn=None):
    conn, own = specs.conn_with_schema(conn)
    for stmt in _SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
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
class Engine:
    def __init__(self, project_id: str, adapter: ExecutorAdapter, *,
                 host: Optional[ExecutionHost] = None, holder: str = "fullcycle-engine",
                 lease_ttl: int = 120, max_attempts: int = MAX_ATTEMPTS,
                 commit_on_approve: bool = True, conn=None,
                 clock: Callable[[], float] = now_ts):
        self.project_id, self.adapter = project_id, adapter
        self.host = host or getattr(adapter, "host", None) or LocalHost()
        self.holder, self.lease_ttl, self.max_attempts = holder, lease_ttl, max_attempts
        self.commit_on_approve, self.clock = commit_on_approve, clock
        self.conn, _ = connect(conn)
        p = specs.get_project(project_id, conn=self.conn)
        if not p:
            raise EngineError(f"no such project {project_id!r}")
        self.project = p
        self.root = p["root"]
        self.log: list = []

    # -- small helpers --------------------------------------------------------
    def _note(self, what: str, **kw) -> None:
        self.log.append({"what": what, **kw})

    def _event(self, type_: str, entity_id: str, payload: dict, *, owner: bool = False,
               dedup_key: str = "", severity: str = "high") -> int:
        return cp.append_event(SOURCE, type_, entity_type="fullcycle_task",
                               entity_id=entity_id, payload=payload,
                               project_id=self.project_id, severity=severity,
                               owner_action_required=owner, dedup_key=dedup_key,
                               correlation_id=f"fullcycle:{self.project_id}",
                               conn=self.conn)

    def _tasks(self) -> list:
        rows = self.conn.execute(
            "SELECT id, key, seq, spec_version, body, contract_id, attempt, handle, feedback,"
            " verification, gate_id, dispatch_failures, last_reason, origin"
            " FROM fc_task WHERE project_id=? ORDER BY seq", (self.project_id,)).fetchall()
        out = []
        for r in rows:
            t = dict(zip(("id", "key", "seq", "spec_version", "body", "contract_id", "attempt",
                          "handle", "feedback", "verification", "gate_id",
                          "dispatch_failures", "last_reason", "origin"), r))
            t["body"] = json.loads(t["body"])
            t["handle"] = json.loads(t["handle"]) if t["handle"] else {}
            t["verification"] = json.loads(t["verification"]) if t["verification"] else {}
            t["state"] = tc.get(t["contract_id"], conn=self.conn)["state"]
            out.append(t)
        return out

    def _set(self, task_id: str, **cols) -> None:
        cols["updated_ts"] = self.clock()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE fc_task SET {sets} WHERE id=?", (*cols.values(), task_id))
        self.conn.commit()

    def _move(self, task: dict, to: str, evidence: Optional[dict] = None) -> None:
        if task["state"] == to:
            return
        tc.transition(task["contract_id"], to, by=SOURCE, evidence=evidence, conn=self.conn)
        self._note("transition", task=task["key"], frm=task["state"], to=to)
        task["state"] = to

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
        s = specs.get_spec(self.project_id, conn=self.conn)
        if not s:
            raise EngineError("project has no spec")
        for t in s["body"]["tasks"]:
            self.add_task(t, spec_version=s["version"], origin="spec")

    # -- dispatch -------------------------------------------------------------
    def _envelope(self, task: dict, attempt: int) -> TaskEnvelope:
        c = tc.get(task["contract_id"], conn=self.conn)["contract"]
        base = os.path.join(self.root, ".fullcycle")
        safe = task["key"].replace("/", "_")
        return TaskEnvelope(
            task_id=task["id"], attempt=attempt, project_id=self.project_id,
            workdir=self.root, goal=c["goal"],
            acceptance_criteria=tuple(c["acceptance_criteria"]), scope=tuple(c["scope"]),
            do_not_touch=tuple(c["do_not_touch"]), feedback=task["feedback"] or "",
            envelope_path=os.path.join(base, "tasks", f"{safe}-{attempt}.json"),
            result_path=os.path.join(base, "results", f"{safe}-{attempt}.json"))

    def _dispatch(self, task: dict, attempt: int) -> dict:
        key = f"fullcycle:{task['id']}:{attempt}"
        self.conn.execute("INSERT OR IGNORE INTO fc_dispatch VALUES (?,?,?,?,?,?)",
                          (key, task["id"], attempt, "sending", "", self.clock()))
        self.conn.commit()
        env = self._envelope(task, attempt)
        r = self.adapter.submit(env, key)
        if not r["ok"] and r["reason"] == "agent_not_running" and self.adapter.owns_agent:
            self._note("agent_restart", task=task["key"], why="not_running_at_dispatch",
                       result=self.adapter.restart(r["handle"]))
            r = self.adapter.submit(env, key)
        self.conn.execute("UPDATE fc_dispatch SET status=?, reason=?, ts=? WHERE idem_key=?",
                          ("sent" if r["ok"] else "failed", r["reason"], self.clock(), key))
        self.conn.commit()
        if r["ok"]:
            self._set(task["id"], attempt=attempt, handle=_j(r["handle"]), dispatch_failures=0,
                      last_reason=r["reason"])
            task.update(attempt=attempt, handle=r["handle"])
            self._note("dispatched", task=task["key"], attempt=attempt, already=r["already"])
        return r

    def _dispatch_or_hold(self, task: dict, attempt: int) -> bool:
        """True when the task is now with the agent."""
        r = self._dispatch(task, attempt)
        if r["ok"]:
            self._move(task, tc.WORKING)
            return True
        reason = r["reason"]
        if any(p in reason for p in _PERMANENT_REFUSALS):
            self._owner(task, f"dispatch_refused:{reason}")
            return False
        n = task["dispatch_failures"] + 1
        self._set(task["id"], dispatch_failures=n, last_reason=reason)
        task["dispatch_failures"] = n
        self._note("dispatch_deferred", task=task["key"], reason=reason, failures=n)
        if n >= MAX_DISPATCH_FAILURES:
            self._block_for_owner(task, f"dispatch_failed_{n}x:{reason}")
        return False

    # -- owner / gpt hand-offs ------------------------------------------------
    def _owner(self, task: dict, reason: str, extra: Optional[dict] = None) -> None:
        """Only the owner can lift this: a gate in the control plane + an owner event."""
        if task["state"] not in (tc.OWNER_DECISION,):
            self._move(task, tc.OWNER_DECISION)
        if not task["gate_id"]:
            g = cp.open_gate(work_item_id=task["id"], agent_id=self.adapter.name,
                             reason=reason, kind="fullcycle", conn=self.conn)
            self._set(task["id"], gate_id=g["id"], last_reason=reason)
            task["gate_id"] = g["id"]
            self._event(OWNER_EVENT, task["id"], {"reason": reason, "gate_id": g["id"],
                                                  "task": task["key"], **(extra or {})},
                        owner=True, dedup_key=f"fullcycle:owner:{g['id']}")
        self._note("owner_gate", task=task["key"], reason=reason)

    def _block_for_owner(self, task: dict, reason: str) -> None:
        if task["state"] != tc.BLOCKED:
            self._move(task, tc.BLOCKED)
        self._owner_gate_only(task, reason)

    def _owner_gate_only(self, task: dict, reason: str) -> None:
        if not task["gate_id"]:
            g = cp.open_gate(work_item_id=task["id"], agent_id=self.adapter.name,
                             reason=reason, kind="fullcycle", conn=self.conn)
            self._set(task["id"], gate_id=g["id"], last_reason=reason)
            task["gate_id"] = g["id"]
            self._event(OWNER_EVENT, task["id"], {"reason": reason, "gate_id": g["id"],
                                                  "task": task["key"]},
                        owner=True, dedup_key=f"fullcycle:owner:{g['id']}")
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

    # -- verification ---------------------------------------------------------
    def _git(self, *args) -> tuple:
        return self.host.run(["git", "-C", self.root, *args], timeout=60)

    def _changed_files(self) -> Optional[list]:
        rc, out, _ = self._git("status", "--porcelain=v1", "-uall")
        if rc != 0:
            return None
        files = []
        for line in out.splitlines():
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(path.strip().strip('"'))
        return files

    def _run_checks(self, checks: list) -> list:
        out = []
        for argv in checks:
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
        if changed is None:
            changed, source = result.get("changed_files", []), "agent_claim"
        else:
            source = "git"
        violations = gates.check_changes(changed, scope=c["scope"],
                                         do_not_touch=c["do_not_touch"])
        checks = self._run_checks(task["body"]["checks"])
        passed = not violations and all(x["passed"] for x in checks) and bool(checks)
        reasons = violations + [f"check_failed:{' '.join(x['argv'])}"
                                for x in checks if not x["passed"]]
        if not checks:
            reasons.append("task_has_no_checks")
        return {"passed": passed, "changed_files": changed, "changed_source": source,
                "violations": violations, "checks": checks, "reasons": reasons,
                "summary": result.get("summary", "")}

    # -- one task step --------------------------------------------------------
    def _step(self, task: dict) -> None:
        st = task["state"]
        if st == tc.CREATED:
            c = tc.get(task["contract_id"], conn=self.conn)["contract"]
            refusal = gates.check_dispatch(root=self.root, executor=self.project["executor"],
                                           contract=c)
            if refusal:
                self._owner(task, f"permission_denied:{refusal}")
                return
            self._dispatch_or_hold(task, max(task["attempt"], 0) + 1
                                   if not self._sent(task) else task["attempt"])
        elif st == tc.WORKING:
            self._step_working(task)
        elif st == tc.AGENT_DONE:
            self._step_agent_done(task)
        elif st == tc.VERIFYING:
            if not self._review_for(task, "review") and task["verification"]:
                self._request_review(task, "review", self._review_payload(task))
            elif not task["verification"]:
                # crashed between AGENT_DONE->VERIFYING and recording the verification
                result = self.adapter.collect(task["handle"]) or {}
                self._finish_verification(task, self._verify(task, result))
        elif st == tc.VERIFICATION_FAILED:
            self._retry(task, why="verification_failed")

    def _sent(self, task: dict) -> bool:
        """The current attempt's dispatch already went out (crash after the send)."""
        if not task["attempt"]:
            return False
        r = self.conn.execute("SELECT status FROM fc_dispatch WHERE idem_key=?",
                              (f"fullcycle:{task['id']}:{task['attempt']}",)).fetchone()
        return bool(r and r[0] in ("sent", "sending"))

    def _step_working(self, task: dict) -> None:
        if not task["handle"]:
            # crash between latch and recording the handle: resend the same key
            self._dispatch_or_hold(task, task["attempt"] or 1)
            return
        obs = self.adapter.poll(task["handle"])
        if obs.state == DONE:
            result = self.adapter.collect(task["handle"]) or {}
            if result.get("status") == "blocked":
                self._move(task, tc.BLOCKED)
                self._request_review(task, "blocked", {
                    "question": str(result.get("question") or result.get("summary"))[:4000]})
                return
            self._move(task, tc.AGENT_DONE)
            self._step_agent_done(task, result)
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
                fb = ("The previous attempt ended because the agent process exited. "
                      "Continue from the current state of the workdir.")
                self._set(task["id"], feedback=fb)
                task["feedback"] = fb
                self._dispatch_or_hold(task, task["attempt"] + 1)
        elif obs.state == WAITING_PERMISSION:
            gate = gates.check_permission_prompt(obs.detail.get("command", ""), root=self.root)
            self._owner(task, "agent_permission_prompt", {"permission": gate})

    def _step_agent_done(self, task: dict, result: Optional[dict] = None) -> None:
        if result is None:
            result = self.adapter.collect(task["handle"]) or {}
        self._move(task, tc.VERIFYING)
        self._finish_verification(task, self._verify(task, result))

    def _finish_verification(self, task: dict, v: dict) -> None:
        self._set(task["id"], verification=_j(v))
        task["verification"] = v
        if not v["passed"]:
            self._move(task, tc.VERIFICATION_FAILED, evidence={"verification": v})
            feedback = "Automatic verification failed: " + "; ".join(v["reasons"])
            restored = self._restore_violations(v)
            if restored:
                feedback += ("\nOwner OS restored these files from HEAD because they are "
                             "outside your scope: " + ", ".join(restored))
            for x in v["checks"]:
                if not x["passed"]:
                    feedback += f"\n$ {' '.join(x['argv'])} (rc={x['rc']})\n{x['output'][-800:]}"
            self._set(task["id"], feedback=feedback[:4000])
            task["feedback"] = feedback[:4000]
            self._retry(task, why="verification_failed")
            return
        self._request_review(task, "review", self._review_payload(task))

    def _restore_violations(self, v: dict) -> list:
        """A tracked file the task may not touch is put back as it was at HEAD; left in
        place it would fail every later attempt. Untracked files are not deleted —
        the agent is told to remove them."""
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

    def _review_payload(self, task: dict) -> dict:
        v = task["verification"]
        return {"title": task["body"]["title"], "goal": task["body"]["goal"],
                "acceptance_criteria": task["body"]["acceptance_criteria"],
                "summary": v.get("summary", ""), "changed_files": v.get("changed_files", []),
                "checks": [{"argv": x["argv"], "passed": x["passed"]} for x in v["checks"]]}

    def _retry(self, task: dict, *, why: str) -> None:
        if task["attempt"] >= self.max_attempts:
            self._block_for_owner(task, f"max_attempts_{self.max_attempts}:{why}")
            return
        self._dispatch_or_hold(task, task["attempt"] + 1)

    def _review_for(self, task: dict, kind: str) -> Optional[int]:
        r = self.conn.execute("SELECT id FROM fc_review WHERE project_id=? AND task_id=? AND"
                              " attempt=? AND kind=?",
                              (self.project_id, task["id"], task["attempt"], kind)).fetchone()
        return r[0] if r else None

    # -- decisions --------------------------------------------------------------
    def _apply_decisions(self) -> None:
        rows = self.conn.execute(
            "SELECT id, task_id, attempt, kind, decision, decision_key, decided_by,"
            " decision_body FROM fc_review WHERE project_id=? AND status='decided' AND"
            " applied=0 ORDER BY id", (self.project_id,)).fetchall()
        tasks = {t["id"]: t for t in self._tasks()}
        for rid, task_id, attempt, kind, decision, key, by, body in rows:
            body = json.loads(body or "{}")
            task = tasks.get(task_id)
            review = {"review_id": rid, "by": by, "key": key, "decision": decision,
                      "feedback": body.get("feedback", "")}
            if kind == "project":
                self._apply_project_decision(rid, decision, body)
            elif task is None or task["attempt"] != attempt:
                pass                                    # stale: nothing to apply to
            elif kind == "review" and task["state"] == tc.VERIFYING:
                if decision == "approve":
                    self._move(task, tc.VERIFIED_DONE, evidence={
                        "tests": task["verification"]["checks"], "review": review})
                    self._commit(task)
                else:
                    self._move(task, tc.VERIFICATION_FAILED, evidence={"review": review})
                    fb = "Reviewer rejected: " + review["feedback"]
                    self._set(task_id, feedback=fb)
                    task["feedback"] = fb
            elif kind == "blocked" and task["state"] == tc.BLOCKED:
                if decision == "answer":
                    fb = "Answer from the tech lead: " + review["feedback"]
                    self._set(task_id, feedback=fb)
                    task["feedback"] = fb
                    self._retry(task, why="blocked_answered")
                else:
                    self._move(task, tc.CANCELLED)
            if kind != "project" and body.get("next_tasks"):
                self._add_tasks(body["next_tasks"], origin=f"review:{rid}")
            self.conn.execute("UPDATE fc_review SET applied=1 WHERE id=?", (rid,))
            self.conn.commit()
            self._note("decision_applied", review_id=rid, decision=decision)

    def _add_tasks(self, tasks: list, *, origin: str) -> None:
        cur = specs.get_spec(self.project_id, conn=self.conn)
        body = dict(cur["body"])
        have = {t["key"] for t in body["tasks"]}
        new = [t for t in tasks if t["key"] not in have]
        if new:
            body["tasks"] = body["tasks"] + new
            cur = specs.put_spec(self.project_id, body, by=origin, conn=self.conn)
        for t in tasks:
            self.add_task(t, spec_version=cur["version"], origin=origin)

    def _apply_project_decision(self, rid: int, decision: str, body: dict) -> None:
        if decision == "add_tasks":
            self._add_tasks(body.get("next_tasks") or [], origin=f"review:{rid}")
        else:
            self._event(OWNER_EVENT, f"{self.project_id}:__project__",
                        {"reason": "project_acceptance_escalated", "review_id": rid,
                         "feedback": body.get("feedback", "")},
                        owner=True, dedup_key=f"fullcycle:escalate:{rid}")

    def _commit(self, task: dict) -> None:
        if not self.commit_on_approve or self._git("rev-parse", "--git-dir")[0] != 0:
            return
        self._git("add", "-A", "--", ".", ":(exclude).fullcycle")
        rc, out, err = self.host.run(
            ["git", "-C", self.root, "-c", "user.name=Owner OS fullcycle",
             "-c", "user.email=fullcycle@localhost", "commit", "-q", "--no-verify", "-m",
             f"fullcycle: {task['key']} (attempt {task['attempt']}) — {task['body']['title']}"],
            timeout=60)
        self._note("committed", task=task["key"], rc=rc, out=(out + err).strip()[-200:])

    # -- owner gates answered ----------------------------------------------------
    def _owner_answers(self) -> None:
        for task in self._tasks():
            if not task["gate_id"] or task["state"] not in (tc.OWNER_DECISION, tc.BLOCKED):
                continue
            r = self.conn.execute("SELECT state, answer FROM owner_gate WHERE id=?",
                                  (task["gate_id"],)).fetchone()
            if not r or r[0] != "answered":
                continue
            answer = (r[1] or "").strip().lower()
            self.conn.execute("UPDATE owner_gate SET state='resolved' WHERE id=?",
                              (task["gate_id"],))
            self._set(task["id"], gate_id="")
            task["gate_id"] = ""
            reason = task["last_reason"]
            if answer in ("cancel", "stop"):
                self._move(task, tc.CANCELLED)
            elif answer in ("resume", "retry", "approve"):
                if reason == "agent_permission_prompt" and task["handle"]:
                    # the owner dealt with the dialog in the pane: keep watching the
                    # SAME attempt; a new envelope would orphan the work in progress
                    self._move(task, tc.WORKING)
                elif not task["attempt"]:
                    # refused before it ever reached the agent: the gates are checked
                    # again — an owner "resume" is not a waiver of a denied root
                    c = tc.get(task["contract_id"], conn=self.conn)["contract"]
                    refusal = gates.check_dispatch(root=self.root,
                                                   executor=self.project["executor"],
                                                   contract=c)
                    if refusal:
                        self._owner(task, f"permission_denied:{refusal}")
                    else:
                        self._dispatch_or_hold(task, 1)
                else:
                    self._dispatch_or_hold(task, task["attempt"] + 1)
            self._note("owner_answer_applied", task=task["key"], answer=answer)

    # -- project completion ------------------------------------------------------
    def _project_step(self, tasks: list) -> Optional[str]:
        if self.conn.execute("SELECT 1 FROM fc_project_done WHERE project_id=?",
                             (self.project_id,)).fetchone():
            return "done"
        if not tasks or any(t["state"] not in tc.TERMINAL for t in tasks):
            return None
        s = specs.get_spec(self.project_id, conn=self.conn)
        checks = self._run_checks(s["body"]["checks"])
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
            # the event every tick; it waits for the owner (escalate) or for new tasks
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

    # -- the tick ------------------------------------------------------------------
    def tick(self) -> dict:
        lease = cp.acquire_lease(f"fullcycle:{self.project_id}", self.holder,
                                 ttl_secs=self.lease_ttl, now=self.clock(), conn=self.conn)
        if not lease:
            return {"skipped": "lease_held", "holder": cp.lease_holder(
                f"fullcycle:{self.project_id}", conn=self.conn)}
        self.log = []
        self._seed()
        self._apply_decisions()
        self._owner_answers()
        tasks = self._tasks()
        done_keys = {t["key"] for t in tasks if t["state"] == tc.VERIFIED_DONE}
        in_flight = [t for t in tasks if t["state"] not in tc.TERMINAL
                     and t["state"] != tc.CREATED]
        for t in in_flight:
            self._step(t)
        # single executor: the next task starts only when nothing else is in flight
        tasks = self._tasks()
        if not any(t["state"] not in tc.TERMINAL and t["state"] != tc.CREATED for t in tasks):
            for t in tasks:
                if t["state"] == tc.CREATED and set(t["body"]["depends_on"]) <= done_keys:
                    self._step(t)
                    break
        tasks = self._tasks()
        project = self._project_step(tasks)
        return {"project": project, "tasks": {t["key"]: t["state"] for t in tasks},
                "log": self.log, "fence": lease["fence_token"]}

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
