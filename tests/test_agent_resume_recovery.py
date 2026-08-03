"""Canonical resume/recovery: a DEAD tmux session (all panes dead) must not block
same-conversation recovery. It is fenced-cleaned (only after proving every pane dead)
and the exact conversation is resumed idempotently, with NO duplicate. A session with a
LIVE pane is still refused.

Reproduces the 2026-08-03 arbitrage2-opus SIGTERM(143) case where agent_resume refused
because the dead session name still existed.
"""
from __future__ import annotations

import pytest

from core import agent_control as ac


class FakeTmux:
    """Scriptable tmux: `panes` is a list of (dead, pid) for the target session."""

    def __init__(self, *, has_session, panes, project_dir="/opt/arbitrage2"):
        self.has_session = has_session
        self.panes = panes
        self.calls = []
        self.killed = False
        self.created = None

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            return (0 if (self.has_session and not self.killed) else 1, "", "")
        if cmd == "list-panes" and "#{pane_dead}\t#{pane_pid}" in args:
            out = "\n".join(f"{d}\t{p}" for d, p in self.panes)
            return (0, out, "") if (self.has_session and not self.killed) else (1, "", "")
        if cmd == "kill-session":
            self.killed = True
            return (0, "", "")
        if cmd == "new-session":
            self.created = args
            return (0, "", "")
        return (0, "", "")


CONV = "64715514-f6bc-4290-9390-cda19127bc17"


def _patch(monkeypatch, fake):
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)   # no live agent
    monkeypatch.setattr(ac, "validate_project_dir", lambda p: p)
    monkeypatch.setattr(ac, "_pid_alive", lambda pid: False)             # dead processes


def test_dead_session_is_cleaned_and_conversation_resumed(monkeypatch):
    fake = FakeTmux(has_session=True, panes=[("1", "3384800")])          # one dead pane
    _patch(monkeypatch, fake)
    res = ac.agent_resume("/opt/arbitrage2", CONV, "arbitrage2-opus")
    assert res["resumed"] is True and res["duplicate_created"] is False
    assert res["conversation_id"] == CONV and res["target"] == "arbitrage2-opus:0.0"
    assert fake.killed is True                                           # fenced cleanup ran
    # new-session used --resume with the exact conversation id
    assert fake.created is not None and "--resume" in fake.created and CONV in fake.created


def test_session_with_live_pane_is_refused(monkeypatch):
    fake = FakeTmux(has_session=True, panes=[("0", "999")])              # a LIVE pane
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda p: p)
    monkeypatch.setattr(ac, "_pid_alive", lambda pid: True)              # process alive
    res = ac.agent_resume("/opt/arbitrage2", CONV, "arbitrage2-opus")
    assert res["resumed"] is False and res["duplicate_created"] is False
    assert fake.killed is False                                          # never killed a live session
    assert "live pane" in res["reason"]


def test_no_session_creates_fresh_without_cleanup(monkeypatch):
    fake = FakeTmux(has_session=False, panes=[])
    _patch(monkeypatch, fake)
    res = ac.agent_resume("/opt/arbitrage2", CONV, "arbitrage2-opus")
    assert res["resumed"] is True and fake.killed is False


def test_liveness_all_dead_detection(monkeypatch):
    fake = FakeTmux(has_session=True, panes=[("1", "1"), ("1", "2")])
    monkeypatch.setattr(ac, "_tmux", fake)
    monkeypatch.setattr(ac, "_pid_alive", lambda pid: False)
    liveness = ac._session_liveness("arbitrage2-opus")
    assert liveness == {"panes": 2, "dead": 2, "live": 0}
