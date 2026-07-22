"""Context-budget controller — detection, tiering, safe-boundary, handoff, rotation."""
from __future__ import annotations

import os
import time

import pytest

from core import agent_context_budget as cb


def _settled_prev():
    """A prior sweep that was at rest long enough to satisfy the stable-idle gate."""
    return {"state": "idle", "last_fresh_activity_ts": time.time() - 999}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "cb.db"))
    # deterministic git facts (no real repo / subprocess).
    monkeypatch.setattr(cb, "git_facts",
                        lambda root: {"branch": "feat/x", "commit": "abc1234",
                                      "dirty": ["core/a.py", "core/b.py"]})
    # isolate from any real on-disk handoff (tests that need one override this).
    monkeypatch.setattr(cb, "existing_fresh_handoff", lambda root, max_age: None)
    monkeypatch.setattr(cb.ac, "pending_input_text", lambda key, tail=None: "")


def _agent(tail="", target="seo-audit:0.0"):
    return {"target": target, "recent_activity": tail, "_tail": tail,
            "claude_cwd": "/opt/seo", "cwd": "/opt/seo"}


def _rec(state="idle", **kw):
    base = {"agent_key": "seo-audit:0.0", "session": "seo-audit", "project": "seo",
            "phase": "stage-4", "state": state, "approved_goal": "SEO Stage 4",
            "report_path": "reports/SEO.md", "last_fresh_activity_ts": time.time() - 999}
    base.update(kw)
    return base


AUTO = {"mode": "auto", "project": "seo", "root": "/opt/seo", "approved_goal": "SEO Stage 4",
        "phases": [{"id": "stage-4"}, {"id": "stage-5"}]}
T = cb._DEFAULTS


# ── detection ───────────────────────────────────────────────────────────────
def test_detect_tokens_footer_over_window():
    # 763.4k tokens / 1,000,000 window = 76.3%
    assert cb.detect_context_pct("new task? /clear to save 763.4k tokens") == pytest.approx(76.3, abs=0.1)


def test_detect_percent_left_form():
    assert cb.detect_context_pct("Context left until auto-compact: 20% left") == 80.0


def test_detect_percent_used_form():
    assert cb.detect_context_pct("context window used: 58%") == 58.0


def test_detect_none_when_no_signal():
    assert cb.detect_context_pct("just some agent output") is None


# ── tiering (defaults 45 / 55 / 65) ─────────────────────────────────────────
@pytest.mark.parametrize("pct,tier", [
    (30, "ok"), (44.9, "ok"), (45, "checkpoint"), (54.9, "checkpoint"),
    (55, "rotate_substantial"), (64.9, "rotate_substantial"), (65, "rotate"), (90, "rotate"),
    (None, "unknown"),
])
def test_classify_tiers(pct, tier):
    assert cb.classify(pct, T) == tier


def test_per_project_threshold_override():
    cfg = {"context_budget": {"rotate_pct": 80, "checkpoint_pct": 50}}
    t = cb.thresholds(cfg)
    assert t["rotate_pct"] == 80 and t["checkpoint_pct"] == 50
    assert cb.classify(70, t) == "rotate_substantial"    # 70 < 80 now


def test_per_model_window_override():
    cfg = {"model": "small", "context_windows": {"small": 200000}}
    t = cb.thresholds(cfg)
    assert t["window_tokens"] == 200000


# ── safe-boundary gate ──────────────────────────────────────────────────────
def test_active_exec_is_not_safe_boundary():
    assert cb.at_safe_boundary(_agent(tail="… esc to interrupt"), "idle") is False


def test_working_state_is_not_safe_boundary():
    assert cb.at_safe_boundary(_agent(tail="quiet"), "working") is False


def test_waiting_and_completed_not_safe_boundary():
    assert cb.at_safe_boundary(_agent(), "waiting_owner") is False
    assert cb.at_safe_boundary(_agent(), "completed") is False


def test_idle_quiet_is_safe_boundary():
    assert cb.at_safe_boundary(_agent(tail="done. ❯"), "idle") is True


@pytest.mark.parametrize("tail", [
    "Exploring the codebase…",
    "dispatching subagent to map the module",
    "running the migration now",
    "deploying backend",
    "building frontend",
    "installing dependencies",
])
def test_active_subagent_migration_deploy_block_clear(tail):
    # The owner's exclusion list: never a safe boundary while these run.
    assert cb.at_safe_boundary(_agent(tail=tail), "idle") is False


def test_no_clear_when_input_line_has_queued_instruction(monkeypatch, tmp_path):
    # An owner-queued instruction in the input line must block /clear (else
    # agent_send would concatenate and SUBMIT it — e.g. a financial action).
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cb.ac, "pending_input_text", lambda key, tail=None: "enable premium and test one charge")
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    tail = ("idle · /clear to save 700k tokens\n──────\n"
            "❯ enable premium and test one charge\n──────")
    a = _agent(tail=tail)
    rec = _rec(state="idle", agent_key="job:0.0", approved_next_task="phase 2")
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "1"}, {"id": "2"}]},
                      rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] == "context_rotate_deferred_pending_input"
    assert sent == []                                   # NEVER cleared


def test_stably_idle_gate():
    import time as _t
    now = _t.time()
    T = cb._DEFAULTS
    # prev was working → never settled (a momentary idle read cannot rotate).
    assert cb.stably_idle({"last_fresh_activity_ts": now - 999}, {"state": "working"}, T["min_idle_dwell_secs"]) is False
    # prev at rest but activity too recent → not settled.
    assert cb.stably_idle({"last_fresh_activity_ts": now - 10}, {"state": "idle"}, T["min_idle_dwell_secs"]) is False
    # prev at rest AND activity older than the dwell → settled.
    assert cb.stably_idle({"last_fresh_activity_ts": now - 999}, {"state": "idle"}, T["min_idle_dwell_secs"]) is True
    # no prior sweep → never settled (first post-restart sweep must not rotate).
    assert cb.stably_idle({"last_fresh_activity_ts": now - 999}, {}, T["min_idle_dwell_secs"]) is False


def test_no_clear_when_unsettled_even_at_safe_boundary(monkeypatch, tmp_path):
    # The 2026-07-22 live regression: a working agent momentarily read idle on the
    # first post-restart sweep and got cleared. Now blocked by the stable-idle gate.
    import time as _t
    sent = []
    monkeypatch.setattr(cb, "existing_fresh_handoff",
                        lambda root, max_age: str(tmp_path / "reports" / "CONTEXT_HANDOFF.md"))
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(cb.ac, "submit_clear", lambda *a, **k: sent.append(("submit",) + a))
    a = _agent(tail="done · /clear to save 900k tokens\n❯ ")
    rec = _rec(state="idle", approved_next_task="Phase 8d", last_fresh_activity_ts=_t.time() - 5)
    prev = {"state": "working"}                         # was mid-turn last sweep
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery (read-only)"}]},
                      rec, prev, act=True, dispatch=True)
    assert out["notification_state"] == "context_rotate_deferred_unsettled"
    assert sent == []                                   # NEVER cleared a just-working agent


def test_resume_prefers_agent_authored_handoff(monkeypatch, tmp_path):
    import time as _t
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "CONTEXT_HANDOFF.md").write_text("agent detailed handoff 8a-8c → 8d")
    sent = []
    monkeypatch.setattr(cb, "existing_fresh_handoff",
                        lambda root, max_age: str(reports / "CONTEXT_HANDOFF.md"))
    monkeypatch.setattr(cb.ac, "pending_input_text", lambda key, tail=None: "")
    monkeypatch.setattr(cb.ac, "agent_send", lambda key, text, **k: sent.append(text))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda *a, **k: None)
    a = _agent(tail="done · /clear to save 900k tokens\n❯ ")   # high context
    rec = _rec(state="idle", approved_next_task="Phase 8d",
               last_fresh_activity_ts=_t.time() - 999)
    prev = {"state": "idle", "last_fresh_activity_ts": _t.time() - 999}
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery (read-only)"}]},
                      rec, prev, act=True, dispatch=True)
    assert out["notification_state"] == "context_rotated_checkpoint"
    # resume message points at the AGENT's reports/ handoff, not the module's root one.
    resume = next(t for t in sent if "Read" in t)
    assert "reports/CONTEXT_HANDOFF.md" in resume


def test_detect_surfaceable_event_independent_of_dispatch(monkeypatch, tmp_path):
    # A checkpoint/completion event surfaces on DETECTION (idle+safe+handoff),
    # regardless of context %, dwell, or dispatch — the 2026-07-22 delivery fix.
    reports = tmp_path / "reports"; reports.mkdir()
    (reports / "CONTEXT_HANDOFF.md").write_text("h")
    a = _agent(tail="done, clean boundary ❯ ")            # NO context% figure
    cfg = {"root": str(tmp_path), "project": "seo",
           "active_task_id": "part-e", "active_task_text": "Continue Part E readiness center"}
    ev = cb.detect_surfaceable_event(a, _rec(state="idle"), cfg, a["_tail"])
    assert ev["event_type"] == "checkpoint_completed_work_remaining"
    assert ev["remaining_id"] == "part-e" and "Part E" in ev["remaining"]
    assert ev["handoff_path"] == str(reports / "CONTEXT_HANDOFF.md")
    # a working agent surfaces nothing.
    assert cb.detect_surfaceable_event(_agent(tail="… esc to interrupt"), _rec(state="working"), cfg, "") is None
    # completed with no remaining work → completion event, not a resume.
    done_cfg = {"root": str(tmp_path), "project": "seo"}
    ev2 = cb.detect_surfaceable_event(a, _rec(state="idle"), done_cfg, "all done")
    assert ev2["event_type"] == "task_completed_no_remaining_work"


def test_context_detection_extra_forms():
    assert cb.detect_context_pct("72% context used") == 72.0
    assert cb.detect_context_pct("context: 1.29 MB") == 90.0
    assert cb.detect_context_pct("context: 700k tokens") == pytest.approx(70.0, abs=0.1)


def test_active_task_text_is_work_remaining_to_continue():
    cfg = {"active_task_id": "part-d", "active_task_text": "Continue Part D: source intake pipeline"}
    cls, rem = cb.completion_class(_rec(), cfg, "idle")
    assert cls == "work_remaining"
    assert rem["id"] == "part-d" and "source intake" in rem["text"]
    # cleared active task + no next-phase text → no remaining work (surface completion).
    assert cb.completion_class(_rec(), {"active_task_text": "  "}, "done")[0] == "task_completed_no_remaining_work"


def test_completion_class_three_way(tmp_path):
    wr = {"phases": [{"id": "a"}, {"id": "b", "approved_task_text": "do exact task X"}]}
    assert cb.completion_class(_rec(), wr, "done")[0] == "work_remaining"
    assert cb.completion_class(_rec(), wr, "done")[1]["text"] == "do exact task X"
    # no owner text + waiting on owner/external → waiting_external (never invent work)
    none_cfg = {"phases": [{"id": "a"}, {"id": "b"}]}     # bare placeholder, no text
    assert cb.completion_class(_rec(), none_cfg, "task complete, waiting for owner credentials")[0] \
        == "task_completed_waiting_external"
    # no text + nothing pending → no remaining work
    assert cb.completion_class(_rec(), none_cfg, "all done, nothing left")[0] \
        == "task_completed_no_remaining_work"


def test_completed_task_is_not_falsely_resumed(monkeypatch, tmp_path):
    # THE 2026-07-22 failure: the approved task was fully complete (no owner-recorded
    # next-phase text — only a config placeholder). Rotation must NOT tell the agent
    # to "continue the task", and must NOT trigger a full test run.
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda key, text, **k: sent.append(text))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda *a, **k: None)
    a = _agent(tail="all committed, deployed, verified. ❯ /clear to save 900k tokens")  # 90% used
    rec = _rec(state="idle")
    cfg = {"root": str(tmp_path), "project": "seo", "phases": [{"id": "done"}, {"id": "stage-5"}]}  # NO approved_task_text
    out = cb.evaluate(a, cfg, rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] == "task_completed_no_remaining_work"
    assert out["rotation"]["action"] == "rotated_idle"
    # a /clear may fire to save tokens, but NO "continue the task" resume is sent.
    assert not any("resume" in t.lower() or "continue" in t.lower() or "subphase" in t.lower() for t in sent)


def test_resume_uses_resolved_path_never_hardcoded_root(monkeypatch, tmp_path):
    # missing root/CONTEXT_HANDOFF.md but a real reports/ one → resume must point at
    # the resolved reports/ path, never the hardcoded root path.
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "CONTEXT_HANDOFF.md").write_text("agent handoff: 8a-8c done → 8d")
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda key, text, **k: sent.append(text))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda *a, **k: None)
    a = _agent(tail="idle · /clear to save 900k tokens\n❯ ")
    rec = _rec(state="idle")
    cfg = {"root": str(tmp_path), "project": "seo",
           "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery"}]}
    out = cb.evaluate(a, cfg, rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] in ("context_rotated", "context_rotated_checkpoint")
    resume = next(t for t in sent if "run remote discovery" in t)
    assert str(reports / "CONTEXT_HANDOFF.md") in resume        # resolved reports/ path
    assert not resume.endswith(str(tmp_path / "CONTEXT_HANDOFF.md"))   # not the hardcoded root path


def test_resume_message_carries_exact_remaining_text(monkeypatch, tmp_path):
    reports = tmp_path / "reports"; reports.mkdir()
    (reports / "CONTEXT_HANDOFF.md").write_text("h")
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda key, text, **k: sent.append(text))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda *a, **k: None)
    a = _agent(tail="idle · /clear to save 900k tokens\n❯ ")
    cfg = {"root": str(tmp_path), "phases": [{"id": "8c"},
           {"id": "8d", "approved_task_text": "read-only remote discovery of cloudrec/seo"}]}
    out = cb.evaluate(a, cfg, _rec(state="idle"), _settled_prev(), act=True, dispatch=True)
    resume = next(t for t in sent if "Read" in t)
    assert "read-only remote discovery of cloudrec/seo" in resume    # exact text, not generic
    assert "[8d]" in resume                                          # exact id
    assert "next subphase" not in resume.lower()                     # no generic language
    assert "full test" in resume.lower()                            # explicit no-auto-test guard


def test_surfacing_without_context_but_no_clear(monkeypatch, tmp_path):
    # Owner policy: a completed checkpoint SURFACES even with NO context% figure,
    # but the /clear itself must NOT fire until context reaches threshold.
    monkeypatch.setattr(cb, "existing_fresh_handoff",
                        lambda root, max_age: str(tmp_path / "reports" / "CONTEXT_HANDOFF.md"))
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    a = _agent(tail="done 8a-8c. ❯ ")                    # NO context% signal
    rec = _rec(state="idle", approved_next_task="Phase 8d")
    wr_cfg = {"root": str(tmp_path), "project": "seo",
              "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery (read-only)"}]}
    # SURFACING fires (independent of context):
    ev = cb.detect_surfaceable_event(a, rec, wr_cfg, a["_tail"])
    assert ev["event_type"] == "checkpoint_completed_work_remaining"
    assert ev["remaining"] == "run remote discovery (read-only)"
    # but evaluate does NOT /clear without a context signal:
    out = cb.evaluate(a, wr_cfg, rec, _settled_prev(), act=True, dispatch=True)
    assert out.get("notification_state") is None and sent == []


def test_checkpoint_rotation_submits_agents_own_clear(monkeypatch, tmp_path):
    # When the agent already typed a bare /clear, submit it (Enter) rather than
    # pasting again; then restore auto mode and resume from the handoff.
    submitted, sent, ensured = [], [], []
    monkeypatch.setattr(cb, "existing_fresh_handoff",
                        lambda root, max_age: str(tmp_path / "reports" / "CONTEXT_HANDOFF.md"))
    monkeypatch.setattr(cb.ac, "pending_input_text", lambda key, tail=None: "/clear")
    monkeypatch.setattr(cb.ac, "submit_clear", lambda key, **k: submitted.append(key) or True)
    monkeypatch.setattr(cb.ac, "agent_send", lambda key, text, **k: sent.append(text))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda key, **k: ensured.append(key))
    a = _agent(tail="done · /clear to save 900k tokens\n❯ /clear")
    rec = _rec(state="idle", approved_next_task="Phase 8d")
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery (read-only)"}]},
                      rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] == "context_rotated_checkpoint"
    assert submitted == ["seo-audit:0.0"]                # submitted the agent's own /clear
    assert not any(t == "/clear" for t in sent)          # never pasted a second /clear
    assert any("Read" in t for t in sent)                # resume-from-handoff sent
    assert ensured == ["seo-audit:0.0"]                  # auto mode restored


def test_checkpoint_refuses_when_nonclear_instruction_queued(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb, "existing_fresh_handoff",
                        lambda root, max_age: str(tmp_path / "CONTEXT_HANDOFF.md"))
    monkeypatch.setattr(cb.ac, "pending_input_text", lambda key, tail=None: "enable premium and charge")
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    a = _agent(tail="done · /clear to save 900k tokens\n❯ enable premium and charge")
    rec = _rec(state="idle", approved_next_task="Phase 8d")
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "8c"}, {"id": "8d", "approved_task_text": "run remote discovery (read-only)"}]},
                      rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] == "context_rotate_deferred_pending_input"
    assert sent == []                                    # never cleared/submitted


def test_rotation_restores_auto_mode_after_clear(monkeypatch, tmp_path):
    sent, ensured = [], []
    monkeypatch.setattr(cb.ac, "agent_send", lambda tgt, text, **k: sent.append((tgt, text)))
    monkeypatch.setattr(cb.ac, "ensure_auto_mode", lambda key, **k: ensured.append(key) or {"action": "restored"})
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    a = _agent(tail="quiet /clear to save 700k tokens\n──────\n❯ \n──────")   # 70%, empty input
    rec = _rec(state="idle", agent_key="seo-audit:0.0", approved_next_task="phase 2")
    out = cb.evaluate(a, {"root": str(tmp_path), "phases": [{"id": "1"}, {"id": "2", "approved_task_text": "finish phase 2"}]},
                      rec, _settled_prev(), act=True, dispatch=True)
    assert out["notification_state"] == "context_rotated"
    assert any(t == "/clear" for _, t in sent)
    assert ensured == ["seo-audit:0.0"]                 # auto mode restored after clear


# ── finish-soon suppression ─────────────────────────────────────────────────
def test_finish_soon_from_pane_cue():
    assert cb.finish_soon(_agent(tail="almost done, one more commit"), _rec()) is True


def test_finish_soon_from_completion_evidence():
    assert cb.finish_soon(_agent(tail="quiet"), _rec(completion_evidence="{...}")) is True


def test_no_rotate_when_finishing_even_above_65(monkeypatch):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    a = _agent(tail="wrapping up · /clear to save 900k tokens")   # 90% used but finishing
    out = cb.evaluate(a, AUTO, _rec(state="idle"), {}, dispatch=True)
    assert out["notification_state"] == "context_finish_soon_suppressed"
    assert sent == []                                            # NO /clear


# ── compact handoff ─────────────────────────────────────────────────────────
def test_handoff_is_compact_and_factual():
    content, h = cb.build_handoff(_rec(), AUTO, "/opt/seo", 66.0, T["handoff_max_bytes"])
    assert len(content.encode("utf-8")) <= T["handoff_max_bytes"]
    assert len(content.encode("utf-8")) <= 2048
    # required, delta-based fields present; no conversation history copied.
    for token in ["Project:", "Approved task:", "Branch:", "Commit:", "Report",
                  "Blocker:", "NEXT", "Rollback:", "Do NOT touch"]:
        assert token in content
    assert isinstance(h, str) and len(h) == 16


def test_handoff_next_command_is_approved_task_not_invented():
    content, _ = cb.build_handoff(_rec(), AUTO, "/opt/seo", 66.0, T["handoff_max_bytes"])
    assert "SEO Stage 4" in content
    assert "do NOT invent" in content


def test_handoff_hash_stable_across_timestamp_but_changes_with_state():
    c1, h1 = cb.build_handoff(_rec(), AUTO, "/opt/seo", 66.0, T["handoff_max_bytes"])
    c2, h2 = cb.build_handoff(_rec(), AUTO, "/opt/seo", 66.0, T["handoff_max_bytes"])
    assert h1 == h2                                             # same state → same hash
    _, h3 = cb.build_handoff(_rec(blocker_text="new blocker"), AUTO, "/opt/seo", 66.0,
                             T["handoff_max_bytes"])
    assert h3 != h1                                             # changed state → changed hash


# ── cooldown + changed-hash anti-loop ───────────────────────────────────────
def test_first_rotation_allowed_then_cooldown_blocks_same_hash():
    assert cb.can_rotate_again("k", "hash1", 2700) is True
    cb._save_rotation("k", "p", 66.0, "cleared", "/x/CONTEXT_HANDOFF.md", "hash1", cb._now_ts())
    # same hash → blocked regardless of time (would loop).
    assert cb.can_rotate_again("k", "hash1", 2700) is False
    # changed hash but still inside cooldown → blocked.
    assert cb.can_rotate_again("k", "hash2", 2700) is False


def test_changed_hash_after_cooldown_allows_repeat(monkeypatch):
    cb._save_rotation("k", "p", 66.0, "cleared", "/x/CONTEXT_HANDOFF.md", "hash1",
                      cb._now_ts() - 3000)              # 50 min ago > 45 min cooldown
    assert cb.can_rotate_again("k", "hash2", 2700) is True     # changed + cooled down


# ── 55–65% rotation gated on substantial work ───────────────────────────────
def test_substantial_work_remains_detection():
    assert cb.substantial_work_remains(_rec(approved_next_task="stage-5 (ready)"), AUTO) is True
    assert cb.substantial_work_remains(_rec(), {"phases": [{"id": "only"}]}) is False


def test_mid_tier_skips_when_no_substantial_work(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    cfg = {"mode": "auto", "root": str(tmp_path), "project": "seo", "phases": [{"id": "only"}]}
    a = _agent(tail="/clear to save 600k tokens")        # 60% → rotate_substantial tier
    out = cb.evaluate(a, cfg, _rec(state="idle"), {}, dispatch=True)
    assert out["notification_state"] == "context_rotate_skipped_finishing"
    assert sent == []


def test_mid_tier_rotates_when_substantial_and_safe(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda tgt, text, **k: sent.append((tgt, text)))
    cfg = {"mode": "auto", "root": str(tmp_path), "project": "seo",
           "phases": [{"id": "stage-4"}, {"id": "stage-5", "approved_task_text": "finish stage-5"}], "approved_goal": "SEO Stage 4"}
    a = _agent(tail="/clear to save 600k tokens")        # 60% used
    rec = _rec(state="idle", approved_next_task="stage-5 (ready)")
    out = cb.evaluate(a, cfg, rec, _settled_prev(), dispatch=True)
    assert out["notification_state"] == "context_rotated"
    assert os.path.exists(os.path.join(str(tmp_path), cb.HANDOFF_FILENAME))
    assert [t for t, _ in sent] == [a["target"], a["target"]]     # /clear then resume
    assert sent[0][1] == "/clear"
    assert cb.HANDOFF_FILENAME in sent[1][1]


# ── no /clear during active work, even above threshold ──────────────────────
def test_no_clear_during_active_work(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    cfg = {"mode": "auto", "root": str(tmp_path), "project": "seo",
           "phases": [{"id": "a"}, {"id": "b"}]}
    a = _agent(tail="running the migration… esc to interrupt · /clear to save 900k tokens")
    out = cb.evaluate(a, cfg, _rec(state="working"), {}, dispatch=True)
    assert out["notification_state"] == "context_rotate_deferred"
    assert sent == []


# ── dry-run does not dispatch ────────────────────────────────────────────────
def test_dry_run_writes_handoff_but_does_not_clear(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    cfg = {"mode": "auto", "root": str(tmp_path), "project": "seo",
           "phases": [{"id": "a"}, {"id": "b"}], "approved_goal": "g"}
    a = _agent(tail="/clear to save 900k tokens")
    out = cb.evaluate(a, cfg, _rec(state="idle", approved_next_task="b"), _settled_prev(), dispatch=False)
    assert out["notification_state"] == "context_rotate_pending"
    assert sent == []
    assert os.path.exists(os.path.join(str(tmp_path), cb.HANDOFF_FILENAME))


# ── resume verification ─────────────────────────────────────────────────────
def test_resume_verified_when_working_after_clear(tmp_path):
    cb._save_rotation("seo-audit:0.0", "stage-4", 66.0, "cleared",
                      str(tmp_path / "CONTEXT_HANDOFF.md"), "h1", cb._now_ts())
    a = _agent(tail="working on it")
    out = cb.evaluate(a, AUTO, _rec(state="working"), {}, dispatch=True)
    assert out["notification_state"] == "rotated_resumed"
    assert cb._rotation_row("seo-audit:0.0")["stage"] == "resumed"


def test_awaiting_resume_when_still_idle_after_clear(tmp_path):
    cb._save_rotation("seo-audit:0.0", "stage-4", 66.0, "cleared",
                      str(tmp_path / "CONTEXT_HANDOFF.md"), "h1", cb._now_ts())
    a = _agent(tail="quiet ❯")
    out = cb.evaluate(a, AUTO, _rec(state="idle"), {}, dispatch=True)
    assert out["notification_state"] == "rotated_awaiting_resume"


# ── monitor/hold agents are detection-only (no writes, no clear) ────────────
def test_non_auto_agent_is_detection_only(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    cfg = {"mode": "hold", "root": str(tmp_path), "project": "safeguard",
           "phases": [{"id": "a"}, {"id": "b"}]}
    a = _agent(tail="/clear to save 900k tokens")        # 90% used, idle
    out = cb.evaluate(a, cfg, _rec(state="idle"), {}, act=False, dispatch=False)
    assert out["context_tier"] == "rotate"
    assert out["notification_state"] == "context_observed"
    assert sent == []
    assert not os.path.exists(os.path.join(str(tmp_path), cb.HANDOFF_FILENAME))   # nothing written


# ── checkpoint tier prepares handoff, never clears ──────────────────────────
def test_checkpoint_tier_prepares_compact_handoff(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(cb.ac, "agent_send", lambda *a, **k: sent.append(a))
    cfg = {"mode": "auto", "root": str(tmp_path), "project": "seo",
           "phases": [{"id": "a"}, {"id": "b"}], "approved_goal": "g"}
    a = _agent(tail="/clear to save 500k tokens")        # 50% → checkpoint
    out = cb.evaluate(a, cfg, _rec(state="idle"), {}, dispatch=True)
    assert out["notification_state"] == "context_checkpoint_prepared"
    assert sent == []
    assert os.path.exists(os.path.join(str(tmp_path), cb.HANDOFF_FILENAME))
