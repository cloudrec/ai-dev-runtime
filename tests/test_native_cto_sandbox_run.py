"""Guards of tools/native_cto_sandbox_run.py: names, isolation, listing filter, send wrapper."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sbx", ROOT / "tools" / "native_cto_sandbox_run.py")
sbx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbx)

W, C = "owneros-sandbox-worker-20261027:0.0", "owner-os-supervisor-clean:0.0"


@pytest.mark.parametrize("worker,ctl,needle", [
    (C, C, "different sessions"),
    ("owner-os-supervisor-clean:1.0", C, "different sessions"),
    ("payorch-ha-fresh:0.0", C, "hard-denied"),
    (W, "PAYORCH-HA-FRESH:0.0", "hard-denied"),
    ("aes-canary:0.0", C, "closed by the owner"),
    ("bootstrap:0.0", C, "closed by the owner"),
    ("giftcards-bot-review-opus:0.0", C, "closed by the owner"),
    ("", C, "required"),
])
def test_unsafe_names_are_refused(worker, ctl, needle):
    with pytest.raises(sbx.Refused, match=needle):
        sbx.preflight_names(worker, ctl)


def test_listing_is_filtered_to_exactly_the_two_panes():
    rows = [{"tmux_target": t, "state": "idle"} for t in
            (W, C, "bitwhite-backend-v3:0.0", "payorch-ha-fresh:0.0", "vtb:0.0")]
    out = sbx.filtered_listing({"source": "claude_agents_json", "sessions": rows}, W, C)
    assert [s["tmux_target"] for s in out["sessions"]] == [W, C]


@pytest.mark.parametrize("rows", [[C], [W], [W, W, C]])
def test_missing_or_ambiguous_panes_refuse(rows):
    with pytest.raises(sbx.Refused, match="expected exactly one"):
        sbx.filtered_listing({"sessions": [{"tmux_target": t} for t in rows]}, W, C)


def test_send_wrapper_refuses_every_other_target():
    calls = []
    send = sbx.guard_send({W, C}, lambda t, x, idempotency_key=None, **k: calls.append(t)
                          or {"submitted": True})
    send(W, "x", "k1")
    send(C, "x", "k2")
    for other in ("bitwhite-backend-v3:0.0", "payorch-ha-fresh:0.0", "vtb:0.0",
                  "mess-telegram-connector:0.0", "owneros-sandbox-worker-20261027:1.0"):
        with pytest.raises(sbx.Refused):
            send(other, "x", "k")
    assert calls == [W, C]


def test_isolation_redirects_every_store_and_drops_keys(tmp_path):
    code = (f"import importlib.util,os,json;"
            f"s=importlib.util.spec_from_file_location('s',{str(ROOT / 'tools' / 'native_cto_sandbox_run.py')!r});"
            f"m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"m.isolate({str(tmp_path)!r},{W!r},{C!r});"
            f"print(json.dumps({{k:os.environ.get(k) for k in ('CONTROL_PLANE_DB','AGENT_CONTROL_DB',"
            f"'AGENT_CONTROL_AUDIT','NATIVE_OWNER_OS_AGENTS','OWNER_OS_CTO_CONTROLLER',"
            f"'NATIVE_GOALS_ENABLED','OPENAI_API_KEY')}}))")
    env = {**os.environ, "OPENAI_API_KEY": "sk-should-vanish"}
    out = json.loads(subprocess.run([sys.executable, "-c", code], env=env, text=True,
                                    capture_output=True, check=True).stdout)
    for k in ("CONTROL_PLANE_DB", "AGENT_CONTROL_DB", "AGENT_CONTROL_AUDIT"):
        assert out[k].startswith(str(tmp_path))
    assert out["NATIVE_OWNER_OS_AGENTS"] == "owneros-sandbox-worker-20261027"
    assert out["OWNER_OS_CTO_CONTROLLER"] == C and out["NATIVE_GOALS_ENABLED"] == "0"
    assert out["OPENAI_API_KEY"] is None
