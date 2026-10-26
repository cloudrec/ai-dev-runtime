"""Native Owner OS acceptance (offline): event -> CTO decision -> outbox -> delivery -> verify,
chained through the real controller tick on an isolated ledger, plus the owner key script's
install / failed-install / rollback paths in a sandbox (no systemd, no network, no agents)."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3
import stat
import subprocess

import pytest

from core import agent_control as ac
from core import cto_bridge as cb
from core import native_owner_os as no
from core.control_plane import cto
from core.control_plane.api import _c

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ncv", ROOT / "tools" / "native_cto_verify.py")
ncv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ncv)

CANARY = "aes-canary:0.0"
SECRET = "sk-acceptance-NEVER-PRINT-0123456789"


def listing(*rows):
    return {"source": "claude_agents_json", "sessions": [
        {"name": n, "kind": "interactive", "cwd": f"/w/{n}", "session_id": n, "state": st,
         "pid": i + 1, "tmux_target": t} for i, (n, t, st) in enumerate(rows)]}


IDLE = listing(("aes-canary", CANARY, "idle"))


class P(cb.Provider):
    name, model = "fake", "m"

    def __init__(self, fail=False, owner=False):
        self.calls, self.fail, self.owner = [], fail, owner

    def create_conversation(self, project):
        return f"conv_{project}"

    def decide(self, ctx, conversation_id=None):
        self.calls.append((ctx["event_type"], ctx["agent"], conversation_id))
        if self.fail:
            raise cb.ProviderError("request_failed:URLError")
        return {"decision": "split the migration", "rationale": "r",
                "owner_needed": self.owner, "message_to_agent": "split the migration"}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    for k in ("NATIVE_GOALS_ENABLED", "OWNER_OS_CTO_PROVIDER", "OWNER_OS_CTO_MODEL",
              "OPENAI_API_KEY", "OWNER_OS_CTO_DENY_AGENTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NATIVE_OWNER_OS_AGENTS", "aes-canary")
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "agent_control")


@pytest.fixture
def sends(monkeypatch):
    log = []

    def fake_send(target, text, idempotency_key=None, **kw):
        log.append((target, text, idempotency_key))
        return {"submitted": True, "delivered": True, "idempotency_key": idempotency_key}
    monkeypatch.setattr(ac, "agent_send", fake_send)
    return log


def emit(type_, agent=CANARY, owner=False):
    return cto.emit("work_evidence", type_, project_id="owner-os", agent_id=agent,
                    severity="info", owner_action_required=owner,
                    payload={"question": "two agents edit the same schema"}, push=False,
                    dedup_window_secs=0)["event_id"]


def q(sql):
    conn, _ = _c(None)
    cb._schema(conn)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def tick(provider, lst=IDLE):
    return no.run_once(listing=lst, provider=provider)


def test_event_to_decision_to_delivery_to_verify_exactly_once(sends):
    p = P()
    tick(p)                                            # cursor -> bus head
    eid = emit("architecture_conflict")
    out = tick(p)
    assert p.calls == [("architecture_conflict", CANARY, "conv_owner-os")]
    assert [(d["msg_key"], d["result"]) for d in out["delivered"]] == [(f"cto:cp:{eid}", "sent")]
    assert sends == [(CANARY, cb.ADVICE_PREFIX + "split the migration", f"cto:cp:{eid}")]
    assert q("SELECT status FROM cto_decision") == [("decided",)]
    assert q("SELECT state FROM native_outbox") == [("sent",)]
    # idempotency: more ticks and a full cursor rewind change nothing
    tick(p)
    cto.set_cursor(no.CONSUMER, 0)
    tick(p)
    assert len(p.calls) == 1 and len(sends) == 1
    # read-back through the verifier, as the post-key check would see it
    env = {"NATIVE_OWNER_OS_ENABLED": "1", "NATIVE_GOALS_ENABLED": "1",
           "NATIVE_OWNER_OS_AGENTS": "aes-canary", "OWNER_OS_CTO_PROVIDER": "openai",
           "OWNER_OS_CTO_MODEL": "m", "OWNER_OS_CTO_DELIVERY_MODE": "native_controller",
           "OPENAI_API_KEY": SECRET}
    cfg, _ = ncv.config_report(env)
    conn, _ = _c(None)
    led = ncv.ledger_report(conn, {"aes-canary"})
    conn.close()
    assert led["decisions"] == {"decided": 1} and led["outbox"] == {"sent": 1}
    assert ncv.verdict(cfg, led, expect="ready", key={"ok": True},
                       targets=[t for t, _, _ in sends]) == []


def test_native_controller_mode_never_types_and_hands_over_once(sends, monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "native_controller")
    p = P()
    tick(p)
    eid = emit("repeated_failure")
    assert tick(p)["delivered"] == [] and sends == []
    msgs = cb.pending_messages()
    assert [(m["msg_key"], m["to_agent"]) for m in msgs] == [(f"cto:cp:{eid}", CANARY)]
    assert cb.mark_sent(msgs[0]["msg_key"], "sendmessage:aes-canary") is True
    assert cb.mark_sent(msgs[0]["msg_key"], "sendmessage:aes-canary") is False
    assert cb.pending_messages() == []


def test_off_mode_delivers_nothing(sends, monkeypatch):
    monkeypatch.setenv("OWNER_OS_CTO_DELIVERY_MODE", "off")
    p = P()
    tick(p)
    emit("repeated_failure")
    tick(p)
    assert sends == [] and q("SELECT state FROM native_outbox") == [("pending",)]


def test_busy_target_waits_then_gets_exactly_one_delivery(sends):
    p = P()
    tick(p)
    emit("milestone_review")
    out = tick(p, listing(("aes-canary", CANARY, "busy")))
    assert out["delivered"][0]["result"] == "pending_busy" and sends == []
    tick(p)
    tick(p)
    assert len(sends) == 1


def test_out_of_scope_agents_get_no_decision_and_no_delivery(sends):
    p = P()
    lst = listing(("aes-canary", CANARY, "idle"), ("bitwhite", "bitwhite-backend-v3:0.0", "idle"))
    tick(p, lst)
    emit("architecture_conflict", agent="bitwhite-backend-v3:0.0")
    tick(p, lst)
    assert p.calls == [] and q("SELECT COUNT(*) FROM cto_decision") == [(0,)]
    # defence in depth: even a queued message for an out-of-scope agent is not delivered
    conn, _ = _c(None)
    cb._schema(conn)
    conn.execute("INSERT INTO native_outbox (msg_key,to_agent,message,created_at) VALUES "
                 "('cto:x','bitwhite-backend-v3:0.0','m','t')")
    conn.commit()
    conn.close()
    out = tick(p, lst)
    assert out["delivered"] == [{"msg_key": "cto:x", "result": "pending_out_of_scope"}]
    assert sends == []


def test_payorch_is_denied_even_when_put_in_scope(sends, monkeypatch):
    monkeypatch.setenv("NATIVE_OWNER_OS_AGENTS", "aes-canary,payorch-ha-fresh")
    lst = listing(("payorch", "payorch-ha-fresh:0.0", "idle"))
    p = P()
    tick(p, lst)
    emit("architecture_conflict", agent="payorch-ha-fresh:0.0")
    tick(p, lst)
    conn, _ = _c(None)
    cb._schema(conn)
    conn.execute("INSERT INTO native_outbox (msg_key,to_agent,message,created_at) VALUES "
                 "('cto:y','payorch-ha-fresh:0.0','m','t')")
    conn.commit()
    conn.close()
    tick(p, lst)
    assert sends == []
    assert ("pending",) not in q("SELECT state FROM native_outbox "
                                 "WHERE to_agent LIKE 'payorch-ha-fresh%'")


def test_owner_needed_is_never_auto_answered(sends):
    p = P(owner=True)
    tick(p)
    emit("repeated_failure")
    emit("agent_waiting_input", owner=True)
    tick(p)
    assert sends == [] and q("SELECT COUNT(*) FROM native_outbox") == [(0,)]
    owner = [e for e in cto.cto_brief_since("acceptance-reader", limit=50)["events"]
             if e.get("type") == no.OWNER_NEEDED_TYPE]
    assert len(owner) == 1                     # one escalation for the provider's verdict


def test_provider_outage_retries_to_cap_then_stops_without_delivery(sends):
    p = P(fail=True)
    tick(p)
    emit("repeated_failure")
    for _ in range(6):
        tick(p)
    assert q("SELECT status,attempts FROM cto_decision") == [("provider_failed", cb.MAX_ATTEMPTS)]
    assert len(p.calls) == cb.MAX_ATTEMPTS and sends == []


def test_missing_secret_waits_then_one_decision_when_the_key_arrives(sends):
    no.run_once(listing=IDLE, gate="missing_secret:OPENAI_API_KEY")
    emit("shared_core_change")
    no.run_once(listing=IDLE, gate="missing_secret:OPENAI_API_KEY")
    assert q("SELECT status FROM cto_decision") == [("waiting_provider",)] and sends == []
    p = P()
    tick(p)
    tick(p)
    assert q("SELECT status FROM cto_decision") == [("decided",)]
    assert len(p.calls) == 1 and len(sends) == 1


def test_watchdog_flags_a_stalled_controller_only_when_events_are_unread():
    cfg, _ = ncv.config_report({"NATIVE_OWNER_OS_ENABLED": "1", "NATIVE_GOALS_ENABLED": "1",
                                "NATIVE_OWNER_OS_AGENTS": "aes-canary"})
    base = {"decisions": {}, "outbox": {}, "conversations": 0, "out_of_scope_decisions": [],
            "denied_outbox_pending": []}
    ok = ncv.verdict(cfg, {**base, "cursor": 5, "bus_head": 5, "cursor_age_secs": 99999},
                     expect="disabled")
    lagging_fresh = ncv.verdict(cfg, {**base, "cursor": 4, "bus_head": 5,
                                      "cursor_age_secs": 30}, expect="disabled")
    stalled = ncv.verdict(cfg, {**base, "cursor": 4, "bus_head": 5,
                                "cursor_age_secs": ncv.STALL_SECS + 1}, expect="disabled")
    assert ok == [] and lagging_fresh == []
    assert any("controller stalled" in f for f in stalled)


# ── owner key script in a sandbox ─────────────────────────────────────────────

SCRIPT = ROOT / "tools" / "native_cto_key.sh"


@pytest.fixture
def sandbox(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("script requires root")
    d = {k: tmp_path / k for k in ("dropins", "keys", "runtime", "bin")}
    for k in ("dropins", "runtime", "bin"):
        d[k].mkdir()
    (d["dropins"] / "96-native-owner-os-canary.conf").write_text("[Service]\n")
    c = sqlite3.connect(d["runtime"] / "agent_control.db")
    c.execute("CREATE TABLE deliveries (idempotency_key TEXT, target TEXT)")
    c.execute("INSERT INTO deliveries VALUES ('x','y')")
    c.commit()
    c.close()
    log = tmp_path / "calls.log"
    stubs = {
        "systemctl": f'echo "systemctl $*" >> {log}',
        "curl": "printf 200",
        "pyverify": f'echo "verify $*" >> {log}; '
                    'case "$*" in *ready*) [ -z "$FAIL_READY" ] || exit 1;; esac',
    }
    for name, body in stubs.items():
        f = d["bin"] / name
        f.write_text(f"#!/usr/bin/env bash\n{body}\n")
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{d['bin']}:{os.environ['PATH']}",
           "NATIVE_CTO_RUNTIME": str(d["runtime"]), "NATIVE_CTO_PY": str(d["bin"] / "pyverify"),
           "NATIVE_CTO_DROPIN_DIR": str(d["dropins"]), "NATIVE_CTO_KEY_DIR": str(d["keys"]),
           "NATIVE_CTO_HEALTH": "http://stub/health"}
    d.update(log=log, env=env)
    return d


def run(sb, mode, stdin="", **extra):
    return subprocess.run(["bash", str(SCRIPT), mode], input=stdin, text=True,
                          capture_output=True, env={**sb["env"], **extra}, timeout=60)


def calls(sb):
    return sb["log"].read_text().splitlines() if sb["log"].exists() else []


def test_preconfig_installs_only_the_provider_dropin_and_expects_missing_secret(sandbox):
    r = run(sandbox, "preconfig")
    assert r.returncode == 0, r.stderr
    assert (sandbox["dropins"] / "97-native-cto-provider.conf").read_text() == \
        (ROOT / "deploy" / "97-native-cto-provider.conf").read_text()
    assert not sandbox["keys"].exists()
    c = calls(sandbox)
    assert "systemctl restart ai-runtime.service" in c
    assert c[-1].endswith("--expect missing_secret --since-rowid 1")


def test_install_writes_the_key_root_only_and_never_in_argv_or_output(sandbox):
    r = run(sandbox, "install", stdin=SECRET + "\n")
    assert r.returncode == 0, r.stderr
    kf = sandbox["keys"] / "cto-openai.env"
    assert kf.read_text() == f"OPENAI_API_KEY={SECRET}\n"
    assert stat.S_IMODE(kf.stat().st_mode) == 0o600
    assert stat.S_IMODE(sandbox["keys"].stat().st_mode) == 0o700
    assert SECRET not in r.stdout + r.stderr + sandbox["log"].read_text()
    assert calls(sandbox)[-1].endswith("--expect ready --key-check --since-rowid 1")
    assert not list(sandbox["keys"].glob(".cto-openai.*"))       # no temp file left


def test_failed_install_verification_rolls_the_key_back(sandbox):
    r = run(sandbox, "install", stdin=SECRET + "\n", FAIL_READY="1")
    assert r.returncode == 1
    assert not (sandbox["keys"] / "cto-openai.env").exists()
    assert (sandbox["dropins"] / "97-native-cto-provider.conf").exists()
    c = calls(sandbox)
    assert c.count("systemctl restart ai-runtime.service") == 2
    assert "--expect missing_secret" in c[-1]


def test_malformed_key_is_rejected_before_anything_changes(sandbox):
    r = run(sandbox, "install", stdin="sk bad key; rm -rf /\n")
    assert r.returncode == 1 and "rejected" in r.stderr
    assert not sandbox["keys"].exists() and calls(sandbox) == []
    assert not (sandbox["dropins"] / "97-native-cto-provider.conf").exists()


def test_remove_rolls_back_key_and_config_and_expects_disabled(sandbox):
    assert run(sandbox, "install", stdin=SECRET + "\n").returncode == 0
    r = run(sandbox, "remove")
    assert r.returncode == 0, r.stderr
    assert not (sandbox["keys"] / "cto-openai.env").exists()
    assert not (sandbox["dropins"] / "97-native-cto-provider.conf").exists()
    assert (sandbox["dropins"] / "96-native-owner-os-canary.conf").exists()
    assert calls(sandbox)[-1].endswith("--expect disabled --since-rowid 1")


def test_refuses_without_the_96_canary_dropin(sandbox):
    (sandbox["dropins"] / "96-native-owner-os-canary.conf").unlink()
    r = run(sandbox, "preconfig")
    assert r.returncode == 1 and "refusing" in r.stderr and calls(sandbox) == []


def test_session_mode_installs_the_keyless_provider_and_expects_ready(sandbox):
    (sandbox["dropins"] / "97-native-cto-provider.conf").write_text("[Service]\n")
    r = run(sandbox, "session")
    assert r.returncode == 0, r.stderr
    assert not (sandbox["dropins"] / "97-native-cto-provider.conf").exists()
    assert (sandbox["dropins"] / "97-native-cto-claude-session.conf").read_text() == \
        (ROOT / "deploy" / "97-native-cto-claude-session.conf").read_text()
    assert not sandbox["keys"].exists()
    assert calls(sandbox)[-1].endswith("--expect ready --since-rowid 1")
    assert run(sandbox, "remove").returncode == 0
    assert not (sandbox["dropins"] / "97-native-cto-claude-session.conf").exists()
