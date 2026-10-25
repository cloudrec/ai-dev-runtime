"""tools/native_cto_verify.py + the canary provider drop-in / key script: gate reporting on the
SERVICE env, no secret in any output, key check without generation, delivery attribution."""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sqlite3
import subprocess
import urllib.error

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ncv", ROOT / "tools" / "native_cto_verify.py")
ncv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ncv)

SECRET = "sk-test-NEVER-PRINT-0123456789abcdef"
BASE = {"NATIVE_OWNER_OS_ENABLED": "1", "NATIVE_GOALS_ENABLED": "1",
        "NATIVE_OWNER_OS_AGENTS": "aes-canary"}
CFG = {"OWNER_OS_CTO_PROVIDER": "openai", "OWNER_OS_CTO_MODEL": "some-model",
       "OWNER_OS_CTO_DELIVERY_MODE": "native_controller"}
LEDGER = {"cursor": 10, "bus_head": 10, "decisions": {}, "outbox": {}, "conversations": 0,
          "out_of_scope_decisions": [], "denied_outbox_pending": []}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.mark.parametrize("env,gate", [
    (BASE, "cto_provider_disabled"),
    ({**BASE, **CFG}, "missing_secret:OPENAI_API_KEY"),
    ({**BASE, **CFG, "OPENAI_API_KEY": SECRET}, ""),
])
def test_gate_is_computed_from_the_given_service_env(env, gate):
    cfg, provider = ncv.config_report(env)
    assert cfg["gate"] == gate and cfg["provider_ready"] is (gate == "")
    assert cfg["OPENAI_API_KEY_present"] is ("OPENAI_API_KEY" in env)
    assert SECRET not in json.dumps(cfg) and SECRET not in repr(provider)


def test_shell_env_does_not_leak_into_the_report(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OWNER_OS_CTO_PROVIDER", "openai")
    cfg, _ = ncv.config_report(BASE)
    assert cfg["gate"] == "cto_provider_disabled" and not cfg["OPENAI_API_KEY_present"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _provider():
    _, p = ncv.config_report({**BASE, **CFG, "OPENAI_API_KEY": SECRET})
    return p


def test_key_check_is_a_get_on_the_model_and_reports_ok():
    seen = []

    def opener(req, timeout):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        return _Resp(json.dumps({"id": "some-model"}).encode())
    assert ncv.key_check(_provider(), opener=opener) == {"ok": True, "status": 200}
    assert seen == [("GET", "https://api.openai.com/v1/models/some-model",
                     f"Bearer {SECRET}")]


@pytest.mark.parametrize("exc,status", [
    (urllib.error.HTTPError("u", 401, "unauthorized", {}, None), 401),
    (OSError(f"boom {SECRET}"), None),
])
def test_key_check_failure_never_echoes_the_key(exc, status):
    def opener(req, timeout):
        raise exc
    r = ncv.key_check(_provider(), opener=opener)
    assert r["ok"] is False and r["status"] == status and SECRET not in json.dumps(r)


def _cfg(env):
    return ncv.config_report(env)[0]


def test_verdict_missing_secret_passes_with_only_waiting_provider():
    led = {**LEDGER, "decisions": {"waiting_provider": 2}}
    assert ncv.verdict(_cfg({**BASE, **CFG}), led, expect="missing_secret",
                       targets=["aes-canary:0.0"]) == []


@pytest.mark.parametrize("env,ledger,targets,expect,needle", [
    ({**CFG}, LEDGER, [], "missing_secret", "NATIVE_OWNER_OS_ENABLED!=1"),
    ({**BASE, **CFG, "NATIVE_OWNER_OS_AGENTS": ""}, LEDGER, [], "missing_secret",
     "not a canary"),
    ({**BASE, **CFG, "OWNER_OS_CTO_DELIVERY_MODE": "agent_control"}, LEDGER, [],
     "missing_secret", "agent_control"),
    ({**BASE, **CFG}, LEDGER, [], "ready", "gate="),
    ({**BASE, **CFG}, {**LEDGER, "cursor": None}, [], "missing_secret", "cursor missing"),
    ({**BASE, **CFG}, {**LEDGER, "decisions": {"decided": 1}}, [], "missing_secret",
     "other than waiting_provider"),
    ({**BASE, **CFG}, {**LEDGER, "out_of_scope_decisions": ["bitwhite"]}, [],
     "missing_secret", "out-of-scope"),
    ({**BASE, **CFG}, {**LEDGER, "denied_outbox_pending": ["payorch-ha-fresh"]}, [],
     "missing_secret", "denied agents"),
    ({**BASE, **CFG}, LEDGER, ["aes-canary:0.0", "payorch-ha-fresh:0.0"], "missing_secret",
     "deliveries outside scope"),
])
def test_verdict_failures(env, ledger, targets, expect, needle):
    fails = ncv.verdict(_cfg(env), ledger, expect=expect, targets=targets)
    assert any(needle in f for f in fails), fails


def test_verdict_ready_requires_a_passing_key_check():
    cfg = _cfg({**BASE, **CFG, "OPENAI_API_KEY": SECRET})
    assert ncv.verdict(cfg, LEDGER, expect="ready", key={"ok": True}) == []
    assert ncv.verdict(cfg, LEDGER, expect="ready", key={"ok": False, "status": 401})


def test_only_this_stacks_deliveries_are_judged(tmp_path):
    db = tmp_path / "ac.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE deliveries (idempotency_key TEXT, target TEXT)")
    c.executemany("INSERT INTO deliveries VALUES (?,?)", [
        ("old", "x:0.0"), ("goal:aes-canary:h", "aes-canary:0.0"),
        ("cto:cp:5", "aes-canary:0.0"), ("nativesup:7", "bitwhite:0.0"), ("adhoc", "me:0.0")])
    c.commit()
    c.close()
    assert ncv.deliveries_since(str(db), 1) == {
        "own": ["aes-canary:0.0", "aes-canary:0.0"], "other": {"nativesup": 1,
                                                              "(unprefixed)": 1}}


def test_ledger_report_flags_out_of_scope_and_denied_pending():
    from core import cto_bridge as cb
    from core.control_plane.api import _c
    conn, _ = _c(None)
    cb._schema(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO cto_decision (event_key,agent,status) VALUES "
                 "('a','aes-canary:0.0','waiting_provider'),('b','bitwhite:0.0','decided')")
    conn.execute("INSERT INTO native_outbox (msg_key,to_agent,state) VALUES "
                 "('m','payorch-ha-fresh:0.0','pending')")
    conn.commit()
    rep = ncv.ledger_report(conn, {"aes-canary"})
    conn.close()
    assert rep["out_of_scope_decisions"] == ["bitwhite:0.0"]
    assert rep["denied_outbox_pending"] == ["payorch-ha-fresh:0.0"]
    assert rep["decisions"] == {"waiting_provider": 1, "decided": 1}


def test_provider_dropin_is_secret_free_and_overrides_the_96_canary():
    text = (ROOT / "deploy" / "97-native-cto-provider.conf").read_text()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert "OPENAI_API_KEY" not in "\n".join(lines)
    assert "EnvironmentFile=-/etc/ai-runtime/cto-openai.env" in lines
    assert "Environment=OWNER_OS_CTO_PROVIDER=openai" in lines
    assert "Environment=OWNER_OS_CTO_DELIVERY_MODE=native_controller" in lines
    assert not any("NATIVE_OWNER_OS_AGENTS" in ln for ln in lines)   # scope stays in 96
    assert sorted(["96-native-owner-os-canary.conf", "97-native-cto-provider.conf"])[-1] \
        .startswith("97")


def test_key_script_parses_and_never_puts_the_key_in_argv():
    sh = ROOT / "tools" / "native_cto_key.sh"
    subprocess.run(["bash", "-n", str(sh)], check=True)
    src = sh.read_text()
    assert "read -rsp" in src and "unset key" in src
    assert "chmod 0600" in src and "install -d -m 0700" in src
    # the only use of the key value is the printf builtin redirect into the 0600 temp file
    uses = [ln for ln in src.splitlines() if '"$key"' in ln and not ln.lstrip().startswith("#")]
    assert len(uses) == 2 and any("printf" in u for u in uses) and any("=~" in u for u in uses)
