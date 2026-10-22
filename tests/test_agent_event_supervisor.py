"""Agent event supervisor: event -> wake -> verify -> DONE / CONTINUE / OWNER_NEEDED / FAILED /
PAUSED, deterministically. Every side effect goes through FakePorts; the ledger is a real
sqlite control-plane DB in tmp_path, so restart behaviour is exercised for real."""
from __future__ import annotations

import pytest

from core import agent_event_supervisor as aes

T = "proj:0.0"
CONV = "conv-1"
CWD = "/work/proj"
AT_REST = "Done with step 2.\n\n❯ \n  ⏵⏵ auto mode on"


def snap(tail=AT_REST, *, state="idle", conv=CONV, cwd=CWD, alive=True, is_agent=True,
         found=True, capture_ok=True, pending="", shell_running=False):
    return {"found": found, "capture_ok": capture_ok, "target": T, "session": "proj",
            "alive": alive, "is_agent": is_agent, "state": state, "tail": tail, "cwd": cwd,
            "conversation_id": conv, "conv_mtime": "2026-10-22T00:00:00",
            "shell_running": shell_running, "pending": pending}


class FakePorts:
    def __init__(self):
        self.events = []
        self.snap = snap()
        self.wake_decision = {"decision": "wake", "reason": "actionable_waiting_transition"}
        self.wake_state = "delivered"
        self.eligible_sessions = {"proj"}
        self.foreign = False
        self.continue_ok = True
        self.continue_raises = False
        self.lease_ok = True
        self.wakes, self.sends, self.owner_emits, self.stall_emits = [], [], [], []
        self.max_id = 0
        self.wake_by_id = {}
        self.on_inspect = None
        self.arts = {"head": "abc123", "dirty_files": 0}

    def events_since(self, cursor, limit, conn):
        return [e for e in self.events if e["id"] > cursor][:limit]

    def max_event_id(self, conn):
        return self.max_id

    def wake(self, ev, conn):
        self.wakes.append(ev["id"])
        return dict(self.wake_decision)

    def wake_status(self, event_id, conn):
        return self.wake_by_id.get(event_id, self.wake_state)

    def inspect(self, target):
        if self.on_inspect:
            self.on_inspect()
        s = self.snap
        return dict(s() if callable(s) else s)

    def artifacts(self, cwd):
        return dict(self.arts)

    def eligible(self, session):
        return session in self.eligible_sessions

    def foreign_send_since(self, target, *, event_ts, guard_since):
        return self.foreign

    def lease(self, target, conn):
        return {"lease_id": "L1"} if self.lease_ok else None

    def release(self, target, lease, conn):
        pass

    def continue_same_session(self, target, cwd, text):
        self.sends.append((target, cwd, text))
        if self.continue_raises:
            raise RuntimeError("process died mid-send")
        return {"ok": self.continue_ok}

    def emit_owner_needed(self, row, reason, conn):
        self.owner_emits.append(reason)

    def emit_stalled(self, row, since_ts, now, conn):
        self.stall_emits.append(since_ts)


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "1")
    for name, val in (("VERIFY_SETTLE_SECS", 60), ("WAKE_CONFIRM_TIMEOUT_SECS", 600),
                      ("WAKE_COOLDOWN_SECS", 300), ("CONTINUE_COOLDOWN_SECS", 300),
                      ("MAX_CONTINUATIONS", 2), ("CONTINUATION_WINDOW_SECS", 3600),
                      ("STALL_SECS", 1800), ("INTERVAL", 60)):
        monkeypatch.setattr(aes, name, val)
    yield


def ev(eid, type_, ts=1000.0, target=T, payload=""):
    return {"id": eid, "type": type_, "agent_id": target, "project_id": "proj", "ts": ts,
            "severity": "high", "owner_action_required": False, "payload": payload}


def state(target=T):
    return aes.status(target)["agents"][0]


def drive(p, *times):
    out = None
    for t in times:
        out = aes.run_once(p, now=t)
    return out


# ── disabled / first start ──────────────────────────────────────────────────
def test_disabled_by_default_does_nothing(monkeypatch):
    monkeypatch.delenv("AGENT_EVENT_SUPERVISOR_ENABLED")
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    assert aes.run_once(p, now=2000)["enabled"] is False
    assert p.wakes == [] and p.sends == [] and aes.status()["agents"] == []


def test_first_start_never_replays_history(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "0")
    p = FakePorts()
    p.events, p.max_id = [ev(1, "work_stopped_incomplete")], 1
    assert aes.run_once(p, now=1000)["cursor_initialised"] is True
    drive(p, 1100, 1200)
    assert p.wakes == [] and p.sends == []
    p.events.append(ev(2, "work_stopped_incomplete", ts=1250))
    drive(p, 1260)
    assert p.wakes == [2]


def test_empty_bus_initialises_once(monkeypatch):
    monkeypatch.setenv("AGENT_EVENT_SUPERVISOR_REPLAY", "0")
    p = FakePorts()
    aes.run_once(p, now=1000)
    p.events = [ev(1, "work_stopped_incomplete", ts=1001)]
    aes.run_once(p, now=1002)
    assert p.wakes == [1]


# ── event -> wake -> verify -> outcome ──────────────────────────────────────
def test_incomplete_event_wakes_then_one_verified_continuation():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    s = state()
    assert s["state"] == "VERIFYING" and p.wakes == [1]
    assert s["wake"]["requested_action"] == "review_supervisor_continues_if_safe"
    assert s["conversation_id"] == CONV and s["cwd"] == CWD
    aes.run_once(p, now=1030)                    # inside settle window: nothing yet
    assert p.sends == []
    drive(p, 1070, 1140)                         # delivery observed, then settle elapses
    assert len(p.sends) == 1 and p.sends[0][0] == T
    s = state()
    assert s["state"] == "WORKING" and s["last_verified_result"] == "continuation_verified"
    assert s["continuation_count"] == 1
    drive(p, 1300, 1600, 2000)                   # later ticks never repeat it
    assert len(p.sends) == 1


def test_wake_delivered_waits_settle_from_delivery_not_event():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    p.wake_state = "pending"
    drive(p, 1001, 1070, 1500)                   # pending, under timeout
    assert p.sends == []
    p.wake_state = "delivered"
    aes.run_once(p, now=1510)                    # first observed delivered: assistant's turn
    assert p.sends == []
    aes.run_once(p, now=1575)
    assert len(p.sends) == 1


def test_completed_event_verified_done():
    p = FakePorts()
    p.events = [ev(1, "task_completed")]
    drive(p, 1001, 1070, 1140)
    s = state()
    assert s["state"] == "DONE" and \
        s["last_verified_result"] == "completion_verified_at_rest_clean_tree"
    assert s["last_evidence"]["artifacts"]["head"] == "abc123"
    assert p.sends == []


def test_completion_claim_with_failing_tests_is_not_done():
    p = FakePorts()
    p.snap = snap("Ran suite.\n3 failed, 40 passed\nAll done!\n❯ ")
    p.events = [ev(1, "agent_completed")]
    drive(p, 1001, 1070, 1140)
    assert state()["state"] == "WORKING" and len(p.sends) == 1


@pytest.mark.parametrize("etype", ["agent_process_failed", "agent_dead"])
def test_failure_events_with_dead_agent_fail(etype):
    p = FakePorts()
    p.events = [ev(1, etype)]
    aes.run_once(p, now=1001)
    p.snap = snap(alive=False)
    drive(p, 1070, 1140)
    assert state()["state"] == "FAILED" and p.sends == []


def test_failure_event_with_vanished_pane_fails_not_recreated():
    p = FakePorts()
    p.events = [ev(1, "agent_dead")]
    p.snap = {"found": False, "capture_ok": False}
    drive(p, 1001, 1070, 1140)
    assert state()["state"] == "FAILED" and p.sends == []


def test_prompt_needs_response_is_owner_needed():
    p = FakePorts()
    p.events = [ev(1, "agent_prompt_needs_response")]
    drive(p, 1001, 1070, 1140)
    s = state()
    assert s["state"] == "OWNER_NEEDED" and p.sends == []
    assert p.owner_emits == ["agent_prompt_needs_response"]


def test_permission_dialog_is_owner_needed():
    p = FakePorts()
    p.snap = snap("Bash command\n  ls\nDo you want to proceed?\n❯ 1. Yes", state="waiting_owner")
    p.events = [ev(1, "agent_waiting_input")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == "permission_prompt_pending" and p.sends == []


@pytest.mark.parametrize("tail,cat", [
    ("Ready. Next: deploy to production.\n❯ ", "prod_release"),
    ("Need to top-up the OpenAI credits ($20) to proceed\n❯ ", "money_spend"),
    ("Please paste the API key for the Stripe sandbox\n❯ ", "credentials_secrets"),
    ("The site shows a reCAPTCHA; I could solve it via a service\n❯ ", "captcha_antibot"),
    ("Rate limited; a residential proxy would fix it\n❯ ", "paid_proxy_vpn"),
    ("Next I will run git push --force to rewrite main\n❯ ", "irreversible"),
    ("Should the plan use Postgres or SQLite?\n❯ ", "product_decision"),
    ("Готово. Дальше деплой на прод.\n❯ ", "prod_release"),
])
def test_owner_gated_waiting_event_gets_no_continuation(tail, cat):
    assert aes.owner_gate_reason(tail) == cat
    p = FakePorts()
    p.snap = snap(tail)
    p.events = [ev(1, "agent_waiting_input")]
    drive(p, 1001, 1070, 1140, 1500)
    s = state()
    assert s["state"] == "OWNER_NEEDED" and s["owner_gate_reason"] == f"owner_gate:{cat}"
    assert p.sends == []


def test_owner_decision_event_never_continues_even_if_pane_looks_benign():
    p = FakePorts()
    p.events = [ev(1, "owner_decision_required")]
    drive(p, 1001, 1070, 1140)
    assert state()["state"] == "OWNER_NEEDED" and p.sends == []


@pytest.mark.parametrize("q", ["Shall I continue with the next step?", "Want me to proceed?"])
def test_benign_continue_question_is_not_a_gate(q):
    assert aes.owner_gate_reason(f"Step 2 finished.\n{q}\n❯ ") == ""


def test_footer_chrome_does_not_trigger_gates():
    assert aes.owner_gate_reason(
        "Edited core/x.py\n❯ \n  ⏵⏵ bypass permissions on · ? for shortcuts") == ""


# ── dedupe / cooldown ───────────────────────────────────────────────────────
def test_same_event_is_ledgered_once_and_burst_is_collapsed():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete"), ev(2, "work_stopped_incomplete", ts=1002)]
    aes.run_once(p, now=1003)
    assert p.wakes == [1]                        # burst of the same kind = one episode
    s = aes.run_once(p, now=1004)
    assert s["accepted"] == 0                    # cursor advanced; nothing re-handled
    drive(p, 1070, 1140)
    assert len(p.sends) == 1


def test_wake_cooldown_suppresses_second_wake_for_a_new_episode():
    p = FakePorts()
    p.snap = snap("Should I use Redis?\n❯ ")     # owner-gated: episode ends OWNER_NEEDED
    p.events = [ev(1, "agent_waiting_input")]
    drive(p, 1001, 1070, 1140)
    p.events.append(ev(2, "work_stopped_incomplete", ts=1150))
    aes.run_once(p, now=1151)
    assert p.wakes == [1]
    assert state()["wake"]["decision"] == "suppressed"


def test_continuation_cooldown_and_budget():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    assert len(p.sends) == 1
    p.events.append(ev(2, "agent_waiting_input", ts=1200))
    drive(p, 1201, 1270, 1340)                   # inside 300s continue cooldown
    assert len(p.sends) == 1 and state()["next_action"] == "wait_cooldown"
    drive(p, 1450, 1520)
    assert len(p.sends) == 2                     # cooldown over: second (and last) allowed
    p.events.append(ev(3, "work_stopped_incomplete", ts=2000))
    drive(p, 2001, 2070, 2140)
    s = state()
    assert len(p.sends) == 2 and s["owner_gate_reason"] == "continuation_budget_exhausted"


# ── same agent/session/worktree only ────────────────────────────────────────
@pytest.mark.parametrize("change,reason", [
    ({"conv": "conv-2"}, "not_same_session:conversation_changed"),
    ({"cwd": "/work/other"}, "not_same_session:worktree_changed"),
    ({"conv": None}, "not_same_session:conversation_unknown"),
])
def test_continuation_refused_when_session_identity_changed(change, reason):
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    p.snap = snap(**change)
    drive(p, 1070, 1140)
    assert state()["owner_gate_reason"] == reason and p.sends == []


def test_identity_not_pinned_at_event_time_fails_closed():
    p = FakePorts()
    p.snap = {"found": True, "capture_ok": False}  # unreadable when the event arrived
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    p.snap = snap()
    drive(p, 1070, 1140)
    assert state()["owner_gate_reason"] == "not_same_session:identity_not_pinned_at_event"
    assert p.sends == []


def test_not_eligible_session_is_owner_needed():
    p = FakePorts()
    p.eligible_sessions = set()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == "session_not_auto_continue_eligible"
    assert p.sends == []


def test_someone_else_already_sent_into_pane_means_no_duplicate():
    p = FakePorts()
    p.foreign = True
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    assert state()["state"] == "WORKING" and p.sends == []


def test_lease_held_by_other_controller_means_no_send():
    p = FakePorts()
    p.lease_ok = False
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    s = state()
    assert p.sends == [] and s["state"] == "VERIFYING"
    assert s["next_action"] == "retry_after_lease"
    p.lease_ok = True                            # holder released: next tick re-judges
    drive(p, 1200)
    assert len(p.sends) == 1


# ── idle vs background shell ────────────────────────────────────────────────
@pytest.mark.parametrize("tail,expect", [
    ("✻ Pondering… (12s · ↓ 1.2k tokens · esc to interrupt)\n❯ ", "claude_working"),
    ("Started tests in background.\n❯ \n  ⏵⏵ auto mode on · 1 shell", "background"),
    ("Monitoring.\n❯ \n  ⏵⏵ auto mode on · 2 monitors · ← 1 agent", "background"),
    ("✻ Waiting for 2 background\n  agents to finish\n❯ ", "background"),
    ("✻ Baked for 1m · 1 shell still running\nmore output\n❯ ", "at_rest"),
    (AT_REST, "at_rest"),
])
def test_activity_distinguishes_claude_idle_from_background_work(tail, expect):
    assert aes.activity(snap(tail, state="working" if expect != "at_rest" else "idle")) \
        == expect


def test_idle_claude_with_background_shell_is_working_not_continued():
    p = FakePorts()
    p.snap = snap("Kicked off the suite.\n❯ \n  ⏵⏵ auto mode on · 1 shell", state="working")
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    s = state()
    assert s["state"] == "WORKING" and s["last_verified_result"] == \
        "background_shell_progressing" and p.sends == []


def test_foreground_shell_running_is_background_work():
    assert aes.activity(snap(shell_running=True)) == "background"


# ── sticky pause ────────────────────────────────────────────────────────────
def test_pause_is_sticky_and_needs_explicit_unpause():
    p = FakePorts()
    aes.pause(T, by="owner", reason="hands off", now=900)
    p.events = [ev(1, "work_stopped_incomplete"), ev(2, "agent_waiting_input", ts=1100),
                ev(3, "task_completed", ts=1200)]
    drive(p, 1001, 1101, 1201, 1500, 5000, 99999)
    s = state()
    assert s["state"] == "PAUSED" and s["last_event_id"] == 3
    assert p.wakes == [] and p.sends == []
    aes.run_once(FakePorts(), now=100000)        # "restart": still paused
    assert state()["state"] == "PAUSED"
    with pytest.raises(ValueError):
        aes.unpause(T, by="")
    assert aes.unpause(T, by="owner", now=100001)["state"] == "WORKING"
    drive(p, 100002, 100100)                     # paused-era events are NOT replayed
    assert p.wakes == [] and p.sends == []
    p.events.append(ev(4, "work_stopped_incomplete", ts=100200))
    drive(p, 100201, 100270, 100340)
    assert p.wakes == [4] and len(p.sends) == 1


def test_pause_mid_verification_stops_continuation():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    aes.pause(T, by="owner", now=1010)
    drive(p, 1070, 1140, 2000)
    assert p.sends == [] and state()["state"] == "PAUSED"


# ── fail closed ─────────────────────────────────────────────────────────────
def test_ambiguous_wake_delivery_fails_closed():
    p = FakePorts()
    p.wake_state = "pending"                     # submitted/decided, never confirmed
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1400)
    assert state()["state"] == "VERIFYING" and p.sends == []
    drive(p, 1700, 1800)
    s = state()
    assert s["owner_gate_reason"] == "wake_delivery_ambiguous" and p.sends == []


def test_explicit_ambiguous_status_fails_closed_immediately():
    p = FakePorts()
    p.wake_state = "ambiguous"
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    assert state()["owner_gate_reason"] == "wake_delivery_ambiguous" and p.sends == []


def test_wake_not_sent_still_verifies_without_assistant():
    p = FakePorts()
    p.wake_decision = {"decision": "skip", "reason": "bridge_disabled"}
    p.wake_state = "not_sent"
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    assert len(p.sends) == 1


def test_unreadable_pane_fails_closed():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    p.snap = {"found": True, "capture_ok": False}
    drive(p, 1070, 1140)
    assert state()["owner_gate_reason"] == "verification_ambiguous" and p.sends == []


def test_foreign_pending_input_is_never_overwritten():
    p = FakePorts()
    p.snap = snap(pending="half-typed owner text")
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == "foreign_pending_input" and p.sends == []


def test_unverified_continuation_is_owner_needed_and_never_retried():
    p = FakePorts()
    p.continue_ok = False
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140, 2000, 3000)
    assert len(p.sends) == 1 and state()["owner_gate_reason"] == "continuation_unverified"


# ── restart survival ────────────────────────────────────────────────────────
def test_state_and_cursor_survive_restart():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    p2 = FakePorts()                              # fresh process, same DB
    p2.events = list(p.events)
    drive(p2, 1070, 1140)
    assert p2.wakes == [] and len(p2.sends) == 1  # not re-woken; continued once
    assert aes.status()["cursor"] == 1


def test_crash_mid_continuation_resolves_owner_needed_without_resend():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    aes.run_once(p, now=1070)
    # simulate death between latch and result: latch row exists, finished=0
    from core.control_plane.api import _c
    conn, _ = _c(None)
    aes._conn(conn)
    conn.execute("INSERT INTO aes_continuation (event_key,target,latched_ts,finished) "
                 "VALUES ('evt:1',?,1100,0)", (T,))
    conn.commit()
    conn.close()
    p2 = FakePorts()
    drive(p2, 1200, 1300, 1400)
    s = state()
    assert p2.sends == [] and p.sends == []
    assert s["owner_gate_reason"] == "continuation_outcome_unknown_after_restart"


# ── internal stall detection ────────────────────────────────────────────────
def test_at_rest_without_progress_emits_one_stall_event():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)                   # continued → WORKING, progress at 1140
    for t in range(1200, 3100, 60):
        aes.run_once(p, now=t)
    assert len(p.stall_emits) >= 1 and set(p.stall_emits) == {1140}
    p.events.append(ev(2, "agent_no_progress", ts=3000))
    drive(p, 3100, 3200)
    assert state()["last_event_kind"] == "stalled"


def test_ignores_paneless_and_own_events():
    p = FakePorts()
    p.events = [ev(1, "task_failed", target="runtimejob:abc"),
                ev(2, "agent_dead", target="runtimejob:abc"),
                ev(3, aes.EVENT_OWNER_NEEDED)]
    drive(p, 1001, 1100)
    assert p.wakes == [] and aes.status()["agents"] == []


# ── read-only API ───────────────────────────────────────────────────────────
def _worktree_api_v1():
    """tools/wake_companion.py and tools/cdp_composer.py put the hard-coded live tree
    (/root/ai-dev-runtime) first on sys.path when their tests run; a later `import api`
    would then load the LIVE api package instead of this checkout's. Load ours explicitly."""
    import importlib
    import pathlib
    import sys
    root = str(pathlib.Path(__file__).resolve().parents[1])
    mod = sys.modules.get("api")
    if mod is not None and not (getattr(mod, "__file__", "") or "").startswith(root):
        for name in [n for n in sys.modules if n == "api" or n.startswith("api.")]:
            del sys.modules[name]
    sys.path.insert(0, root)
    try:
        return importlib.import_module("api.v1")
    finally:
        sys.path.remove(root)


def test_status_api_is_read_only_and_reports_required_fields():
    import asyncio
    v1 = _worktree_api_v1()
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    before = aes.status()
    out = asyncio.run(v1.agents_event_supervisor_status(target=T, limit=10, _=True))
    a = out["agents"][0]
    for f in ("target", "project", "state", "last_event_id", "last_check_at",
              "last_progress_at", "last_verified_result", "last_evidence", "next_action",
              "owner_gate_reason", "continuation_count", "continuation_window_start"):
        assert f in a
    assert aes.status() == before
    hist = asyncio.run(v1.agents_event_supervisor_history(target=T, limit=10, _=True))
    assert [h["to_state"] for h in hist["transitions"]][:2] == ["WORKING", "CONTINUE"]


def test_owner_cli_pause_unpause_roundtrip(capsys):
    from tools import agent_event_supervisor as cli
    assert cli.main(["pause", T, "--by", "owner", "--reason", "manual"]) == 0
    assert state()["state"] == "PAUSED"
    with pytest.raises(SystemExit):
        cli.main(["unpause", T])                 # --by is mandatory
    assert state()["state"] == "PAUSED"
    assert cli.main(["unpause", T, "--by", "owner"]) == 0
    assert state()["state"] == "WORKING"


# ── review regressions ──────────────────────────────────────────────────────
def test_gate_sees_a_question_that_scrolled_far_up():
    tail = "Should I charge the customer card now?\n" + "\n".join(
        f"log line {i}" for i in range(35)) + "\n❯ "
    assert aes.owner_gate_reason(tail) == "money_spend"


def test_waiting_input_state_counts_as_staged_text_even_if_pending_read_empty():
    p = FakePorts()
    p.snap = snap(state="waiting_input", pending="")
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == "foreign_pending_input" and p.sends == []


def test_stale_backlog_is_ledgered_never_acted_on():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete", ts=1000)]
    drive(p, 1000 + 3601, 9000, 9100)
    assert p.wakes == [] and p.sends == [] and aes.status()["agents"] == []


def test_second_event_cannot_wipe_an_unresolved_first_wake():
    p = FakePorts()
    p.wake_by_id = {1: "pending", 2: "not_sent"}
    p.events = [ev(1, "work_stopped_incomplete"), ev(2, "agent_waiting_input", ts=1005)]
    drive(p, 1001, 1006, 1070, 1300)
    assert p.sends == [] and state()["wake_status"] == "pending"
    p.wake_by_id[1] = "delivered"
    drive(p, 1310, 1380)
    assert len(p.sends) == 1


def test_crash_after_bridge_commit_still_resolves_wake_from_bridge():
    p = FakePorts()
    p.wake_by_id = {1: "pending"}                # bridge decided; companion not done yet
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    from core.control_plane.api import _c
    conn, _ = _c(None)
    conn.execute("DELETE FROM aes_wake")         # we "died" before writing our wake row
    conn.commit()
    conn.close()
    drive(p, 1070, 1200)
    assert p.sends == [] and state()["wake_status"] == "pending"


def test_wake_error_without_audit_row_is_ambiguous():
    class Boom(FakePorts):
        def wake(self, ev, conn):
            raise RuntimeError("bridge exploded")
    p = Boom()
    p.wake_state = "not_sent"
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)
    assert state()["owner_gate_reason"] == "wake_delivery_ambiguous" and p.sends == []


def test_pause_landing_mid_verification_wins_over_the_sweep():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    calls = {"n": 0}

    def owner_pauses_now():
        calls["n"] += 1
        if calls["n"] == 1:
            aes.pause(T, by="owner", now=1069)
    p.on_inspect = owner_pauses_now
    drive(p, 1070, 1140, 2000)
    assert p.sends == [] and state()["state"] == "PAUSED" and state()["paused"] == 1


def test_recovered_latch_escalates_and_counts_even_if_a_newer_event_arrived():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    from core.control_plane.api import _c
    conn, _ = _c(None)
    conn.execute("INSERT INTO aes_continuation (event_key,target,latched_ts,finished) "
                 "VALUES ('evt:0',?,1050,0)", (T,))
    conn.commit()
    conn.close()
    p.events.append(ev(2, "work_stopped_incomplete", ts=1060))
    drive(p, 1070, 1140, 1200)
    s = state()
    assert p.sends == [] and s["continuation_count"] == 1
    assert p.owner_emits[0] == "continuation_outcome_unknown_after_restart"
    p.events.append(ev(3, "work_stopped_incomplete", ts=1250))
    drive(p, 1251, 1320)                         # inside cooldown of the unknown send
    assert p.sends == []


@pytest.mark.parametrize("arts,reason", [
    ({"head": "abc", "dirty_files": 4}, "completion_with_uncommitted_changes"),
    ({}, "completion_unverifiable_no_artifacts"),
])
def test_done_requires_clean_tree_evidence(arts, reason):
    p = FakePorts()
    p.arts = arts
    p.events = [ev(1, "task_completed")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == reason and p.sends == []


def test_transcript_advanced_since_event_means_someone_acted():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    aes.run_once(p, now=1001)
    moved = snap()
    moved["conv_mtime"] = "2026-10-22T00:05:00"
    p.snap = moved
    drive(p, 1070, 1140)
    assert state()["last_verified_result"] == "conversation_advanced_since_event"
    assert p.sends == []


def test_hung_background_work_escalates_on_stall():
    p = FakePorts()
    p.snap = snap("tail -f log\n❯ \n  ⏵⏵ auto mode on · 1 shell", state="working")
    p.events = [ev(1, "agent_no_progress")]
    drive(p, 1001, 1070, 1140)
    assert state()["owner_gate_reason"] == "background_work_no_progress"


def test_stall_watch_resurfaces_each_window():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    drive(p, 1001, 1070)                         # continued at 1070
    for t in range(1130, 1070 + 1800 * 3, 60):
        aes.run_once(p, now=t)
    assert len(p.stall_emits) >= 2               # port dedups per window; still re-offered


def test_one_bad_event_does_not_wedge_the_cursor():
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete", target="bad:0.0"),
                ev(2, "work_stopped_incomplete")]
    real = p.inspect

    def inspect(target):
        if target == "bad:0.0":
            raise RuntimeError("tmux exploded")
        return real(target)
    p.inspect = inspect
    s = aes.run_once(p, now=1001)
    assert s["errors"] == 1 and aes.status()["cursor"] == 2 and p.wakes == [2]


def test_sweep_lease_serialises_workers():
    from core.control_plane import api as cp
    assert cp.acquire_lease("agent_event_supervisor:sweep", "other-worker", ttl_secs=300,
                            now=1000)
    p = FakePorts()
    p.events = [ev(1, "work_stopped_incomplete")]
    assert aes.run_once(p, now=1001)["skipped"] == "sweep_lease_held"
    assert p.wakes == []
    assert aes.run_once(p, now=1400)["accepted"] == 1     # lease expired → next sweep runs


def test_real_ports_ack_without_delivery_is_ambiguous_and_superseded_is_not_sent(monkeypatch):
    from core import wake_bridge as wb
    from core.control_plane.api import _c
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    wb.bind_chat("https://chatgpt.com/c/aes-test")
    rp = aes.Ports()
    conn, _ = _c(None)
    for eid in (11, 12, 13):
        d = wb.should_wake(event_id=eid, severity="critical", event_type="agent_waiting_input",
                           conn=conn, now=1000.0 + eid * 100)
        wb.record(d, event_id=eid, severity="critical", event_type="agent_waiting_input",
                  conn=conn, now=1000.0 + eid * 100)
    wb.acknowledge(11, conn=conn)                # retired/acked, never delivered
    conn.execute("UPDATE wake_audit SET superseded_by=99 WHERE event_id=12")
    conn.commit()
    assert rp.wake_status(11, conn) == "ambiguous"
    assert rp.wake_status(12, conn) == "not_sent"
    assert rp.wake_status(13, conn) == "pending"
    wb.mark_submitted(13, conn=conn)
    wb.record_delivery("companion", event_id=13, delivered=True, conn=conn)
    assert rp.wake_status(13, conn) == "delivered"
    conn.close()


def test_sweep_write_of_a_stale_row_cannot_unpause():
    from core.control_plane.api import _c
    aes.pause(T, by="owner", now=1000)
    conn, _ = _c(None)
    aes._conn(conn)
    stale = {"target": T, "state": "CONTINUE", "paused": 0}
    assert aes._put_agent(conn, stale, prev_state="VERIFYING", now=1001) is False
    conn.commit()
    conn.close()
    assert state()["state"] == "PAUSED" and state()["paused"] == 1
