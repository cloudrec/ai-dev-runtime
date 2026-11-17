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
    cfg = tmp_path / "policy.yaml"
    cfg.write_text("hard_deny_dirs:\n  - /opt/payment-orchestrator\n  - /opt/vault-core\n")
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(cfg))
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


# ── 2026-11-17 independent security review findings ─────────────────────────
def test_r1_an_unknown_mode_offers_nothing(monkeypatch):
    monkeypatch.delenv("WAKE_BRIDGE_ONLY_AGENTS")
    for typo in ("nonpaymnet", "all", "off", "*"):
        assert not wb.offer_allowed("worker-a:0.0", mode=typo, only=set(), panes=dict(PANES))
        assert not wb.offer_allowed("payorch-ha-fresh:0.0", mode=typo, only=set())


def test_r2_identity_recorded_as_payment_wins_over_a_reused_session_name():
    # `worker-a` was a payment pane when the event was decided; the name now belongs
    # to an ordinary pane. The recorded verdict keeps it out.
    assert not wb.offer_allowed("worker-a:0.0", panes=dict(PANES), stored=wb.ID_PAYMENT)


def test_r4_a_crashed_agent_is_offered_on_its_recorded_identity():
    assert wb.offer_allowed("crashed:0.0", panes=dict(PANES), stored=wb.ID_OK)
    assert not wb.offer_allowed("crashed:0.0", panes=dict(PANES), stored=wb.ID_UNKNOWN)
    assert not wb.offer_allowed("crashed:0.0", panes=dict(PANES), stored="")


def test_r4_record_stores_the_live_identity(monkeypatch):
    monkeypatch.setenv("WAKE_BRIDGE_IDENTITY", "1")
    _wake(931, "worker-a:0.0", 1000.0)
    _wake(932, "renamed:0.0", 1100.0)
    _wake(933, "ghost:0.0", 1200.0)
    conn, _ = wb._conn(None)
    got = dict(conn.execute("SELECT event_id, offer_identity FROM wake_audit "
                            "WHERE event_id IN (931,932,933)").fetchall())
    assert got == {931: "ok", 932: "payment", 933: "unknown"}


def test_r4_record_never_reads_tmux_unless_the_unit_opts_in(monkeypatch):
    monkeypatch.delenv("WAKE_BRIDGE_IDENTITY", raising=False)
    monkeypatch.setattr(wb, "pane_cwds", lambda: pytest.fail("tmux read"))
    _wake(934, "worker-a:0.0", 1000.0)


def test_r6_full_cycle_identity_comes_from_the_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("FULLCYCLE_INTAKE_ROOT", "/opt/fc-projects")
    conn, _ = wb._conn(None)
    conn.execute("CREATE TABLE IF NOT EXISTS fc_project (id TEXT PRIMARY KEY, name TEXT, "
                 "root TEXT, executor TEXT, created_at TEXT, created_ts REAL)")
    conn.executemany("INSERT INTO fc_project (id, root) VALUES (?,?)", [
        ("calc", "/opt/fc-projects/calc"), ("odd", "/opt/elsewhere/odd"),
        ("vault", "/opt/vault-core/x")])
    conn.commit()
    assert wb.identity_verdict("fc-calc", "calc", conn=conn, panes={}) == wb.ID_OK
    assert wb.identity_verdict("fc-odd", "odd", conn=conn, panes={}) == wb.ID_UNKNOWN
    assert wb.identity_verdict("fc-vault", "vault", conn=conn, panes={}) == wb.ID_PAYMENT


def test_r7_a_hard_deny_dir_without_a_payment_name_is_denied():
    assert not wb.offer_allowed("v:0.0", panes={"v": ["/opt/vault-core/sub"]})


def test_r7_an_unreadable_policy_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(tmp_path / "missing.yaml"))
    assert not wb.offer_allowed("worker-a:0.0", panes=dict(PANES))


def test_r5_payment_words_beyond_the_first_list():
    from core import agent_creation_policy as acp
    for t in ("autonomypay", "pay2-agent", "yookassa-hook", "платежи", "antilopay-x"):
        assert acp.payment_marked(t), t
    for t in ("display", "payload-x", "ebay-shop", "gaika-country-pl"):
        assert not acp.payment_marked(t), t


def test_r5_a_payment_chat_bound_under_a_neutral_key_is_never_rung(monkeypatch):
    conn, _ = wb._conn(None)
    pay = {"https://chatgpt.com/c/pay-chat"}
    resolved = {"route_key": "ops", "conversation": "https://chatgpt.com/c/pay-chat",
                "bound": True}
    assert not wb.offerable(conn, "worker-a:0.0", "", wb.ID_OK, resolved, {"fc-*"},
                            dict(PANES), "nonpayment", pay)


def test_rule1_payment_never_falls_back_to_a_non_payment_chat_even_unfiltered(monkeypatch):
    monkeypatch.delenv("WAKE_BRIDGE_OFFER")
    monkeypatch.delenv("WAKE_BRIDGE_ONLY_AGENTS")
    conn, _ = wb._conn(None)
    owner_os = {"route_key": "owner-os", "conversation": "https://c/owner", "bound": True}
    pay_chat = {"route_key": "payment-orchestrator", "conversation": "https://c/pay",
                "bound": True}
    assert not wb.offerable(conn, "payorch-ha-fresh:0.0", "", "", owner_os, set(), {}, "",
                            set())
    assert not wb.offerable(conn, "neutral:0.0", "", wb.ID_PAYMENT, owner_os, set(), {}, "",
                            set())
    # unfiltered, a payment event may still ring its OWN payment chat (unchanged)
    assert wb.offerable(conn, "payorch-ha-fresh:0.0", "", "", pay_chat, set(), {}, "", set())
    # filtered: no payment chat at all
    assert not wb.offerable(conn, "payorch-ha-fresh:0.0", "", "", pay_chat, {"x"}, {}, "",
                            set())


def test_r3_a_filtered_generic_wake_never_swallows_a_deliverable_one():
    def gen(eid, agent, now):          # generic (non-actionable): these are coalesced
        d = wb.should_wake(event_id=eid, severity="critical", agent_id=agent,
                           event_type="deploy_failed", now=now)
        wb.record(d, event_id=eid, severity="critical", agent_id=agent,
                  event_type="deploy_failed", now=now)
        assert d["wake"], d
    t2 = 1000.0 + wb.COOLDOWN_SECS + 1
    gen(941, "worker-a:0.0", 1000.0)
    gen(942, "runtimejob:abcd1234", t2)                # newer, never offerable
    p = wb.pending_wake(now=t2 + 1)
    assert p.get("pending") and p["event_id"] == 941, p


def test_r8_a_long_filtered_backlog_does_not_hide_a_deliverable_wake():
    conn, _ = wb._conn(None)
    for i in range(wb._CANDIDATE_SCAN_LIMIT + 20):
        conn.execute("INSERT INTO wake_audit (ts,at,event_id,decision,reason,actionable,"
                     "agent_id,acknowledged) VALUES (?,?,?,?,?,?,?,0)",
                     (500.0 + i, "x", 50000 + i, "wake", "t", 1, "payorch-ha-fresh:0.0"))
    conn.commit()
    _wake(951, "worker-a:0.0", 1000.0)
    p = wb.pending_wake(now=1100.0)
    assert p.get("pending") and p["event_id"] == 951, p


def test_r9_loop_watch_does_not_alarm_on_a_wake_whose_route_is_payment(monkeypatch):
    _wake(961, "worker-a:0.0", 1000.0)
    conn, _ = wb._conn(None)
    real = wb.wake_routes.resolve
    monkeypatch.setattr(wb.wake_routes, "resolve", lambda **kw: {
        **real(**kw), "route_key": "payment-orchestrator"})
    import core.wake_routes as wr
    monkeypatch.setattr(wr, "resolve", wb.wake_routes.resolve)
    assert lw.undelivered_wakes(conn, 1000.0 + lw.UNDELIVERED_AFTER + 10) == {}


def test_live_policy_denies_every_known_payment_dir():
    """2026-11-17: /opt/vtb_old (QR payments via Bakai Bank) carried no payment word in
    its session or path; it is denied by the policy's hard_deny_dirs."""
    import pathlib
    import yaml
    from core import agent_creation_policy as acp
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "config" /
                          "agent_creation.yaml").read_text())
    for d in ("/opt/payment-orchestrator", "/opt/vtb_old", "/opt/vtb_old/app"):
        assert acp.hard_denied(d, "vtb", cfg), d
    assert not acp.hard_denied("/opt/vtb_older", "x", cfg)
