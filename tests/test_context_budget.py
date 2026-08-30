"""Context budget / checkpoint / rotation — non-vacuous tests.

Every rotation-path test asserts the SIDE EFFECTS (what was and was NOT sent to the
pane, what was and was NOT written durably), not just return shapes, so a broken
guard cannot pass silently.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from core import context_budget as cb
from core import agent_continuation_watchdog as cw
from core.control_plane import actuator as act


TARGET = "cp-canary:0.0"
ENTRY = {"project": "cp-canary-v2", "root": "/root/cp-canary-v2",
         "end_state": "canary green", "next_step": "continue with the next safe canary note."}
SUBAGENT_TAIL = "✻ Waiting for 1 background agent to finish\n\n❯ \n"
IDLE_TAIL = "done.\n\n  4 tasks (3 done, 0 in progress, 1 open)\n\n❯ \n"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("CONTEXT_BUDGET_CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    # project-root checkpoint copies must land in the test sandbox, never the
    # real project directory
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setitem(ENTRY, "root", str(proj))
    monkeypatch.setattr(act, "ENABLED", True)
    monkeypatch.setattr(act, "CANARY_AGENTS", frozenset({TARGET}))
    monkeypatch.setattr(cw, "VERIFY_TIMEOUT", 1)
    yield


def _no_sleep(_):
    pass


class RotCtrl:
    """Fake pane controller that records every delivery. Simulates the conversation
    switching to a new id after /clear is delivered."""

    def __init__(self, conv_holder):
        self.conv = conv_holder                       # {"id": ..., "size": ...}
        self.delivered = []
        self.s = {"pending": "", "state": "idle", "tail": "", "mt": "m0"}

    def snapshot(self, target, cwd):
        return {"tail": self.s["tail"], "pending": self.s["pending"],
                "conv_mtime": self.s["mt"], "state": self.s["state"],
                "activity": self.s["tail"]}

    def send(self, target, text, idem):
        self.delivered.append(text)
        self.s["mt"] += "+"
        self.s["tail"] += f" <{text[:20]}>"
        if text.strip() == "/clear":
            # a real /clear completes quickly: new conversation file, pane back at rest
            self.conv["id"] = "conv-NEW"
            self.conv["size"] = 100
            self.s["state"] = "idle"
        else:
            self.s["state"] = "working"
        return {"submitted": True}

    def enter(self, target):
        return 0

    def robust_submit(self, target, text):
        self.delivered.append(text)
        return True


def _mk(conv_holder):
    def conv_meta():
        size = conv_holder["size"]
        return {"conversation_id": conv_holder["id"], "size_bytes": size,
                "est_tokens": size // 4,
                "over_soft": size >= cb.soft_limit_bytes(),
                "over_hard": size >= cb.hard_limit_bytes()}
    return conv_meta


def _measurement(size, conv="conv-OLD"):
    return {"conversation_id": conv, "size_bytes": size, "est_tokens": size // 4,
            "over_soft": size >= cb.soft_limit_bytes(),
            "over_hard": size >= cb.hard_limit_bytes()}


# ── thresholds ───────────────────────────────────────────────────────────────
def test_measure_thresholds(monkeypatch):
    monkeypatch.setenv("CONTEXT_BUDGET_SOFT_BYTES", "1000")
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "2000")
    ev = {"latest": {"conversation_id": "c1", "size_bytes": 1500,
                     "modified_at": "2026-08-03T00:00:00+00:00"}}
    m = cb.measure("/x", conv_evidence_fn=lambda cwd: ev)
    assert m["over_soft"] is True and m["over_hard"] is False and m["est_tokens"] == 375
    ev["latest"]["size_bytes"] = 2500
    m2 = cb.measure("/x", conv_evidence_fn=lambda cwd: ev)
    assert m2["over_hard"] is True


def test_under_threshold_never_rotates(monkeypatch):
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "2000")
    holder = {"id": "conv-OLD", "size": 500}
    ctrl = RotCtrl(holder)
    out = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                    measurement=_measurement(500), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder))
    assert out["rotated"] is False and out["reason"] == "under_hard_threshold"
    assert ctrl.delivered == [], "nothing may be sent to the pane under threshold"


# ── phase-boundary protection ────────────────────────────────────────────────
@pytest.mark.parametrize("state,tail,pending,expect", [
    ("working", IDLE_TAIL, "", "state_working"),
    ("shell_running", IDLE_TAIL, "", "state_shell_running"),
    ("waiting_owner", IDLE_TAIL, "", "permission_dialog_open"),
    ("idle", IDLE_TAIL + "\nesc to interrupt", "", "active_execution_marker"),
    ("idle", IDLE_TAIL, "run this queued command", "pending_input_line"),
])
def test_rotation_refused_at_unsafe_phase(monkeypatch, state, tail, pending, expect):
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-OLD", "size": 5000}
    ctrl = RotCtrl(holder)
    out = cb.rotate(TARGET, ENTRY, state=state, tail=tail, pending=pending,
                    measurement=_measurement(5000), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder))
    assert out["rotated"] is False and out["reason"] == "unsafe_phase"
    assert expect in out["blocking"]
    assert ctrl.delivered == [], "/clear must NEVER reach the pane at an unsafe boundary"
    # the refusal is durably recorded
    conn = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    n = conn.execute("SELECT count(*) FROM context_rotation WHERE target=? AND "
                     "status='refused_unsafe_phase'", (TARGET,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_background_subagent_blocks_rotation(monkeypatch):
    """The mess-qa-automation live case: a running Fable subagent must block /clear."""
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-OLD", "size": 5000}
    ctrl = RotCtrl(holder)
    out = cb.rotate(TARGET, ENTRY, state="idle", tail=SUBAGENT_TAIL, pending="",
                    measurement=_measurement(5000), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder))
    assert out["rotated"] is False
    assert "background_subagent_running" in out["blocking"] \
        or "active_execution_marker" in out["blocking"]
    assert ctrl.delivered == []


# ── checkpoint atomicity + verification gate ─────────────────────────────────
def test_checkpoint_written_verified_and_complete(tmp_path):
    m = _measurement(5000)
    content = cb.build_checkpoint(TARGET, ENTRY, state="idle", tail=IDLE_TAIL,
                                  measurement=m, head="a" * 40)
    path = cb.write_checkpoint(TARGET, content)
    v = cb.verify_checkpoint(path)
    assert v["ok"] is True
    text = open(path).read()
    for s in cb.REQUIRED_SECTIONS:
        assert f"## {s}" in text
    assert "a" * 40 in text                       # exact HEAD present
    assert ENTRY["next_step"] in text             # exact next command present
    assert not os.path.exists(path + ".tmp")      # atomic: no tmp residue


def test_verify_rejects_missing_and_empty_sections(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("# CONTEXT CHECKPOINT\n## PROJECT\nx\n")
    assert cb.verify_checkpoint(str(p))["ok"] is False
    # all sections present but HEAD empty → refused
    full = "\n".join(f"## {s}\nx" for s in cb.REQUIRED_SECTIONS)
    full = full.replace("## HEAD\nx", "## HEAD\n")
    p2 = tmp_path / "bad2.md"
    p2.write_text(full)
    v = cb.verify_checkpoint(str(p2))
    assert v["ok"] is False and "HEAD" in v["reason"]
    assert cb.verify_checkpoint(str(tmp_path / "missing.md"))["ok"] is False


def test_failed_checkpoint_refuses_rotation(monkeypatch):
    """A checkpoint that does not verify must REFUSE rotation — nothing sent."""
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-OLD", "size": 5000}
    ctrl = RotCtrl(holder)
    monkeypatch.setattr(cb, "verify_checkpoint",
                        lambda p: {"ok": False, "reason": "simulated_corruption"})
    out = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                    measurement=_measurement(5000), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder))
    assert out["rotated"] is False and out["reason"] == "checkpoint_unverified"
    assert ctrl.delivered == [], "no /clear without a verified checkpoint"


def test_checkpoint_write_failure_refuses_rotation(monkeypatch):
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-OLD", "size": 5000}
    ctrl = RotCtrl(holder)

    def boom(t, c):
        raise OSError("disk full")
    monkeypatch.setattr(cb, "write_checkpoint", boom)
    out = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                    measurement=_measurement(5000), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder))
    assert out["rotated"] is False and out["reason"] == "checkpoint_write_failed"
    assert ctrl.delivered == []


# ── full rotation + post-clear continuation + duplicate prevention ───────────
def test_full_rotation_clear_resume_mapping_and_no_duplicate(monkeypatch):
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-OLD", "size": 5000}
    ctrl = RotCtrl(holder)
    out = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                    measurement=_measurement(5000), ctrl=ctrl, sleep=_no_sleep,
                    conv_meta_fn=_mk(holder), wait_new_conv_secs=0.01)
    assert out["rotated"] is True
    # exactly TWO deliveries: /clear then the resume step referencing the checkpoint
    assert len(ctrl.delivered) == 2
    assert ctrl.delivered[0].strip() == "/clear"
    assert ctrl.delivered[1].lower().startswith("resume")
    # the resume references the PROJECT-ROOT copy (the agent's sandbox may forbid
    # the central path — live cp-canary 2026-08-03) and carries the exact next
    # command inline so an unreadable file cannot lose the handoff
    proj_copy = os.path.join(ENTRY["root"], "CONTEXT_CHECKPOINT.md")
    assert proj_copy in ctrl.delivered[1]
    assert cb.verify_checkpoint(proj_copy)["ok"] is True
    assert ENTRY["next_step"] in ctrl.delivered[1]
    # the resume text must still classify autonomous_safe end-to-end
    assert act.classify_action(ctrl.delivered[1]) == act.AUTONOMOUS_SAFE
    # conversation mapping recorded old → new
    assert out["old_conversation"] == "conv-OLD"
    assert out["new_conversation"] == "conv-NEW"
    conn = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    row = conn.execute("SELECT old_conversation_id,new_conversation_id,status,checkpoint_path "
                       "FROM context_rotation WHERE target=? AND status='rotated'",
                       (TARGET,)).fetchone()
    assert row == ("conv-OLD", "conv-NEW", "rotated", out["checkpoint"])
    conn.close()
    # checkpoint on disk is readable and complete after the fact
    assert cb.verify_checkpoint(out["checkpoint"])["ok"] is True

    # DUPLICATE PREVENTION: the same (target, old conversation) never rotates twice
    ctrl2 = RotCtrl({"id": "conv-OLD", "size": 5000})
    out2 = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                     measurement=_measurement(5000), ctrl=ctrl2, sleep=_no_sleep,
                     conv_meta_fn=_mk({"id": "conv-OLD", "size": 5000}))
    assert out2["rotated"] is False and out2["reason"] == "already_rotated_this_conversation"
    assert ctrl2.delivered == []


class KeyedRotCtrl(RotCtrl):
    """Mirrors the REAL transport (`agent_control._deliver`): a repeated idempotency
    key is silently dropped. The plain RotCtrl ignored keys entirely, which made the
    2026-08-03 live failure invisible to the suite (vacuous coverage): the constant
    `cw:<hash>` key meant the second /clear EVER was never delivered."""
    _seen_keys = None

    def __init__(self, conv_holder, seen):
        super().__init__(conv_holder)
        self._seen_keys = seen

    def send(self, target, text, idem):
        if idem in self._seen_keys:
            return {"submitted": False, "duplicate": True}
        self._seen_keys.add(idem)
        return super().send(target, text, idem)


def test_second_rotation_clear_is_not_dropped_by_transport_dedupe(monkeypatch):
    """LIVE 2026-08-03 cp-canary: rotation #1 verified; rotation #2 (new
    conversation) failed verify because the transport dropped the identical
    '/clear' under the constant idempotency key — spurious owner gate."""
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    seen = set()
    h1 = {"id": "conv-A", "size": 5000}
    out1 = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                     measurement=_measurement(5000, "conv-A"),
                     ctrl=KeyedRotCtrl(h1, seen), sleep=_no_sleep,
                     conv_meta_fn=_mk(h1), wait_new_conv_secs=0.01)
    assert out1["rotated"] is True
    # the same agent later fills the NEW conversation → must rotate again
    h2 = {"id": "conv-NEW", "size": 5000}
    ctrl2 = KeyedRotCtrl(h2, seen)          # same durable transport seen-set
    out2 = cb.rotate(TARGET, ENTRY, state="idle", tail=IDLE_TAIL, pending="",
                     measurement=_measurement(5000, "conv-NEW"),
                     ctrl=ctrl2, sleep=_no_sleep,
                     conv_meta_fn=_mk(h2), wait_new_conv_secs=0.01)
    assert out2["rotated"] is True, out2
    assert ctrl2.delivered and ctrl2.delivered[0].strip() == "/clear", \
        "the second /clear must reach the pane — transport dedupe must not eat it"


def test_non_canary_rotation_is_owner_gated(monkeypatch):
    """A non-canary agent over the hard budget must produce the owner-gated event and
    ZERO actuation — the Actuator's confinement is the hard wall."""
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "100")
    holder = {"id": "conv-P", "size": 900000}
    ctrl = RotCtrl(holder)
    reg = {"payment:0.0": {"project": "payment-orchestrator",
                           "root": "/opt/payment-orchestrator",
                           "next_step": "continue the read-only recovery."}}
    inv = {"agents": [{"target": "payment:0.0", "alive": True, "is_agent": True,
                       "claude_cwd": "/opt/payment-orchestrator",
                       "cwd": "/opt/payment-orchestrator", "state": "idle"}]}
    res = cb.tick(registry=reg, inventory=inv, tail_fn=lambda t: IDLE_TAIL,
                  pending_fn=lambda t: "", measure_fn=lambda cwd: _measurement(900000, "conv-P"),
                  ctrl=ctrl, sleep=_no_sleep)
    r = res["results"][0]
    assert r["action"] == "rotation_needed_owner_gated"
    assert ctrl.delivered == [], "a non-canary pane must never be touched"
    from core import agent_control as ac
    evs = ac.list_commander_events(limit=10)
    assert any(e["event_type"] == "context_rotation_needed" and e["agent"] == "payment:0.0"
               for e in evs)


def test_tick_soft_threshold_checkpoints_once_per_conversation(monkeypatch):
    monkeypatch.setenv("CONTEXT_BUDGET_SOFT_BYTES", "100")
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "999999999")
    inv = {"agents": [{"target": TARGET, "alive": True, "is_agent": True,
                       "claude_cwd": "/root/cp-canary-v2", "cwd": "/root/cp-canary-v2",
                       "state": "idle"}]}
    kw = dict(registry={TARGET: ENTRY}, inventory=inv, tail_fn=lambda t: IDLE_TAIL,
              pending_fn=lambda t: "", measure_fn=lambda cwd: _measurement(5000, "conv-S"),
              sleep=_no_sleep)
    r1 = cb.tick(**kw)
    assert r1["results"][0]["action"] == "checkpointed"
    assert cb.verify_checkpoint(r1["results"][0]["checkpoint"])["ok"] is True
    # second tick, same conversation → no second checkpoint (no churn)
    r2 = cb.tick(**kw)
    assert r2["results"][0]["action"] == "tracked"


def test_tick_soft_threshold_unsafe_phase_does_not_checkpoint(monkeypatch):
    """Even a checkpoint (no actuation) waits for a safe boundary — its content
    would otherwise describe a mid-mutation state."""
    monkeypatch.setenv("CONTEXT_BUDGET_SOFT_BYTES", "100")
    monkeypatch.setenv("CONTEXT_BUDGET_HARD_BYTES", "999999999")
    inv = {"agents": [{"target": TARGET, "alive": True, "is_agent": True,
                       "claude_cwd": "/root/cp-canary-v2", "cwd": "/root/cp-canary-v2",
                       "state": "working"}]}
    r = cb.tick(registry={TARGET: ENTRY}, inventory=inv, tail_fn=lambda t: IDLE_TAIL,
                pending_fn=lambda t: "", measure_fn=lambda cwd: _measurement(5000, "conv-W"),
                sleep=_no_sleep)
    assert r["results"][0]["action"] == "tracked"


def test_state_tracking_is_durable_for_every_agent():
    inv = {"agents": [{"target": TARGET, "alive": True, "is_agent": True,
                       "claude_cwd": "/root/cp-canary-v2", "cwd": "/root/cp-canary-v2",
                       "state": "working"}]}
    cb.tick(registry={TARGET: ENTRY}, inventory=inv, tail_fn=lambda t: "",
            pending_fn=lambda t: "", measure_fn=lambda cwd: _measurement(1234, "conv-T"),
            sleep=_no_sleep)
    conn = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    row = conn.execute("SELECT conversation_id,size_bytes,est_tokens,state,safe_boundary "
                       "FROM context_budget_state WHERE target=?", (TARGET,)).fetchone()
    conn.close()
    assert row == ("conv-T", 1234, 308, "working", 0)


def test_bare_clear_is_autonomous_safe_but_prefixed_is_not():
    assert act.classify_action("/clear") == act.AUTONOMOUS_SAFE
    assert act.classify_action("  /compact  ") == act.AUTONOMOUS_SAFE
    assert act.classify_action("/clear && rm -rf /") == act.PROHIBITED
    assert act.classify_action("please /clear now") == act.OWNER_APPROVAL
