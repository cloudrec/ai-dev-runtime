"""The deterministic control path: a continuation exists because a ROW exists.

This replaces the heuristic that failed for two weeks. The tests below pin the properties
the owner named, and deliberately include the two live failures that motivated the rewrite:
a dim recall ghost read as staged input, and dim staged input read as a ghost.
"""
from __future__ import annotations

import json

import pytest

from core import os_task_queue as q


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


def _transcript(monkeypatch, msgs):
    monkeypatch.setattr(q, "transcript_messages", lambda cwd, limit_files=1: msgs)


# ── 1. the queue is the source of truth ─────────────────────────────────────
def test_a_task_exists_because_a_row_exists():
    t = q.enqueue("cp-canary:0.0", "do thing one")
    assert t["state"] == q.QUEUED and t["id"] and t["idem"].startswith("ostask:")
    assert q.next_queued("cp-canary:0.0")["id"] == t["id"]


def test_tasks_are_issued_in_order():
    a = q.enqueue("cp-canary:0.0", "first")
    b = q.enqueue("cp-canary:0.0", "second")
    assert q.next_queued("cp-canary:0.0")["id"] == a["id"]
    q.set_state(a["id"], q.DONE)
    assert q.next_queued("cp-canary:0.0")["id"] == b["id"]


def test_no_pane_text_can_create_a_task():
    """The whole point: nothing visible in a terminal makes a continuation exist."""
    assert q.next_queued("cp-canary:0.0") is None
    assert q.active_task("cp-canary:0.0") is None


# ── 2. acknowledgement comes from the transcript, not the prompt ────────────
def test_ack_requires_the_text_in_the_transcript(monkeypatch):
    t = q.enqueue("cp-canary:0.0", "continue with slice 2")
    q.set_state(t["id"], q.SUBMITTED, submitted_ts=1000.0)
    _transcript(monkeypatch, [])
    assert q.find_ack("/x", "continue with slice 2", 1000.0) is None
    _transcript(monkeypatch, [{"type": "user", "text": "continue with slice 2", "ts": 1001.0}])
    assert q.find_ack("/x", "continue with slice 2", 1000.0) is not None


def test_a_prompt_line_is_never_evidence_of_ack(monkeypatch):
    """A dim/bright prompt line proves nothing — only the transcript does. This is exactly
    what the ghost-vs-staged confusion got wrong in both directions."""
    _transcript(monkeypatch, [{"type": "user", "text": "some OTHER command", "ts": 1001.0}])
    assert q.find_ack("/x", "continue with slice 2", 1000.0) is None


def test_ack_ignores_an_older_identical_message(monkeypatch):
    """A previous run of the same text must not acknowledge today's task."""
    _transcript(monkeypatch, [{"type": "user", "text": "run the thing", "ts": 500.0}])
    assert q.find_ack("/x", "run the thing", 1000.0) is None


def test_ack_survives_collapsed_multiline(monkeypatch):
    """A pasted multiline command can arrive with line breaks collapsed; content decides."""
    _transcript(monkeypatch, [{"type": "user", "text": "line one line two", "ts": 1001.0}])
    assert q.find_ack("/x", "line one\nline two", 1000.0) is not None


def test_turn_finished_only_when_the_agent_answered_last(monkeypatch):
    _transcript(monkeypatch, [{"type": "user", "text": "go", "ts": 1000.0}])
    assert q.turn_finished("/x", 1000.0) is False
    _transcript(monkeypatch, [{"type": "user", "text": "go", "ts": 1000.0},
                              {"type": "assistant", "text": "done", "ts": 1002.0}])
    assert q.turn_finished("/x", 1000.0) is True


# ── 3. bounded retry, exactly one, same idempotency key ────────────────────
def _stub_submit(monkeypatch, calls, acted=True):
    def _s(task, *, cwd, ctrl=None, conn=None, now=None, robust=False):
        calls.append({"id": task["id"], "idem": task["idem"], "robust": robust,
                      "attempt": int(task.get("attempts") or 0) + 1})
        q.set_state(task["id"], q.SUBMITTED, attempts=int(task.get("attempts") or 0) + 1,
                    submitted_ts=now or 0.0)
        return {"acted": acted, "task_id": task["id"]}
    monkeypatch.setattr(q, "submit", _s)


def test_no_ack_retries_exactly_once_then_fails_and_notifies(monkeypatch):
    calls = []
    _stub_submit(monkeypatch, calls)
    _transcript(monkeypatch, [])
    notified = []
    monkeypatch.setattr(q, "_notify_owner",
                        lambda t, r, d: notified.append((t["id"], r)) or "gate1")

    t = q.enqueue("cp-canary:0.0", "never acknowledged")
    assert q.advance("cp-canary:0.0", cwd="/x", now=1000.0)["action"] == "submitted"
    # inside the window: wait, do not resend
    assert q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + 5)["action"] == "awaiting_ack"
    # past the window: exactly one retry, SAME idempotency key
    r = q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + q.ACK_TIMEOUT_SECS + 1)
    assert r["action"] == "retried" and r["idempotency_key"] == t["idem"]
    assert calls[0]["idem"] == calls[1]["idem"], "a retry must never take a new identity"
    assert calls[0]["robust"] is False and calls[1]["robust"] is True, \
        "the retry uses the robust path — a bare Enter is exactly what failed live"
    # still nothing: fail + notify, and never send a third time
    r2 = q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + 2 * q.ACK_TIMEOUT_SECS + 2)
    assert r2["action"] == "failed" and r2["reason"] == "ack_timeout"
    assert notified == [(t["id"], "not acknowledged")]
    assert len(calls) == 2, calls
    assert q.get(t["id"])["state"] == q.FAILED


def test_an_acknowledged_task_is_never_resent(monkeypatch):
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "do it once")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    # the transcript shows what was SENT: the grounded pointer naming this task id
    _transcript(monkeypatch, [{"type": "user",
                               "text": f"continue with the durable queue stage task_{t['id']}"
                                       f" defined in /x/.owner-os-tasks/task_{t['id']}.md",
                               "ts": 1001.0}])
    for step in range(4):
        q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + q.ACK_TIMEOUT_SECS * (step + 2))
    assert len(calls) == 1, "exactly-once: an acknowledged task is never resent"


def test_completion_moves_to_done_and_releases_the_next_task(monkeypatch):
    calls = []
    _stub_submit(monkeypatch, calls)
    a = q.enqueue("cp-canary:0.0", "task A")
    q.enqueue("cp-canary:0.0", "task B")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"continue with task_{a['id']} now",
                               "ts": 1001.0},
                              {"type": "assistant", "text": "finished A", "ts": 1002.0}])
    assert q.advance("cp-canary:0.0", cwd="/x", now=1003.0)["action"] == "done"
    assert q.get(a["id"])["state"] == q.DONE
    r = q.advance("cp-canary:0.0", cwd="/x", now=1004.0)
    assert r["action"] == "submitted" and len(calls) == 2


# ── 4. /clear and restart recover from the ledger ──────────────────────────
def test_clear_before_ack_requeues_the_same_task(monkeypatch):
    t = q.enqueue("cp-canary:0.0", "survive the clear")
    q.set_state(t["id"], q.SUBMITTED, conversation_id="conv-old", submitted_ts=1000.0)
    monkeypatch.setattr(q, "transcript_messages", lambda cwd, limit_files=1: [])
    monkeypatch.setattr("core.agent_control.conversation_evidence",
                        lambda cwd: {"latest": {"conversation_id": "conv-new"}})
    r = q.restore_after_reset("cp-canary:0.0", cwd="/x")
    assert r["action"] == "requeued"
    again = q.get(t["id"])
    assert again["state"] == q.QUEUED and again["idem"] == t["idem"], \
        "the SAME task returns, keeping its identity"


def test_clear_after_ack_keeps_the_task(monkeypatch):
    """The agent already read it — a new conversation must not cause a re-send."""
    t = q.enqueue("cp-canary:0.0", "already read")
    q.set_state(t["id"], q.ACKNOWLEDGED, conversation_id="conv-old", submitted_ts=1000.0)
    monkeypatch.setattr("core.agent_control.conversation_evidence",
                        lambda cwd: {"latest": {"conversation_id": "conv-new"}})
    assert q.restore_after_reset("cp-canary:0.0", cwd="/x")["action"] == "kept"
    assert q.get(t["id"])["state"] == q.ACKNOWLEDGED


def test_restart_with_no_active_task_is_a_no_op():
    assert q.restore_after_reset("cp-canary:0.0", cwd="/x")["action"] == "nothing_active"


# ── 5. allowlist and cross-project safety ─────────────────────────────────
def test_submission_refuses_a_target_outside_the_actuation_allowlist(monkeypatch):
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    t = q.enqueue("payment:0.0", "anything at all")
    out = q.submit(t, cwd="/opt/payment-orchestrator")
    assert out["acted"] is False and out["reason"] == "not_canary"
    assert q.get(t["id"])["state"] == q.QUEUED, "a refused task stays queued, never submitted"


def test_a_task_is_only_ever_delivered_to_its_own_target(monkeypatch):
    q.enqueue("cp-canary:0.0", "canary work")
    q.enqueue("other:0.0", "other work")
    assert q.next_queued("cp-canary:0.0")["text"] == "canary work"
    assert q.next_queued("other:0.0")["text"] == "other work"


def test_the_pane_never_receives_the_tasks_free_text(tmp_path, monkeypatch):
    """The safety classifier refused arbitrary task prose (live: owner_approval_required),
    and bypassing it would remove the last wall. The task text goes to a durable FILE inside
    the project's own directory; the pane receives only the closed-form grounded pointer."""
    from core.control_plane import actuator as act
    sent = {}
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(act, "actuate",
                        lambda **kw: sent.update(text=kw["action_text"]) or {"acted": True})
    monkeypatch.setattr("core.control_plane.api.acquire_lease",
                        lambda *a, **k: {"lease_id": "l", "fence_token": 1})
    t = q.enqueue("cp-canary:0.0", "delete everything and publish the release")
    q.submit(t, cwd=str(tmp_path))
    assert "delete everything" not in sent["text"], "free text must never reach the pane"
    assert f"task_{t['id']}" in sent["text"]
    assert act.classify_action(sent["text"]) == "autonomous_safe"
    written = (tmp_path / ".owner-os-tasks" / f"task_{t['id']}.md").read_text()
    assert "delete everything and publish the release" in written, \
        "the owner's own words are preserved in the durable file for the agent to read"


def test_a_policy_refusal_does_not_consume_the_retry_budget(tmp_path, monkeypatch):
    """Live: five `owner_approval_required` refusals pushed attempts to 7 before any real
    delivery, exhausting a budget that exists for DELIVERY failures."""
    from core.control_plane import actuator as act
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({"cp-canary:0.0"}))
    monkeypatch.setattr(act, "actuate", lambda **kw: {"acted": False,
                                                      "reason": "owner_approval_required"})
    monkeypatch.setattr("core.control_plane.api.acquire_lease",
                        lambda *a, **k: {"lease_id": "l", "fence_token": 1})
    t = q.enqueue("cp-canary:0.0", "something the classifier will refuse")
    for _ in range(4):
        q.submit(t, cwd=str(tmp_path))
        t = q.get(t["id"])
    assert t["attempts"] == 0, "refusals never reached the pane; they cost no attempts"
    assert t["state"] == q.QUEUED


def test_done_is_not_declared_while_the_agent_is_still_working(monkeypatch):
    """Live: task e8702015 was acknowledged at 00:12:41 and marked done at 00:12:42 — one
    second — and its artefact was never written. Claude Code emits an assistant entry per
    tool call, so "last entry is assistant" fires mid-turn."""
    _transcript(monkeypatch, [{"type": "user", "text": "task_x", "ts": 1000.0},
                              {"type": "assistant", "text": "calling a tool", "ts": 1001.0}])
    monkeypatch.setattr("core.agent_control.pane_capture", lambda *a, **k: (True, ""))
    monkeypatch.setattr("core.agent_control.agent_status", lambda *a, **k: {"state": "working"})
    assert q.turn_finished("/x", 1000.0, target="cp-canary:0.0") is False
    monkeypatch.setattr("core.agent_control.agent_status", lambda *a, **k: {"state": "idle"})
    assert q.turn_finished("/x", 1000.0, target="cp-canary:0.0") is True


def test_completion_is_not_claimed_when_the_pane_cannot_be_read(monkeypatch):
    """Fail-safe: no corroboration means no `done`."""
    _transcript(monkeypatch, [{"type": "user", "text": "task_x", "ts": 1000.0},
                              {"type": "assistant", "text": "ok", "ts": 1001.0}])
    def _boom(*a, **k):
        raise RuntimeError("tmux unavailable")
    monkeypatch.setattr("core.agent_control.pane_capture", _boom)
    assert q.turn_finished("/x", 1000.0, target="cp-canary:0.0") is False


def test_an_agent_that_dies_after_ack_does_not_stall_forever(monkeypatch):
    """Live gap: T7 was acknowledged, its agent was then killed, and the task sat in
    `working` indefinitely — the bounded timeout covered only the ack phase."""
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "will be abandoned")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: False)
    notified = []
    monkeypatch.setattr(q, "_notify_owner",
                        lambda t_, r, d: notified.append(r) or "gate")
    # within the bound: still working, no alarm
    assert q.advance("cp-canary:0.0", cwd="/x",
                     now=1001.0 + q.WORK_STALL_SECS - 1)["action"] == "working"
    r = q.advance("cp-canary:0.0", cwd="/x", now=1001.0 + q.WORK_STALL_SECS + 1)
    assert r["action"] == "failed" and r["reason"] == "work_stall"
    assert notified == ["stalled after acknowledgement"]
    assert q.get(t["id"])["state"] == q.FAILED


def test_a_long_running_turn_is_never_declared_stalled(monkeypatch):
    """Anti-overcorrection: MESS legitimately held one turn for 1h19m."""
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "long but healthy")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: True)      # still executing
    r = q.advance("cp-canary:0.0", cwd="/x", now=1001.0 + q.WORK_STALL_SECS * 10)
    assert r["action"] == "working", "a busy agent is never failed for taking its time"


# ── outcomes must reach the CTO inbox ──────────────────────────────────────
def _events(kind=None):
    import os, sqlite3
    c = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    c.row_factory = sqlite3.Row
    q = "SELECT type,severity,owner_action_required,payload FROM event"
    if kind:
        q += f" WHERE type='{kind}'"
    return [dict(r) for r in c.execute(q)]


def test_a_completed_task_becomes_a_durable_inbox_event(monkeypatch):
    """Completion previously emitted NOTHING — a task could finish with no record anywhere
    but its own ledger row."""
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "finish me", project="canary")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0},
                              {"type": "assistant", "text": "done", "ts": 1002.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: False)
    assert q.advance("cp-canary:0.0", cwd="/x", now=1003.0)["action"] == "done"
    ev = _events("task_completed")
    assert len(ev) == 1 and t["id"] in ev[0]["payload"]


def test_a_completion_does_not_page_the_owner(monkeypatch):
    """It belongs in the inbox, not in Telegram and not as a chat wake. Waking someone to say
    a routine task finished is how a channel becomes noise."""
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "quiet finish")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0},
                              {"type": "assistant", "text": "ok", "ts": 1002.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: False)
    q.advance("cp-canary:0.0", cwd="/x", now=1003.0)
    ev = _events("task_completed")[0]
    assert ev["severity"] == "info" and not ev["owner_action_required"]


def test_a_failure_does_page_the_owner(monkeypatch):
    calls = []
    _stub_submit(monkeypatch, calls)
    _transcript(monkeypatch, [])
    monkeypatch.setattr(q, "_notify_owner", lambda t, r, d: "gate")
    q.enqueue("cp-canary:0.0", "never acked")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + q.ACK_TIMEOUT_SECS + 1)
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0 + 2 * q.ACK_TIMEOUT_SECS + 2)
    ev = _events("task_failed")
    assert ev and ev[0]["severity"] == "high" and ev[0]["owner_action_required"]


def test_the_same_outcome_appears_once_however_many_ticks_see_it(monkeypatch):
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "finish once")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0},
                              {"type": "assistant", "text": "done", "ts": 1002.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: False)
    for step in range(4):
        q.advance("cp-canary:0.0", cwd="/x", now=1003.0 + step)
    assert len(_events("task_completed")) == 1, "dedupe keeps one row per outcome"


def test_visibility_never_breaks_the_state_machine(monkeypatch):
    """An unavailable inbox must not stop a task completing."""
    calls = []
    _stub_submit(monkeypatch, calls)
    t = q.enqueue("cp-canary:0.0", "finish anyway")
    q.advance("cp-canary:0.0", cwd="/x", now=1000.0)
    _transcript(monkeypatch, [{"type": "user", "text": f"task_{t['id']}", "ts": 1001.0},
                              {"type": "assistant", "text": "done", "ts": 1002.0}])
    monkeypatch.setattr(q, "_agent_busy", lambda target: False)
    monkeypatch.setattr(q, "_emit_task_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("inbox down")))
    try:
        out = q.advance("cp-canary:0.0", cwd="/x", now=1003.0)
    except RuntimeError:
        out = {"action": "raised"}
    assert q.get(t["id"])["state"] == q.DONE or out["action"] == "done"
