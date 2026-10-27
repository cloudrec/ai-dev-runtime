"""Fail-closed automation policy at agent_control's single delivery choke point."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import agent_control as ac  # noqa: E402


@pytest.fixture
def panes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    for k in ("AGENT_AUTOMATION_TARGETS", "AGENT_AUTOMATION_POLICY"):
        monkeypatch.delenv(k, raising=False)
    calls = []

    def fake_tmux(args, stdin=None):
        calls.append(list(args))
        return 0, "", ""

    def fake_pane(target):
        sess = target.split(":")[0].lstrip("%")
        sess = {"11": "payorch-ha-fresh"}.get(sess, sess)       # %11 = a pane id
        return {"target": f"{sess}:0.0", "session": sess, "alive": True}
    monkeypatch.setattr(ac, "_tmux", fake_tmux)
    monkeypatch.setattr(ac, "_pane_is_live_agent", fake_pane)
    monkeypatch.setattr(ac, "_busy_for_send", lambda t: "")
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return calls


def typed(calls):
    return [c for c in calls if c and c[0] in ("paste-buffer", "send-keys", "load-buffer")]


@pytest.mark.parametrize("fn", [ac.agent_send, ac.agent_answer])
def test_automatic_caller_with_no_allowlist_types_nothing(panes, fn):
    r = fn("vtb:0.0", "continue", idempotency_key="k1")
    assert r["refused"] == "automation_target_not_allowlisted" and not r["delivered"]
    assert typed(panes) == []


def test_star_is_never_a_wildcard(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "*")
    assert ac.agent_send("vtb:0.0", "continue", idempotency_key="k")["refused"]
    assert typed(panes) == []


@pytest.mark.parametrize("entry", ["worker-a", "worker-a:0.0"])
def test_allowlisted_session_or_target_is_typed(panes, monkeypatch, entry):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", f"other,{entry}")
    r = ac.agent_send("worker-a:0.0", "continue", idempotency_key="k")
    assert not r.get("refused") and typed(panes)


def test_allowlist_does_not_leak_to_other_sessions(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "worker-a")
    for t in ("worker-b:0.0", "worker-a-old:0.0", "bitwhite-backend-v3:0.0"):
        assert ac.agent_send(t, "x", idempotency_key=f"k-{t}")["refused"]
    assert typed(panes) == []


@pytest.mark.parametrize("target", ["payorch-ha-fresh:0.0", "payorch-ha-fresh",
                                    "PAYORCH-HA-FRESH:0.0", "payorch-ha-fresh:1.1"])
@pytest.mark.parametrize("actor", [None, "native_supervisor", "cto_bridge",
                                   "api:hmac/chatgpt-mcp", "api:bearer"])
def test_payment_agent_is_hard_denied_for_everyone(panes, monkeypatch, target, actor):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "payorch-ha-fresh,PAYORCH-HA-FRESH")
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "off")
    r = ac.agent_send(target, "continue", idempotency_key=f"k-{target}-{actor}", actor=actor)
    assert r["refused"] == "hard_denied_target"
    r = ac.agent_answer(target, "1", idempotency_key=f"a-{target}-{actor}", actor=actor)
    assert r["refused"] == "hard_denied_target"
    assert typed(panes) == []


def test_api_callers_are_held_to_the_allowlist_too(panes):
    """HMAC auth proves the shared runtime token, not the owner (see owner proof tests)."""
    r = ac.agent_send("vtb:0.0", "status?", idempotency_key="k", actor="api:hmac/chatgpt-mcp")
    assert r["refused"] == "automation_target_not_allowlisted" and typed(panes) == []


def test_policy_off_is_an_explicit_escape_hatch(panes, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "off")
    assert not ac.agent_send("vtb:0.0", "x", idempotency_key="k").get("refused")


def test_refusal_is_audited_and_not_recorded_as_a_delivery(panes, tmp_path):
    ac.agent_send("vtb:0.0", "continue", idempotency_key="k-audit")
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "automation_target_not_allowlisted" in audit
    assert ac._seen_delivery("k-audit") is None             # a later allowed retry may send


def test_agent_control_is_the_only_live_module_that_types_into_panes():
    """The policy is only as strong as the choke point: no live module may type into a
    pane except through agent_control._deliver. direct_pane_control is a library used only
    by tests; it must stay unimported by live code."""
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    typing_re = re.compile(r"""["'](send-keys|paste-buffer)["']""")
    offenders, importers = [], []
    for d in ("core", "api", "tools", "hooks"):
        for f in (root / d).rglob("*.py"):
            rel = str(f.relative_to(root))
            src = f.read_text(errors="replace")
            if typing_re.search(src) and rel not in ("core/agent_control.py",
                                                     "core/direct_pane_control.py",
                                                     "tools/cdp_composer.py"):
                offenders.append(rel)
            if re.search(r"\bdirect_pane_control\b", src) and rel != "core/direct_pane_control.py":
                importers.append(rel)
    assert offenders == [] and importers == []


def keys_sent(calls):
    return [c for c in calls if c and c[0] == "send-keys"]


def test_control_keys_follow_the_same_policy(panes, monkeypatch):
    assert ac.send_control_keys("vtb:0.0", ["Enter"])["refused"] == \
        "automation_target_not_allowlisted"
    assert ac.send_control_keys("payorch-ha-fresh:0.0", ["Enter"],
                                actor="api:hmac/x")["refused"] == "hard_denied_target"
    assert keys_sent(panes) == []
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "worker-a")
    r = ac.send_control_keys("worker-a:0.0", ["1", "Enter"])
    assert r["sent"] and keys_sent(panes) == [["send-keys", "-t", "worker-a:0.0", "1", "Enter"]]


@pytest.mark.parametrize("bad", ["continue", "rm -rf /", "C-c", "12", "", "Enter Enter"])
def test_control_keys_cannot_type_text(panes, monkeypatch, bad):
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "off")
    with pytest.raises(ac.AgentControlError):
        ac.send_control_keys("worker-a:0.0", [bad])
    assert keys_sent(panes) == []


def test_watchdog_enter_and_session_recovery_choice_go_through_the_policy(panes):
    from core import agent_continuation_watchdog as wd
    from core import session_recovery as sr
    src_wd = pathlib.Path(wd.__file__).read_text()
    assert "send_control_keys(target, [\"Enter\"]" in src_wd
    res = sr.choose_summary_if_offered("vtb:0.0",
                                       capture_fn=lambda: "  2. Resume from summary")
    assert keys_sent(panes) == []            # refused: vtb is not allowlisted
    assert res["offered"]
