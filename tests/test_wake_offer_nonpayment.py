"""WAKE_BRIDGE_OFFER=nonpayment (2026-11-17): the tech lead hears every agent with an
authoritative identity, never payment work. Synthetic targets only; tmux is faked."""
from __future__ import annotations

import pytest

from core import loop_watch as lw
from core import wake_bridge as wb

PANES = {"worker-a": ["/opt/worker-a"], "newcomer": ["/opt/newcomer"],
         "renamed": ["/opt/payorch-wt-x"], "edge": ["/opt/blagopay-edge"],
         "split": ["/opt/ok", "/opt/payment-orchestrator/sub"], "blank": [""]}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    monkeypatch.setenv("WAKE_BRIDGE_OFFER", "nonpayment")
    monkeypatch.setenv("WAKE_BRIDGE_ONLY_AGENTS", "fc-*")
    monkeypatch.setattr(wb, "pane_cwds", lambda: dict(PANES))
    wb.bind_chat("https://chatgpt.com/c/default-test-chat")


def _wake(eid, agent, now, project=""):
    d = wb.should_wake(event_id=eid, severity="critical", agent_id=agent, project_id=project,
                       event_type="work_stopped_incomplete", now=now)
    wb.record(d, event_id=eid, severity="critical", agent_id=agent, project_id=project,
              event_type="work_stopped_incomplete", now=now)
    assert d["wake"], d


def _offered(now):
    out = []
    for i in range(20):
        p = wb.pending_wake(now=now + i)
        if not p.get("pending"):
            return out
        out.append(p["event_id"])
        wb.acknowledge(p["event_id"])
    return out


@pytest.mark.parametrize("agent,project,ok", [
    ("worker-a:0.0", "", True),
    ("newcomer:0.0", "", True),                     # new agent: no list edit needed
    ("fc-proj", "proj", True),                      # explicit list still works
    ("payorch-ha-fresh:0.0", "", False),
    ("autonomy-pay-sonnet:0.0", "", False),
    ("renamed:0.0", "", False),                     # harmless name, payment cwd
    ("edge:0.0", "", False),
    ("split:0.0", "", False),                       # one pane in payment dir: whole agent out
    ("worker-a:0.0", "payment-orchestrator", False),  # payment project
    ("fc-blagopay-x", "", False),                   # listed prefix cannot launder payment
    ("ghost:0.0", "", False),                       # no live pane: unknown identity
    ("blank:0.0", "", False),                       # pane without cwd
    ("worker-a", "", False),                        # not an exact target
    ("session:6b65158d-d90", "", False),            # hook id: not authoritative
    ("", "", False),
])
def test_offer_allowed(agent, project, ok):
    assert wb.offer_allowed(agent, project, "", panes=dict(PANES)) is ok


def test_tmux_unreadable_offers_only_the_explicit_list():
    assert not wb.offer_allowed("worker-a:0.0", panes=None)
    assert wb.offer_allowed("fc-proj", "proj", panes=None)


def test_payment_route_is_never_rung():
    assert not wb.offer_allowed("worker-a:0.0", "", "payment-orchestrator", panes=dict(PANES))


def test_list_mode_never_offers_payment_even_if_listed(monkeypatch):
    monkeypatch.delenv("WAKE_BRIDGE_OFFER")
    monkeypatch.setenv("WAKE_BRIDGE_ONLY_AGENTS", "payorch-ha-fresh,worker-a,fc-*")
    assert wb.offer_allowed("worker-a:0.0")
    assert not wb.offer_allowed("payorch-ha-fresh:0.0")
    assert not wb.offer_allowed("fc-blagopay-x")
    assert not wb.offer_allowed("newcomer:0.0", panes=dict(PANES))   # list mode: not listed


def test_no_filter_at_all_is_unchanged(monkeypatch):
    monkeypatch.delenv("WAKE_BRIDGE_OFFER")
    monkeypatch.delenv("WAKE_BRIDGE_ONLY_AGENTS")
    assert wb.offer_allowed("worker-a:0.0") and wb.offer_allowed("payorch-ha-fresh:0.0")


def test_pending_wake_end_to_end_skips_payment_and_unknown_keeps_them_pending():
    _wake(901, "payorch-ha-fresh:0.0", 1000.0)
    _wake(902, "ghost:0.0", 1100.0)
    _wake(903, "renamed:0.0", 1200.0)
    _wake(904, "newcomer:0.0", 1300.0)
    _wake(905, "worker-a:0.0", 1400.0)
    assert _offered(1500.0) == [904, 905]
    conn, own = wb._conn(None)
    left = {r[0] for r in conn.execute(
        "SELECT event_id FROM wake_audit WHERE decision='wake' AND acknowledged=0")}
    assert {901, 902, 903} <= left                     # passed over, never consumed


def test_payment_chat_found_only_at_delivery_is_not_rung(monkeypatch):
    _wake(911, "worker-a:0.0", 1000.0)
    real = wb.wake_routes.resolve

    def to_payment(**kw):
        r = dict(real(**kw))
        r["route_key"] = "payment-orchestrator"
        return r
    monkeypatch.setattr(wb.wake_routes, "resolve", to_payment)
    assert wb.pending_wake(now=1100.0)["pending"] is False


def test_loop_watch_counts_only_what_would_be_offered():
    _wake(921, "payorch-ha-fresh:0.0", 1000.0)
    _wake(922, "ghost:0.0", 1100.0)
    conn, own = wb._conn(None)
    assert lw.undelivered_wakes(conn, 1100.0 + lw.UNDELIVERED_AFTER + 10) == {}
    _wake(923, "worker-a:0.0", 1200.0)
    assert lw.undelivered_wakes(conn, 1200.0 + lw.UNDELIVERED_AFTER + 10)


def test_pane_cwds_reads_tmux_without_inheriting_TMUX(monkeypatch):
    import subprocess
    seen = {}

    class R:
        returncode, stdout = 0, "a\t/opt/a\nb\t/opt/b\na\t/opt/a2\n"

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return R()
    monkeypatch.undo()                                   # the real pane_cwds
    monkeypatch.setenv("TMUX", "/tmp/x,1,0")
    monkeypatch.setenv("AGENT_TMUX_SOCKET", "sock")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert wb.pane_cwds() == {"a": ["/opt/a", "/opt/a2"], "b": ["/opt/b"]}
    assert seen["argv"][:3] == ["tmux", "-L", "sock"] and "TMUX" not in seen["env"]
