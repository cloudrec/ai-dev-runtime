"""DIRECT AGENTS truthful snapshot (build_direct_agents)."""
from __future__ import annotations

from core.agent_orchestrator import build_direct_agents

NOW = 1_000_000.0


def _agent(target, state, cwd, alive=True, is_agent=True, queued="", last_line=""):
    return {"target": target, "is_agent": is_agent, "alive": alive, "state": state,
            "claude_cwd": cwd, "queued_input": queued, "last_pane_line": last_line}


def _rec(task=None, blocker=None, age_s=None, phase=None, completion=None, goal=None):
    return {"current_task": task, "blocker_text": blocker, "phase": phase,
            "approved_goal": goal, "completion_evidence": completion,
            "last_fresh_activity_ts": (NOW - age_s) if age_s is not None else None}


def _one(agents, records):
    return build_direct_agents(agents, records, NOW)[0]


def test_working_is_not_idle_and_shows_task():
    d = _one([_agent("email:0.0", "working", "/opt/email")],
             {"email:0.0": _rec(task="warmup send", age_s=30)})
    assert d["bucket"] == "working" and d["state"] == "working"
    assert d["current_task"] == "warmup send" and d["last_activity_age_s"] == 30


def test_idle_bucket_is_idle():
    d = _one([_agent("a:0.0", "idle", "/opt/a")], {"a:0.0": _rec(age_s=500)})
    assert d["bucket"] == "idle" and d["state"] == "idle"


def test_shell_running_counts_as_working():
    d = _one([_agent("cap:0.0", "shell_running", "/opt/capacity")], {})
    assert d["bucket"] == "working"


def test_stale_and_dead_map_to_dead():
    stale = _one([_agent("s:0.0", "stale", "/opt/s")], {})
    dead = _one([_agent("d:0.0", "dead", "/opt/d", alive=False)], {})
    assert stale["bucket"] == "dead" and dead["bucket"] == "dead" and dead["alive"] is False


def test_waiting_input_shows_queued_command():
    d = _one([_agent("q:0.0", "waiting_input", "/opt/q", queued="[Pasted text #1 +9 lines]")], {})
    assert d["bucket"] == "waiting" and d["queued_input"] == "[Pasted text #1 +9 lines]"


def test_externally_blocked_is_owner_action_not_stalled():
    d = _one([_agent("b:0.0", "externally_blocked", "/opt/b")],
             {"b:0.0": _rec(blocker="vendor key required")})
    assert d["owner_action"] is True and d["bucket"] == "waiting"
    assert d["blocker"] == "vendor key required"


def test_no_conversation_flagged():
    d = _one([_agent("new:0.0", "idle", "/opt/new")], {})     # no record at all
    assert d["has_conversation"] is False and d["current_task"] is None


def test_duplicate_cwd_flagged_for_both():
    ds = build_direct_agents(
        [_agent("x:0.0", "working", "/opt/dup"), _agent("y:0.0", "idle", "/opt/dup"),
         _agent("z:0.0", "working", "/opt/solo")], {}, NOW)
    by = {d["target"]: d for d in ds}
    assert by["x:0.0"]["duplicate_cwd"] and by["y:0.0"]["duplicate_cwd"]
    assert not by["z:0.0"]["duplicate_cwd"]


def test_non_agent_pane_skipped():
    ds = build_direct_agents([_agent("sh:0.0", "idle", "/tmp", is_agent=False)], {}, NOW)
    assert ds == []


def test_secret_redaction_applied_to_task_and_blocker():
    red = lambda s: (s or "").replace("sk-SECRET", "***")
    ds = build_direct_agents(
        [_agent("a:0.0", "externally_blocked", "/opt/a")],
        {"a:0.0": _rec(task="use token sk-SECRET now", blocker="need sk-SECRET")}, NOW, redact=red)
    assert "sk-SECRET" not in ds[0]["current_task"] and "***" in ds[0]["current_task"]
    assert "sk-SECRET" not in ds[0]["blocker"]


def test_completion_evidence_last_result():
    d = _one([_agent("a:0.0", "idle", "/opt/a")],
             {"a:0.0": _rec(completion='{"report_path": "reports/DONE.md"}')})
    assert d["last_result"] == "reports/DONE.md" and d["has_conversation"] is True


# ── current-task from transcript (item 1) ────────────────────────────────────
def test_task_lookup_used_only_when_record_has_no_task():
    lookups = []
    def lk(cwd):
        lookups.append(cwd)
        return "warmup send"
    # record HAS a task → lookup NOT called; task_source=record
    d1 = build_direct_agents([_agent("a:0.0", "working", "/opt/a")],
                             {"a:0.0": _rec(task="orch task")}, NOW, task_lookup=lk)[0]
    assert d1["current_task"] == "orch task" and d1["task_source"] == "record"
    # record has NO task → lookup used; task_source=transcript
    d2 = build_direct_agents([_agent("b:0.0", "working", "/opt/b")], {}, NOW, task_lookup=lk)[0]
    assert d2["current_task"] == "warmup send" and d2["task_source"] == "transcript"
    assert lookups == ["/opt/b"]


def test_transcript_current_task_last_real_user_instruction(tmp_path, monkeypatch):
    import json
    from core import agent_orchestrator as ao
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-opt-foo"
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"content": "first task"}},
        {"type": "assistant", "message": {"content": "ok"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},  # no text
        {"type": "user", "isMeta": True, "message": {"content": "meta continuation"}},          # meta
        {"type": "user", "message": {"content": "<command-name>/foo</command-name>"}},          # wrapper
        {"type": "user", "message": {"content": "REAL task: warmup send\nsecond line"}},
    ]
    (proj / "c.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    assert ao.transcript_current_task("/opt/foo") == "REAL task: warmup send"   # last real, 1st line


def test_transcript_current_task_missing_is_none(tmp_path, monkeypatch):
    from core import agent_orchestrator as ao
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ao.transcript_current_task("/opt/none") is None                       # no dir → None
    assert ao.transcript_current_task("") is None


def test_transcript_current_task_redacts(tmp_path, monkeypatch):
    import json
    from core import agent_orchestrator as ao
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-opt-sec"
    proj.mkdir(parents=True)
    (proj / "c.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "use sk-SECRET now"}}))
    out = ao.transcript_current_task("/opt/sec", redact=lambda s: (s or "").replace("sk-SECRET", "***"))
    assert "sk-SECRET" not in out and "***" in out


# ── bounded cache + fail-open (item 2) ───────────────────────────────────────
def test_direct_agents_snapshot_cache_and_fail_open(monkeypatch):
    from core import agent_orchestrator as ao, agent_control as ac
    calls = {"n": 0}
    def fake_list():
        calls["n"] += 1
        return {"agents": [_agent("a:0.0", "working", "/opt/a")], "duplicates": []}
    monkeypatch.setattr(ac, "agent_list", fake_list)
    monkeypatch.setattr(ao, "all_records", lambda: [])
    ao._DIRECT_CACHE.update({"data": None, "duplicates": [], "ts": 0.0})
    d1, _, m1 = ao.direct_agents_snapshot()
    assert m1["cached"] is False and calls["n"] == 1
    d2, _, m2 = ao.direct_agents_snapshot()                    # within TTL → cached, no new capture
    assert m2["cached"] is True and calls["n"] == 1 and d2 == d1
    ao.direct_agents_snapshot(force=True)
    assert calls["n"] == 2                                     # force → live
    # fail-open: TTL=0 forces recompute, agent_list raises → return last cached, marked stale
    monkeypatch.setattr(ao, "_DIRECT_TTL", 0.0)
    def boom():
        raise RuntimeError("tmux down")
    monkeypatch.setattr(ac, "agent_list", boom)
    d4, _, m4 = ao.direct_agents_snapshot()
    assert m4.get("fail_open") is True and m4["stale"] is True and d4 == d1
