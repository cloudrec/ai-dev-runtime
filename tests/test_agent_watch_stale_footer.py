"""A frozen background-task footer must not hold an agent `working` forever.

2026-11-18, ServerWarden: the pane read `✻ Cogitated for 5m 34s · done 5:46 PM · 1 shell
still running`. That time the shell was genuine (the agent resumed by itself when it
ended), but it exposed the gap: `agent_watch.classify` returns `working` for any active
inventory state BEFORE the quiescence rule, so an active state that rests only on footer
TEXT ("· N shells" without a done stamp, "· N monitors") is never announced, however long
nothing happens.

The rule under test: such a pane becomes `quiescent` (one `work_stopped_incomplete`, never
`task_completed`) only when the active evidence is footer text alone, the Claude process
has no live child, the pane foreground runs no command, the transcript has been silent and
the bottom region has been frozen for AGENT_WATCH_STALE_ACTIVE_SECS. Unknown facts mean
live: no wake. Liveness facts are injected through `aw._live_facts`; no tmux, no /proc.
"""
from __future__ import annotations

import pytest

from core import agent_watch as aw

STALE = 1800.0                    # AGENT_WATCH_STALE_ACTIVE_SECS default
T = "warden:0.0"

# The footer that keeps the inventory active through text alone (path 1a): a shell count
# with no `· done HH:MM` stamp on the turn line.
SHELL_FOOTER = """● Промежуточно готово: ci split, 5 тестов зелёные.
                  ✔ Update installed · /restart to apply
──────────────────────────────────────────────────
❯
──────────────────────────────────────────────────
  ⏵⏵ auto mode on · 1 shell · ⧉ 4"""

# Path 5: an idle composer with a background-monitor footer.
MONITOR_FOOTER = """● Watching the deploy log; will report when it settles.
──────────────────────────────────────────────────
❯
──────────────────────────────────────────────────
  ⏵⏵ auto mode on · 1 monitor"""

# The exact ServerWarden shape (done stamp present, a shell still attached).
WARDEN_1646 = """● False-positive 41 маркеров: нет
✻ Cogitated for 5m 34s · done 5:46 PM · 1 shell still
  running
                  ✔ Update installed · /restart to apply
──────────────────────────────────────────────────
❯
──────────────────────────────────────────────────
  [CAVEMAN]
  ⏵⏵ auto mode on · 1 shell · ⧉ 4 · ← 7 agents"""


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    yield


class _Emit:
    def __init__(self):
        self.calls = []

    def __call__(self, source, etype, **kw):
        self.calls.append({"source": source, "type": etype, **kw})
        return {"event_id": len(self.calls)}


def _facts(monkeypatch, *, child=False, transcript_idle=1e9, raise_exc=False):
    calls = []

    def fake(agent):
        calls.append(agent.get("target"))
        if raise_exc:
            raise OSError("proc unreadable")
        return {"background_live": child, "transcript_idle_secs": transcript_idle}
    monkeypatch.setattr(aw, "_live_facts", fake, raising=False)
    return calls


def _agent(state="shell_running", command="claude"):
    return {"target": T, "cwd": "/opt/warden", "claude_cwd": "/opt/warden", "alive": True,
            "is_agent": True, "state": state, "command": command, "claude_pid": 4242}


def _sweeps(tail, times, emit, **agent_kw):
    out = []
    for now in times:
        out.append(aw.scan(agents=[_agent(**agent_kw)], read_fn=lambda _t: tail,
                           emit_fn=emit, now=now))
    return out


def _stops(emit):
    return [c for c in emit.calls if c["type"] == "work_stopped_incomplete"]


def _cls(r):
    return [e["class"] for e in r["emitted"]]


# ── 1, 5: the gap ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("tail,state", [(MONITOR_FOOTER, "shell_running"),
                                        (SHELL_FOOTER, "working")],
                         ids=["monitor-footer", "shell-footer-no-done-stamp"])
def test_a_frozen_footer_with_no_live_child_is_announced_once(monkeypatch, tail, state):
    """Fails on the released code: the pane stays `working` forever."""
    _facts(monkeypatch)
    emit = _Emit()
    _sweeps(tail, [1000.0, 1000.0 + STALE], emit, state=state)
    stops = _stops(emit)
    assert len(stops) == 1
    assert not [c for c in emit.calls if c["type"] == "task_completed"]
    row = aw._conn(None)[0].execute(
        "SELECT cls FROM agent_watch_state WHERE target=?", (T,)).fetchone()
    assert row[0] == "quiescent"


def test_the_reason_names_the_stale_footer(monkeypatch):
    _facts(monkeypatch)
    c = aw.classify(MONITOR_FOOTER, state="shell_running", prev_cls="working",
                    quiet_secs=STALE, stale_active=True)
    assert c["cls"] == "quiescent" and c["reason"].startswith("stale_active_footer_")


# ── 2, 3, 4, 10: genuine work and unknowns never wake ───────────────────────
def test_a_live_child_process_keeps_it_working_at_any_age(monkeypatch):
    _facts(monkeypatch, child=True)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE * 10], emit)
    assert not _stops(emit)


def test_a_recent_transcript_write_keeps_it_working(monkeypatch):
    _facts(monkeypatch, transcript_idle=STALE - 1)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE * 10], emit)
    assert not _stops(emit)


def test_one_second_short_of_the_bound_is_still_working(monkeypatch):
    _facts(monkeypatch)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE - 1], emit)
    assert not _stops(emit)


def test_transcript_silence_exactly_at_the_bound_qualifies(monkeypatch):
    _facts(monkeypatch, transcript_idle=STALE)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE], emit)
    assert len(_stops(emit)) == 1


@pytest.mark.parametrize("facts", [
    {"child": None}, {"transcript_idle": None}, {"raise_exc": True}],
    ids=["child-unknown", "transcript-unknown", "facts-raise"])
def test_unknown_liveness_is_treated_as_live(monkeypatch, facts):
    _facts(monkeypatch, **facts)
    emit = _Emit()
    r = _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE * 10], emit)
    assert not _stops(emit)
    assert all("unreadable" not in str(x["skipped"]) for x in r)   # the sweep continues


# ── 6, 7: genuine work by other evidence ────────────────────────────────────
def test_a_ticking_spinner_is_never_stale(monkeypatch):
    _facts(monkeypatch)
    emit = _Emit()
    for i in range(6):
        tail = f"✻ Pouncing… ({i * 400}s · ↓ 1.6k tokens)\n  ⏵⏵ auto mode on · 1 shell"
        aw.scan(agents=[_agent(state="working")], read_fn=lambda _t, s=tail: s,
                emit_fn=emit, now=1000.0 + i * STALE)
    assert not _stops(emit)


def test_a_frozen_live_spinner_is_foreground_work_not_footer_text(monkeypatch):
    """A spinner line is foreground evidence; only footer-only activity may be demoted."""
    _facts(monkeypatch)
    emit = _Emit()
    _sweeps("✻ Pouncing… (29s · ↓ 1.6k tokens)\n  ⏵⏵ auto mode on · 1 shell",
            [1000.0, 1000.0 + STALE * 10], emit, state="working")
    assert not _stops(emit)


def test_a_real_foreground_command_is_never_demoted(monkeypatch):
    _facts(monkeypatch)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0, 1000.0 + STALE * 10], emit, command="pytest")
    assert not _stops(emit)


# ── 8: one wake per episode ─────────────────────────────────────────────────
def test_one_wake_per_frozen_episode_and_one_more_after_a_change(monkeypatch):
    _facts(monkeypatch)
    emit = _Emit()
    _sweeps(MONITOR_FOOTER, [1000.0 + i * 600 for i in range(10)], emit)
    assert len(_stops(emit)) == 1
    other = MONITOR_FOOTER.replace("Watching the deploy log", "Watching the build log")
    _sweeps(other, [7000.0 + i * 600 for i in range(10)], emit)
    assert len(_stops(emit)) == 2


# ── 9, 11: everything else is unchanged ─────────────────────────────────────
def test_an_owner_prompt_still_outranks(monkeypatch):
    _facts(monkeypatch)
    emit = _Emit()
    tail = "Do you want to proceed?\n ❯ 1. Yes\n   2. No"
    r = _sweeps(tail, [1000.0, 1000.0 + STALE], emit, state="waiting_owner")
    assert _cls(r[0]) == ["owner_prompt"] and not _stops(emit)


def test_the_warden_1646_shape_with_its_shell_alive_is_working(monkeypatch):
    _facts(monkeypatch, child=True)
    emit = _Emit()
    _sweeps(WARDEN_1646, [1000.0, 1000.0 + STALE * 3], emit, state="working")
    assert not emit.calls


def test_an_inactive_pane_does_not_consult_liveness(monkeypatch):
    """Facts are read only for an active pane with footer-only evidence: no /proc walk
    for every idle agent on every sweep."""
    calls = _facts(monkeypatch)
    aw.scan(agents=[_agent(state="idle")], read_fn=lambda _t: "All done.", emit_fn=_Emit(),
            now=1000.0)
    aw.scan(agents=[_agent(state="working")], read_fn=lambda _t: "✻ Pouncing… (3s · x)",
            emit_fn=_Emit(), now=1000.0)
    # A footer-only active pane is not read either until it has been frozen for the bound.
    _sweeps(MONITOR_FOOTER, [2000.0, 2000.0 + STALE - 1], _Emit())
    assert calls == []


def test_classify_without_the_flag_is_unchanged():
    c = aw.classify(MONITOR_FOOTER, state="shell_running", prev_cls="working",
                    quiet_secs=STALE * 100)
    assert c == {"cls": "working", "reason": "inventory_state_shell_running"}
