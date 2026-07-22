"""orchestrator_plan — durable goal → plan → queue → dispatch → completion → next."""
from __future__ import annotations

import pytest

from core import orchestrator_plan as plan


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "plan.db"))


def test_idle_no_goal():
    t = plan.tick(dispatch=True)
    assert t["state"] == "ORCHESTRATOR_IDLE_NO_GOAL" and t["goal"] is None


def test_goal_is_durable_and_idempotent():
    g1 = plan.set_goal("finish X")
    assert g1["created"] is True
    g2 = plan.set_goal("finish X")               # same text → same row, idempotent
    assert g2["created"] is False and g2["id"] == g1["id"]
    assert plan.get_active_goal()["text"] == "finish X"


def test_dispatch_then_complete_then_next(monkeypatch, tmp_path):
    g = plan.set_goal("build orchestrator")
    m1 = tmp_path / "t1.done"
    t1 = plan.add_task(g["id"], "owneros", "subtask 1", "do subtask 1", agent="owneros-direct-fix",
                       order_index=1, completion_marker=str(m1))
    t2 = plan.add_task(g["id"], "owneros", "subtask 2", "do subtask 2", agent="owneros-direct-fix",
                       order_index=2, depends_on=[t1])
    sent = []
    cbs = dict(agent_available=lambda a: True, send=lambda a, txt: sent.append((a, txt)) or {"ok": True})

    # tick 1: t1 dispatched (t2 blocked on t1).
    r1 = plan.tick(dispatch=True, **cbs)
    assert r1["state"] == "ORCHESTRATING"
    assert [d["task_id"] for d in r1["dispatches"]] == [t1]
    assert sent == [("owneros-direct-fix", "do subtask 1")]
    assert any(b["task_id"] == t2 and "dependencies" in b["reason"] for b in r1["blockers"])

    # tick 2: t1 not yet complete (no marker) → no new dispatch, no re-dispatch.
    sent.clear()
    r2 = plan.tick(dispatch=True, **cbs)
    assert r2["dispatches"] == [] and sent == []

    # subtask 1 completes (real marker) → tick 3 records completion + dispatches t2.
    m1.write_text("done")
    sent.clear()
    r3 = plan.tick(dispatch=True, **cbs)
    assert [c["task_id"] for c in r3["completions"]] == [t1]
    assert [d["task_id"] for d in r3["dispatches"]] == [t2]
    assert sent == [("owneros-direct-fix", "do subtask 2")]

    # last_dispatch / last_completion recorded.
    s = plan.status()
    assert "task#%d" % t2 in (s["last_dispatch"] or "")
    assert "task#%d" % t1 in (s["last_completion"] or "")


def test_never_dispatch_onto_busy_agent(monkeypatch, tmp_path):
    g = plan.set_goal("g")
    t1 = plan.add_task(g["id"], "p", "task", "do it", agent="seo-audit")
    sent = []
    r = plan.tick(dispatch=True, agent_available=lambda a: False,   # agent busy
                  send=lambda a, txt: sent.append(1) or {"ok": True})
    assert sent == [] and r["dispatches"] == []
    assert any(i["agent"] == "seo-audit" for i in r["idle_reasons"])


def test_waiting_external_and_no_false_restart(tmp_path):
    # SEO internal complete + waiting external → not re-dispatched, surfaced, and a
    # completed task is never restarted (no work invented).
    g = plan.set_goal("g")
    t_seo = plan.add_task(g["id"], "seo", "SEO internal build", "n/a", status="waiting_external")
    t_done = plan.add_task(g["id"], "seo", "earlier subtask", "n/a", status="completed")
    sent = []
    r = plan.tick(dispatch=True, agent_available=lambda a: True,
                  send=lambda a, txt: sent.append(1) or {"ok": True})
    assert sent == []                                    # nothing dispatched
    assert "SEO internal build" in r["waiting_external"]
    assert "earlier subtask" in r["completed"]


def test_goal_complete_when_no_pending(tmp_path):
    g = plan.set_goal("g")
    plan.add_task(g["id"], "p", "only task", "n/a", status="completed")
    r = plan.tick(dispatch=True, agent_available=lambda a: True, send=lambda a, t: {"ok": True})
    assert r["state"].startswith("GOAL_COMPLETE")
    assert plan.get_active_goal() is None                # goal marked complete
