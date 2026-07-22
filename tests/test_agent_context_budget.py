"""Context-budget controller — detection, tiering, safe-boundary, handoff, rotation."""
from __future__ import annotations

import os

import pytest

from core import agent_context_budget as cb


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "cb.db"))
    # deterministic git facts (no real repo / subprocess).
    monkeypatch.setattr(cb, "git_facts",
                        lambda root: {"branch": "feat/x", "commit": "abc1234",
                                      "dirty": ["core/a.py", "core/b.py"]})


def _agent(tail="", target="seo-audit:0.0"):
    return {"target": target, "recent_activity": tail, "_tail": tail,
            "claude_cwd": "/opt/seo", "cwd": "/opt/seo"}


def _rec(state="idle", **kw):
    base = {"agent_key": "seo-audit:0.0", "session": "seo-audit", "project": "seo",
            "phase": "stage-4", "state": state, "approved_goal": "SEO Stage 4",
            "report_path": "reports/SEO.md"}
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
           "phases": [{"id": "stage-4"}, {"id": "stage-5"}], "approved_goal": "SEO Stage 4"}
    a = _agent(tail="/clear to save 600k tokens")        # 60% used
    rec = _rec(state="idle", approved_next_task="stage-5 (ready)")
    out = cb.evaluate(a, cfg, rec, {}, dispatch=True)
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
    out = cb.evaluate(a, cfg, _rec(state="idle", approved_next_task="b"), {}, dispatch=False)
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
