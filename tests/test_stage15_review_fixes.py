"""Fixes after the Fable review of e94a32e..2d762c4 (N1-N4) and the audit's open items.

Offline: tmp DBs, fake executables, no network, no live file.
"""
from __future__ import annotations

import pytest

from core import agent_auth_mode as aam
from core.fullcycle import engine as fe
from core.fullcycle import gates
from tests.test_agent_creation_policy import mutations, tmux, wt  # noqa: F401


# ── N4: a failing wake bridge leaves nothing open on the shared connection ───────
@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    c, _ = fe.connect()
    c.execute("CREATE TABLE bridge_work (x)")
    c.commit()
    yield c
    c.close()


def _fake_bridge(monkeypatch, conn):
    from core import wake_bridge as wb
    monkeypatch.setattr(wb, "_enabled", lambda: (True, ""))
    monkeypatch.setattr(wb, "is_significant", lambda **k: {"significant": True})
    monkeypatch.setattr(wb, "should_wake", lambda **k: {"decision": "wake"})

    def half_done(d, **k):
        conn.execute("INSERT INTO bridge_work VALUES (1)")
        raise RuntimeError("injected bridge failure")
    monkeypatch.setattr(wb, "record", half_done)


def test_n4_a_bridge_failure_after_dml_does_not_poison_the_next_tx(conn, monkeypatch):
    _fake_bridge(monkeypatch, conn)
    fe._offer_to_wake_bridge(1, "t", "high", False, project_id="p", agent_id="a",
                             correlation_id="c", conn=conn)
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM bridge_work").fetchone()[0] == 0
    with fe._tx(conn):
        fe._audit(conn, action="x", review_id=None, actor="t", in_tx=True)


def test_n4_the_committed_event_survives_the_rollback(conn, monkeypatch):
    from core.control_plane import api as cp
    eid = cp.append_event("t", "fullcycle.test", conn=conn)
    _fake_bridge(monkeypatch, conn)
    fe._offer_to_wake_bridge(eid, "t", "high", False, project_id="p", agent_id="a",
                             correlation_id="c", conn=conn)
    conn.close()
    fresh, _ = fe.connect()
    try:
        assert fresh.execute("SELECT COUNT(*) FROM event WHERE id=?", (eid,)).fetchone()[0] == 1
    finally:
        fresh.close()


# ── N2: the API launcher is never a check ───────────────────────────────────────
@pytest.mark.parametrize("argv", [
    ["/usr/local/bin/claude-owner-api", "--model", "sonnet"], ["claude-owner-api"],
    ["sh", "-c", "claude-owner-api -p hi"], ["env", "X=1", "/usr/local/bin/claude-owner-api"]])
@pytest.mark.parametrize("trusted", [True, False])
def test_n2_the_launcher_is_refused_as_a_check(tmp_path, argv, trusted):
    assert gates.check_argv(argv, root=str(tmp_path), trusted=trusted) == \
        "check_api_launcher_forbidden"


def test_n2_a_configured_launcher_name_counts_too(tmp_path, monkeypatch):
    cfg = tmp_path / "modes.yaml"
    cfg.write_text("launcher: /opt/bin/my-paid-claude\n")
    monkeypatch.setenv("AGENT_AUTH_MODES_CONFIG", str(cfg))
    assert gates.check_argv(["/opt/bin/my-paid-claude"], root=str(tmp_path), trusted=True) == \
        "check_api_launcher_forbidden"


def test_n2_a_refused_check_never_reaches_the_host(tmp_path):
    ran = []

    class Host:
        def run(self, argv, **k):
            ran.append(argv)
            return 0, "", ""
    e = fe.Engine.__new__(fe.Engine)
    e.root, e.host = str(tmp_path), Host()
    (r,) = e._run_checks([["claude-owner-api"]], trusted=True)
    assert r["passed"] is False and r["output"] == "check_api_launcher_forbidden" and ran == []


def test_n2_ordinary_trusted_checks_still_run(tmp_path):
    for argv in (["python3", "-m", "pytest"], ["sh", "-c", "claude --version"], ["./verify.sh"]):
        assert gates.check_argv(argv, root=str(tmp_path), trusted=True) is None
    assert aam.DEFAULT_LAUNCHER.endswith("claude-owner-api")


# ── N1: v2 signs the request target as sent, through the real ASGI stack ────────
TOKEN = "n1-test-token"


@pytest.fixture
def signed_client(monkeypatch):
    import hashlib
    import hmac
    import time

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from api import v1
    monkeypatch.setattr(v1, "_TOKEN", TOKEN)
    v1._seen_sigs.clear()
    app = FastAPI()

    @app.api_route("/jobs/{jid}", methods=["GET", "POST"], dependencies=[Depends(v1._auth)])
    def job(jid: str):
        return {"jid": jid}
    client = TestClient(app)

    def headers(method, path, query="", body=b""):
        ts = str(int(time.time()))
        msg = v1.hmac_v2_message(ts, method, path, query, body)
        return {"X-Runtime-Timestamp": ts, "X-Runtime-Signature-Version": "2",
                "X-Runtime-Signature": hmac.new(TOKEN.encode(), msg, hashlib.sha256).hexdigest()}
    return client, headers


def test_n1_a_signed_request_passes(signed_client):
    client, headers = signed_client
    r = client.get("/jobs/x?a=1", headers=headers("GET", "/jobs/x", "a=1"))
    assert r.status_code == 200 and r.json() == {"jid": "x"}


@pytest.mark.parametrize("sent", ["/jobs/x%23y?any=thing", "/jobs/x%3Fy?any=thing"])
def test_n1_a_signature_does_not_cover_an_unsigned_query(signed_client, sent):
    client, headers = signed_client
    assert client.get(sent, headers=headers("GET", "/jobs/x")).status_code == 401


def test_n1_an_encoded_character_is_signed_as_sent(signed_client):
    client, headers = signed_client
    r = client.get("/jobs/x%23y", headers=headers("GET", "/jobs/x%23y"))
    assert r.status_code == 200 and r.json() == {"jid": "x#y"}
    assert client.get("/jobs/x%23z", headers=headers("GET", "/jobs/x%23y")).status_code == 401


def test_n1_query_and_body_stay_bound(signed_client):
    client, headers = signed_client
    assert client.get("/jobs/x?a=2", headers=headers("GET", "/jobs/x", "a=1")).status_code == 401
    assert client.post("/jobs/x", content=b"other",
                       headers=headers("POST", "/jobs/x", body=b"signed")).status_code == 401


# ── F7: a settings file can bring a credential back into a pane ─────────────────
def _settings(path, **data):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_f7_helper_and_env_block_are_named_and_values_never_returned(tmp_path):
    cfg, proj = tmp_path / "cfg", tmp_path / "proj"
    _settings(cfg / "settings.json", apiKeyHelper="/bin/print-secret-value")
    _settings(proj / ".claude" / "settings.json",
              env={"ANTHROPIC_API_KEY": "secret-value", "DISABLE_TELEMETRY": "1"})
    found = aam.settings_api_overrides(str(proj), config_dir=str(cfg))
    assert [f.rsplit(":", 1)[1] for f in found] == ["apiKeyHelper", "env.ANTHROPIC_API_KEY"]
    assert "secret-value" not in " ".join(found)


def test_f7_a_clean_or_absent_settings_file_reports_nothing(tmp_path):
    _settings(tmp_path / "settings.json", model="opus", env={"DISABLE_TELEMETRY": "1"})
    assert aam.settings_api_overrides(str(tmp_path / "none"), config_dir=str(tmp_path)) == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_f7_an_unreadable_settings_file_counts(tmp_path, content):
    (tmp_path / "settings.json").write_text(content)
    assert aam.settings_api_overrides(str(tmp_path / "none"), config_dir=str(tmp_path)) == [
        f"{tmp_path / 'settings.json'}:unreadable"]


@pytest.fixture
def overridden(wt, tmp_path, monkeypatch):  # noqa: F811
    _settings(tmp_path / "cfg" / "settings.json", apiKeyHelper="/bin/x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))


def _launch(wt):  # noqa: F811
    from core import agent_control as ac
    return ac.agent_resume(wt["kz"], session_name="gaika-kz", actor="api:hmac/chatgpt")


def test_f7_default_reports_and_still_starts(wt, tmux, overridden, tmp_path):  # noqa: F811
    assert _launch(wt)["agent_created"]
    line = [x for x in (tmp_path / "audit.jsonl").read_text().splitlines()
            if '"resumed": true' in x]
    assert line and "apiKeyHelper" in line[-1] and "/bin/x" not in line[-1]


def test_f7_enforce_refuses_before_any_tmux_mutation(wt, tmux, overridden, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_AUTH_SETTINGS_GUARD", "enforce")
    r = _launch(wt)
    assert r["refused"] == "auth_mode:settings_api_override" and not r["agent_created"]
    assert mutations(tmux["calls"]) == []


def test_f7_enforce_passes_a_clean_host(wt, tmux, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("AGENT_AUTH_SETTINGS_GUARD", "enforce")
    assert _launch(wt)["agent_created"]
