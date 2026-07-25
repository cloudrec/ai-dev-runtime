"""Watcher / stuchalka — idle-unfinished detection, safe same-conversation resume,
no duplicate agents, and alert dedup by (agent, condition, evidence_hash)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from core import agent_watcher as w


def _iso(delta_secs: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_secs)).isoformat(timespec="seconds")


def _task(status="dispatched", dispatched_secs_ago=600, tid=7):
    return {"id": tid, "title": "T", "task_text": "do the thing",
            "status": status, "dispatched_at": _iso(-dispatched_secs_ago)}


NOW = datetime.now(timezone.utc).timestamp()


# ── detection ────────────────────────────────────────────────────────────────
def test_idle_with_unfinished_task_past_dwell_is_a_stall():
    s = w.detect(agent_key="email:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW)
    assert s and s["condition"] == "idle_unfinished" and s["task_id"] == 7


def test_idle_but_within_dwell_is_not_a_stall():
    s = w.detect(agent_key="email:0.0", alive=True, state="idle",
                 assigned_task=_task(dispatched_secs_ago=10), now_ts=NOW)
    assert s is None                              # just between-turn latency


def test_working_agent_is_never_a_stall():
    assert w.detect(agent_key="a:0.0", alive=True, state="working",
                    assigned_task=_task(), now_ts=NOW) is None


def test_completed_or_no_task_is_not_a_stall():
    assert w.detect(agent_key="a:0.0", alive=True, state="idle",
                    assigned_task=_task(status="completed"), now_ts=NOW) is None
    assert w.detect(agent_key="a:0.0", alive=True, state="idle",
                    assigned_task=None, now_ts=NOW) is None


def test_exited_agent_with_unfinished_task_is_a_stall():
    s = w.detect(agent_key="seo-audit:0.0", alive=False, state="exited",
                 assigned_task=_task(), now_ts=NOW)
    assert s and s["condition"] == "exited_unfinished"


# ── decision: resume same conversation vs one owner blocker ──────────────────
def test_idle_auto_resumes_same_conversation_no_duplicate():
    s = w.detect(agent_key="email:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, resume_count=0)
    d = w.decide(s, mode="auto", approve=True, budget_locked=False)
    assert d["action"] == "resume" and d["resume_count"] == 1   # same pane, not a new agent


def test_exited_never_resumes_only_owner_blocker():
    s = w.detect(agent_key="seo-audit:0.0", alive=False, state="exited",
                 assigned_task=_task(), now_ts=NOW)
    d = w.decide(s, mode="auto", approve=True, budget_locked=False)
    assert d["action"] == "owner_blocker" and "duplicate" in d["reason"]


def test_non_auto_or_budget_locked_does_not_resume():
    s = w.detect(agent_key="x:0.0", alive=True, state="idle", assigned_task=_task(), now_ts=NOW)
    assert w.decide(s, mode="monitor", approve=True, budget_locked=False)["action"] == "owner_blocker"
    assert w.decide(s, mode="auto", approve=True, budget_locked=True)["action"] == "owner_blocker"


def test_resume_budget_exhausted_becomes_owner_blocker():
    s = w.detect(agent_key="x:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, resume_count=w.MAX_RESUMES)
    assert w.decide(s, mode="auto", approve=True, budget_locked=False)["action"] == "owner_blocker"


# ── alert dedup: same condition/evidence → same key; any change → new key ─────
def test_dedup_key_stable_when_nothing_changed():
    a = w.detect(agent_key="e:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, pane_tail="line-1\nline-2")
    b = w.detect(agent_key="e:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, pane_tail="line-1\nline-2")
    assert w.dedup_key(a) == w.dedup_key(b)          # unchanged → suppressed (no re-alert)


def test_crash_and_idle_unfinished_classify_as_recovery_failure_not_owner_decision():
    # A crash / failed-resume / idle-with-unfinished-work must map to
    # agent_recovery_failure — NEVER an owner decision request.
    exited = w.detect(agent_key="email:0.0", alive=False, state="exited",
                      assigned_task=_task(), now_ts=NOW)
    d_exit = w.decide(exited, mode="auto", approve=True, budget_locked=False)
    assert w.event_type(d_exit) == w.EVENT_RECOVERY_FAILURE == "agent_recovery_failure"

    idle_stuck = w.detect(agent_key="email:0.0", alive=True, state="idle",
                          assigned_task=_task(), now_ts=NOW, resume_count=w.MAX_RESUMES)
    d_idle = w.decide(idle_stuck, mode="auto", approve=True, budget_locked=False)
    assert w.event_type(d_idle) == "agent_recovery_failure"
    assert "owner_decision" not in w.event_type(d_idle)


def test_successful_resume_classifies_as_recovered_not_failure():
    s = w.detect(agent_key="e:0.0", alive=True, state="idle", assigned_task=_task(), now_ts=NOW)
    d = w.decide(s, mode="auto", approve=True, budget_locked=False)
    assert w.event_type(d) == w.EVENT_RESUMED == "agent_resumed_same_conversation"


def test_dedup_key_changes_when_evidence_changes():
    a = w.detect(agent_key="e:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, pane_tail="before")
    b = w.detect(agent_key="e:0.0", alive=True, state="idle",
                 assigned_task=_task(), now_ts=NOW, pane_tail="after — context grew a lot")
    assert w.dedup_key(a) != w.dedup_key(b)          # change → new key → notify
