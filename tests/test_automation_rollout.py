"""Rollout safety: caller-scoped allowlist entries, payment-agent stop guard, dry-run tool."""
from __future__ import annotations

import gzip
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import agent_control as ac  # noqa: E402

KEY = b"k" * 64


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("AGENT_CONTROL_AUDIT", str(tmp_path / "audit.jsonl"))
    for k in ("AGENT_AUTOMATION_TARGETS", "AGENT_AUTOMATION_POLICY"):
        monkeypatch.delenv(k, raising=False)
    kf = tmp_path / "owner.key"
    kf.write_bytes(KEY)
    kf.chmod(0o600)
    monkeypatch.setenv("AGENT_OWNER_PROOF_KEY_FILE", str(kf))
    calls = []
    monkeypatch.setattr(ac, "_tmux", lambda args, stdin=None: calls.append(list(args))
                        or (0, "", ""))
    pane = lambda t: {"target": t if ":" in t else f"{t}:0.0",  # noqa: E731
                      "session": t.split(":")[0], "alive": True, "pid": 1, "claude_pid": 2}
    monkeypatch.setattr(ac, "_pane_is_live_agent", pane)
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [pane(t) for t in (
        "payorch-ha-fresh:0.0", "vtb:0.0")]})
    monkeypatch.setattr(ac, "_busy_for_send", lambda t: "")
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return calls


def typed(calls):
    return [c for c in calls if c and c[0] in ("paste-buffer", "send-keys", "load-buffer")]


@pytest.mark.parametrize("entries,actor,ok", [
    ("ctl@api", "api:bearer/owneros", True),
    ("ctl@api", "native_supervisor", False),
    ("ctl@api", None, False),
    ("ctl@native_supervisor", "native_supervisor", True),
    ("ctl@native_supervisor", "api:hmac", False),
    ("ctl@unattributed", None, True),
    ("ctl@unattributed", "native_goals", False),
    ("ctl", "native_supervisor", True),          # bare entry = any caller (documented)
    ("ctl:0.0@api", "api:hmac", True),
    ("*@api", "api:hmac", False),
    ("other@api", "api:hmac", False),
])
def test_caller_scoped_entries(env, monkeypatch, entries, actor, ok):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", entries)
    r = ac.agent_send("ctl:0.0", "x", idempotency_key=f"{entries}-{actor}", actor=actor)
    assert (not r.get("refused")) is ok, r
    assert bool(typed(env)) is ok


def test_controller_entry_for_api_does_not_open_it_to_the_native_supervisor(env, monkeypatch):
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "owner-os-supervisor-clean@api")
    assert not ac.agent_send("owner-os-supervisor-clean:0.0", "event", idempotency_key="a",
                             actor="api:bearer/owneros").get("refused")
    n = len(typed(env))
    r = ac.agent_send("owner-os-supervisor-clean:0.0", "continue", idempotency_key="b",
                      actor="native_supervisor")
    assert r["refused"] == "automation_target_not_allowlisted" and len(typed(env)) == n


def kills(calls):
    return [c for c in calls if c and c[0] == "kill-pane"]


def test_payment_agent_is_never_stopped_without_owner_proof(env):
    r = ac.agent_stop("payorch-ha-fresh:0.0", confirm=True, idempotency_key="s1")
    assert r["refused"] == "hard_denied_target" and not r["stopped"] and kills(env) == []
    bad = ac.sign_owner_proof(b"x" * 64, "agent_stop", "payorch-ha-fresh:0.0", "")
    r = ac.agent_stop("payorch-ha-fresh:0.0", confirm=True, idempotency_key="s2",
                      owner_proof=bad)
    assert r["refused"] == "owner_proof_bad_signature" and kills(env) == []
    wrong_action = ac.sign_owner_proof(KEY, "agent_send", "payorch-ha-fresh:0.0", "")
    assert ac.agent_stop("payorch-ha-fresh:0.0", confirm=True, idempotency_key="s3",
                         owner_proof=wrong_action)["refused"] == "owner_proof_bad_signature"
    good = ac.sign_owner_proof(KEY, "agent_stop", "payorch-ha-fresh:0.0", "")
    r = ac.agent_stop("payorch-ha-fresh:0.0", confirm=True, idempotency_key="s4",
                      owner_proof=good)
    assert r["stopped"] and kills(env) == [["kill-pane", "-t", "payorch-ha-fresh:0.0"]]


def test_other_stops_keep_todays_confirm_gate(env):
    with pytest.raises(ac.AgentControlError):
        ac.agent_stop("vtb:0.0", confirm=False)
    assert ac.agent_stop("vtb:0.0", confirm=True, idempotency_key="v")["stopped"]


# ── dry-run tool ──────────────────────────────────────────────────────────────

def _tool():
    spec = importlib.util.spec_from_file_location("dry", ROOT / "tools" /
                                                  "automation_policy_dryrun.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_dryrun_replays_rotated_logs_oldest_first_and_writes_nothing(tmp_path, capsys,
                                                                     monkeypatch):
    base = tmp_path / "agent_control.jsonl"
    ev = lambda ts, **k: json.dumps({"ts": ts, "delivered": True, **k}) + "\n"  # noqa: E731
    with gzip.open(str(base) + ".2.gz", "wt") as fh:
        fh.write(ev("2026-01-01", action="agent_send", target="bitwhite:0.0",
                    actor="native_supervisor", idempotency_key="nativesup:1"))
    (tmp_path / "agent_control.jsonl.1").write_text(
        ev("2026-01-02", action="agent_send", target="owner-os-supervisor-clean:0.0",
           actor="api:bearer/owneros", idempotency_key="owneros-1"))
    base.write_text(ev("2026-01-03", action="agent_stop", target="payorch-ha-fresh:0.0",
                       stopped=True, idempotency_key="stop-1")
                    + ev("2026-01-03", action="agent_send", target="x:0.0", delivered=False))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "")      # the tool sets it in-process
    _tool().main(["--targets", "owner-os-supervisor-clean@api",
                  "--log-glob", str(base) + "*", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["window"] == ["2026-01-01", "2026-01-03"] and out["total"] == 3
    v = {(r["sender"], r["target"]): r["verdict"] for r in out["rows"]}
    assert v[("native_supervisor", "bitwhite:0.0")].startswith("BLOCKED")
    assert v[("owner_os_api_to_controller", "owner-os-supervisor-clean:0.0")] == "allowed"
    assert v[("stop", "payorch-ha-fresh:0.0")].startswith("BLOCKED hard_denied")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before   # read-only
