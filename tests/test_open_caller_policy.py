"""Owner 2026-11-17: the GPT tech lead (caller class `api`) drives every agent except
payment work. Payment is closed by session name and by directory, for every caller."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import agent_control as ac  # noqa: E402
from core import agent_creation_policy as acp  # noqa: E402
from core import autonomy  # noqa: E402

CWDS = {"payorch-wt": "/opt/payorch-wt-tabpay", "edge": "/opt/blagopay-edge-rollout",
        "nocwd": ""}


@pytest.fixture
def panes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(tmp_path / "missing.yaml"))
    for k in ("AGENT_AUTOMATION_TARGETS", "AGENT_AUTOMATION_POLICY",
              "AGENT_AUTOMATION_OPEN_CALLERS"):
        monkeypatch.delenv(k, raising=False)
    calls = []

    def fake_tmux(args, stdin=None):
        calls.append(list(args))
        return 0, "", ""

    def fake_pane(target):
        sess = target.split(":")[0]
        return {"target": f"{sess}:0.0", "session": sess, "alive": True,
                "cwd": CWDS.get(sess, f"/opt/{sess}")}
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    monkeypatch.setattr(ac, "_pane_is_live_agent", fake_pane)
    monkeypatch.setattr(ac, "_busy_for_send", lambda t: "")
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return calls


def typed(calls):
    return [c for c in calls if c and c[0] in ("paste-buffer", "send-keys", "load-buffer")]


GPT = "api:client/chatgpt-tech-lead"


def test_open_api_caller_reaches_any_ordinary_agent(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    for t in ("serverwarden-livefire", "gaika-country-hu", "owneros-autonomy-loop"):
        r = ac.agent_send(f"{t}:0.0", "status?", idempotency_key=f"k-{t}", actor=GPT)
        assert not r.get("refused"), r
    assert typed(panes)


def test_without_the_setting_api_stays_on_the_allowlist(panes):
    r = ac.agent_send("serverwarden-livefire:0.0", "x", idempotency_key="k", actor=GPT)
    assert r["refused"] == "automation_target_not_allowlisted" and typed(panes) == []


def test_open_caller_is_only_that_class(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    for actor in (None, "native_supervisor", "cto_bridge"):
        r = ac.agent_send("vtb:0.0", "x", idempotency_key=f"k-{actor}", actor=actor)
        assert r["refused"] == "automation_target_not_allowlisted"
    assert typed(panes) == []


def test_star_never_opens(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "*")
    assert ac.agent_send("vtb:0.0", "x", idempotency_key="k", actor=GPT)["refused"]


@pytest.mark.parametrize("session", ["payorch-ha-fresh", "payorch-wt", "edge",
                                     "autonomy-pay-sonnet", "blagopay-site",
                                     "cloudpayments-test", "payment-orchestrator"])
@pytest.mark.parametrize("actor", [GPT, None, "native_supervisor"])
def test_payment_by_name_or_dir_is_denied_even_when_open(panes, monkeypatch, session, actor):
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", f"{session},{session}@api")
    r = ac.agent_send(f"{session}:0.0", "x", idempotency_key=f"k-{session}-{actor}",
                      actor=actor)
    assert r["refused"] == "hard_denied_target"
    assert typed(panes) == []


def test_open_caller_needs_a_known_cwd(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    r = ac.agent_send("nocwd:0.0", "x", idempotency_key="k", actor=GPT)
    assert r["refused"] == "open_caller_needs_cwd" and typed(panes) == []


@pytest.mark.parametrize("text,paid", [
    ("blagopay-edge-rollout", True), ("/opt/blagopay-ru-site-migration-staging", True),
    ("autonomy-pay-sonnet", True), ("/opt/payorch-wt-tabpay", True),
    ("CloudPayments", True), ("payment-orchestrator", True),
    ("gaika-country-pl", False), ("owneros-autonomy-loop", False),
    ("/opt/fc-projects/paypal-free-text", False), ("display", False), ("", False)])
def test_payment_marked(text, paid):
    assert acp.payment_marked(text) is paid


def test_hard_denied_covers_payment_dirs_without_config():
    assert acp.hard_denied("/opt/blagopay-edge-rollout", "", {})
    assert acp.hard_denied("/opt/x", "autonomy-pay-sonnet", {})
    assert not acp.hard_denied("/opt/gaika-extension", "gaika-country-pl", {})


def test_autonomy_permits_all_but_payment_when_open(monkeypatch):
    monkeypatch.delenv("AGENT_AUTOMATION_TARGETS", raising=False)
    monkeypatch.setattr("core.automation_grants.list_grants", lambda: [])
    assert not autonomy.permitted("owneros-autonomy-loop:0.0")
    monkeypatch.setenv("AGENT_AUTOMATION_OPEN_CALLERS", "api")
    assert autonomy.permitted("owneros-autonomy-loop:0.0")
    assert not autonomy.permitted("autonomy-pay-sonnet:0.0")
    assert not autonomy.permitted("payorch-ha-fresh:0.0")
