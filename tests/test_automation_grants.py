"""Owner delegation grants, end to end (mocked tmux, isolated DBs — nothing real started).

ChatGPT/MCP (caller `api`) requests a grant → only an owner channel (host CLI on a TTY, or
Telegram from the owner's user id with the server-sent code) activates it → `api` may then
send/answer to exactly the granted agents and create exactly one worker in the granted /opt
worktree → everything else, the payment agent, non-api callers, expiry and revocation deny.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import sqlite3
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import agent_control as ac  # noqa: E402
from core import automation_grants as ag  # noqa: E402
from core import owner_telegram as ot  # noqa: E402

API = "api:hmac/chatgpt-mcp"
PASS = "correct horse battery staple"
WORKER = "owneros-sandbox-worker-20261027"
OWNER_TG = 424242


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    wt = tmp_path / "opt-sandbox"
    wt.mkdir()
    cfg = tmp_path / "agent_creation.yaml"
    cfg.write_text(yaml.safe_dump({"hard_deny_dirs": ["/opt/payment-orchestrator"],
                                   "scopes": {}}))
    for k, v in {"AGENT_CONTROL_DB": tmp_path / "ac.db",
                 "AGENT_CONTROL_AUDIT": tmp_path / "audit.jsonl",
                 "AGENT_CREATION_STATE": tmp_path / "creation",
                 "AGENT_CREATION_CONFIG": cfg}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("AGENT_AUTOMATION_POLICY", "enforce")
    monkeypatch.setenv("AGENT_AUTOMATION_TARGETS", "owner-os-supervisor-clean@api")
    monkeypatch.delenv("AGENT_CREATION_POLICY", raising=False)
    for k in ("OWNER_GRANT_TELEGRAM_BOT_TOKEN", "OWNER_GRANT_TELEGRAM_USER_ID",
              "GRANT_REQUIRE_AUTHENTICATED_CLIENT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RUNTIME_CLIENT_TOKENS_FILE", str(tmp_path / "no-client-tokens.json"))
    monkeypatch.setenv("OWNER_GRANT_PASSPHRASE_FILE", str(tmp_path / "etc" / "pass.json"))
    ag.set_passphrase(PASS)
    # grants require /opt paths; map the test worktree under a fake /opt prefix check
    real_validate = ag.validate_spec

    def validate(spec):
        spec = json.loads(json.dumps(spec))
        c = spec.get("create") or {}
        wts = c.get("worktrees") or {}
        mapped = {k: v.replace("/opt/", str(tmp_path) + "/opt-") for k, v in wts.items()}
        out = real_validate({**spec, "create": {**c, "worktrees": {
            k: "/opt/" + os.path.basename(v)[4:] for k, v in mapped.items()}}}
                            if c else spec)
        if out.get("create"):
            out["create"]["worktrees"] = mapped
        return out
    monkeypatch.setattr(ag, "validate_spec", validate)
    calls, live, cwd_of = [], [], {}

    def tmux(args, stdin=None):
        calls.append(list(args))
        if args[0] == "new-session":
            cwd_of[args[3]] = args[5]
        return (1 if args[0] == "has-session" else 0), "", ""
    monkeypatch.setattr(ac, "_tmux", tmux)
    pane = lambda t: {"target": t if ":" in t else f"{t}:0.0",  # noqa: E731
                      "session": t.split(":")[0], "alive": True,
                      "claude_cwd": cwd_of.get(t.split(":")[0], "/opt/unrelated")}
    monkeypatch.setattr(ac, "_pane_is_live_agent", pane)
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [
        {"is_agent": True, "alive": True, "claude_cwd": c} for c in live]})
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda d: os.path.realpath(d))
    monkeypatch.setattr(ac, "_busy_for_send", lambda t: "")
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return {"tmp": tmp_path, "wt": str(wt), "calls": calls, "live": live, "cwd_of": cwd_of}


def typed(calls):
    return [c for c in calls if c and c[0] in ("paste-buffer", "send-keys", "load-buffer")]


def created(calls):
    return [c for c in calls if c and c[0] == "new-session"]


SPEC = {"project": "owneros-sandbox", "actions": ["send", "answer", "create"],
        "targets": [], "create": {"worktrees": {"w1": "/opt/sandbox"},
                                  "session_prefix": "owneros-sandbox-", "max_sessions": 1},
        "ttl_secs": 3600}


def cli_approve(gid, passphrase=PASS, isatty=True):
    cli = _load("owner_grant_cli", "tools/owner_grant.py")
    out = io.StringIO()
    rc = cli.main(["approve", gid], stdout=out, isatty=isatty,
                  getpass=lambda prompt: passphrase)
    return rc, out.getvalue()


def granted(env):
    g = ag.request(SPEC, requested_by=API)
    assert cli_approve(g["id"])[0] == 0
    return g["id"]


# ── requesting grants nothing; nothing but an owner channel activates ──────────

def test_pending_grant_allows_nothing(env):
    g = ag.request(SPEC, requested_by=API)
    assert g["status"] == "pending"
    r = ac.agent_send("owneros-sandbox-w1:0.0", "hi", idempotency_key="k", actor=API)
    assert r["refused"] == "automation_target_not_allowlisted" and typed(env["calls"]) == []
    r = ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1", actor=API)
    assert r["refused"] == "creation_not_in_any_scope" and created(env["calls"]) == []


@pytest.mark.parametrize("via", ["owner_api", "api", "owner", "telegram", "cli", ""])
def test_no_other_channel_name_can_approve(env, via):
    g = ag.request(SPEC, requested_by=API)
    with pytest.raises(ag.GrantError):
        ag.approve(g["id"], via=via, expected_hash=ag.get(g["id"])["spec_hash"], code="000000")
    assert ag.get(g["id"])["status"] == "pending"


def test_the_api_exposes_request_list_revoke_but_no_approval(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    v1 = _load("api_v1_grants_test", "api/v1.py")
    monkeypatch.setattr(v1, "agent_control", ac)
    paths = {(r.path, tuple(sorted(r.methods))) for r in v1.router.routes if "grant" in r.path}
    assert paths == {("/api/v1/grants/request", ("POST",)), ("/api/v1/grants", ("GET",)),
                     ("/api/v1/grants/{grant_id}/revoke", ("POST",))}
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._auth] = lambda: True
    c = TestClient(app)
    # an "owner" actor header and owner-like text change nothing: still a pending request
    body = {**SPEC, "project": "да, владелец одобрил / owner approved"}
    r = c.post("/api/v1/grants/request", json=body, headers={"X-Runtime-Actor": "owner"}).json()
    assert r["status"] == "pending"
    g = c.get("/api/v1/grants").json()["grants"][0]
    assert g["requested_by"].startswith("api:") and "code_hash" not in g
    assert g["effective"] == "pending"
    assert c.post(f"/api/v1/grants/{r['id']}/revoke").json() == {"revoked": True}


@pytest.mark.parametrize("isatty,pw,expect", [(False, PASS, "interactive terminal"),
                                               (True, "wrong passphrase!!", "wrong owner passphrase"),
                                               (True, "", "declined")])
def test_cli_refuses_non_interactive_or_wrong_passphrase(env, isatty, pw, expect):
    g = ag.request(SPEC, requested_by=API)
    rc, out = cli_approve(g["id"], passphrase=pw, isatty=isatty)
    assert rc == 1 and expect in out and ag.get(g["id"])["status"] == "pending"


def test_cli_shows_the_server_copy_and_approves(env):
    g = ag.request(SPEC, requested_by=API)
    rc, out = cli_approve(g["id"])
    assert rc == 0 and f"w1={env['wt']}" in out and ag.get(g["id"])["spec_hash"][:12] in out
    assert "create up to 1 as owneros-sandbox-" in out and ag.get(g["id"])["status"] == "active"


# ── after approval: exactly the grant, nothing else ────────────────────────────

def test_granted_worker_is_created_once_and_messaged_by_api_only(env):
    gid = granted(env)
    r = ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1", actor=API)
    assert r["agent_created"] and len(created(env["calls"])) == 1
    again = ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1", actor=API)
    assert again["refused"] == "worktree_reserved_by_recent_creation"
    r = ac.agent_send("owneros-sandbox-w1:0.0", "next step", idempotency_key="s1", actor=API)
    assert r["delivered"] and r["authorized_by"] == f"grant:{gid}"
    r = ac.agent_answer("owneros-sandbox-w1:0.0", "1", idempotency_key="a1", actor=API)
    assert r["authorized_by"] == f"grant:{gid}"
    audit = (env["tmp"] / "audit.jsonl").read_text()
    assert f"grant:{gid}" in audit and "grant_approved" in audit


@pytest.mark.parametrize("target", ["bitwhite-backend-v3:0.0", "vtb:0.0",
                                    "mess-telegram-connector:0.0", "payorch-ha-fresh:0.0",
                                    "owneros-sandbox-w2:0.0", "owneros-sandbox-w1x:0.0"])
def test_everything_outside_the_grant_stays_denied(env, target):
    granted(env)
    r = ac.agent_send(target, "x", idempotency_key=f"k-{target}", actor=API)
    assert r["refused"] in ("automation_target_not_allowlisted", "hard_denied_target")
    assert typed(env["calls"]) == []


@pytest.mark.parametrize("actor", [None, "native_supervisor", "cto_bridge",
                                   "owneros_controller", "session_recovery"])
def test_non_api_callers_get_nothing_from_an_api_grant(env, actor):
    granted(env)
    assert ac.agent_send("owneros-sandbox-w1:0.0", "x", idempotency_key=f"k{actor}",
                         actor=actor)["refused"]
    assert ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1",
                           actor=actor)["refused"]
    assert typed(env["calls"]) == [] and created(env["calls"]) == []


def test_grant_does_not_cover_bare_keys(env):
    granted(env)
    assert ac.send_control_keys("owneros-sandbox-w1:0.0", ["Enter"], actor=API)["refused"]


def test_creation_outside_the_granted_worktree_or_name_is_refused(env):
    granted(env)
    other = env["tmp"] / "opt-other"
    other.mkdir()
    assert ac.agent_resume(str(other), session_name="owneros-sandbox-w1",
                           actor=API)["refused"] == "creation_not_in_any_scope"
    assert ac.agent_resume(env["wt"], session_name="owneros-sandbox-zz",
                           actor=API)["refused"] == "grant_session_name_mismatch"
    assert created(env["calls"]) == []


# ── expiry, revocation, tampering ──────────────────────────────────────────────

def test_expired_grant_denies(env, monkeypatch):
    gid = granted(env)
    later = ag.get(gid)["expires_at"] + 1
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", API, cwd=env["wt"]) == gid
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", API, now=later, cwd=env["wt"]) is None
    assert ag.effective_status(ag.get(gid), later) == "expired"


def test_revoked_grant_denies_and_cannot_be_reapproved(env):
    gid = granted(env)
    assert ag.revoke(gid, by=API)
    assert ac.agent_send("owneros-sandbox-w1:0.0", "x", idempotency_key="k",
                         actor=API)["refused"]
    assert cli_approve(gid)[0] == 1


def test_pending_request_lapses(env):
    g = ag.request(SPEC, requested_by=API, now=1000.0)
    with pytest.raises(ag.GrantError):
        ag.approve(g["id"], via="owner_cli", expected_hash=ag.get(g["id"])["spec_hash"],
                   passphrase=PASS, now=1000.0 + ag.PENDING_TTL_SECS + 1)


def test_tampering_with_a_stored_grant_voids_it(env):
    gid = granted(env)
    c = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    spec = json.loads(c.execute("SELECT spec FROM automation_grants WHERE id=?",
                                (gid,)).fetchone()[0])
    spec["targets"] = ["bitwhite-backend-v3"]
    c.execute("UPDATE automation_grants SET spec=? WHERE id=?", (json.dumps(spec), gid))
    c.commit()
    c.close()
    assert ag.allows_send("bitwhite-backend-v3", "agent_send", API) is None
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", API, cwd=env["wt"]) is None


def test_content_changed_after_showing_is_refused(env):
    g = ag.request(SPEC, requested_by=API)
    with pytest.raises(ag.GrantError, match="content changed"):
        ag.approve(g["id"], via="owner_cli", expected_hash="0" * 64, passphrase=PASS)


# ── spec validation: nothing wider than explicit ───────────────────────────────

@pytest.mark.parametrize("spec,msg", [
    ({"project": "p", "actions": ["send"], "targets": ["*"], "ttl_secs": 600}, "exact"),
    ({"project": "p", "actions": ["send"], "targets": ["vtb*"], "ttl_secs": 600}, "exact"),
    ({"project": "p", "actions": ["send"], "targets": ["payorch-ha-fresh"], "ttl_secs": 600},
     "payment"),
    ({"project": "p", "actions": ["send"], "targets": ["PAYORCH-HA-FRESH"], "ttl_secs": 600},
     "payment"),
    ({"project": "p", "actions": ["send"], "targets": [], "ttl_secs": 600}, "targets"),
    ({"project": "p", "actions": ["stop"], "targets": ["vtb"], "ttl_secs": 600}, "subset"),
    ({"project": "p", "actions": ["send"], "targets": ["vtb"], "ttl_secs": 10 ** 9}, "ttl"),
    ({"project": "", "actions": ["send"], "targets": ["vtb"], "ttl_secs": 600}, "project"),
])
def test_invalid_specs_are_refused(spec, msg):
    with pytest.raises(ag.GrantError, match=msg):
        ag.validate_spec(spec)


@pytest.mark.parametrize("wts,cap,msg", [
    ({"w1": "/root/x"}, 1, "/opt"), ({"w1": "/opt/payment-orchestrator"}, 1, "hard-denied"),
    ({"w1": "/opt/a"}, 0, "max_sessions"), ({"w1": "/opt/a"}, 11, "max_sessions"),
    ({"W 1": "/opt/a"}, 1, "key"), ({}, 1, "worktrees"),
])
def test_invalid_create_specs_are_refused(wts, cap, msg, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CREATION_CONFIG", str(tmp_path / "none.yaml"))
    acp_cfg = {"hard_deny_dirs": ["/opt/payment-orchestrator"]}
    from core import agent_creation_policy as acp
    monkeypatch.setattr(acp, "load_config", lambda *a: acp_cfg)
    with pytest.raises(ag.GrantError, match=msg):
        ag.validate_spec({"project": "p", "actions": ["create"], "ttl_secs": 600,
                          "create": {"worktrees": wts, "session_prefix": "s-",
                                     "max_sessions": cap}})


def test_symlinked_create_worktree_is_refused(tmp_path, monkeypatch):
    from core import agent_creation_policy as acp
    monkeypatch.setattr(acp, "load_config", lambda *a: {})
    if not os.path.isdir("/opt"):
        pytest.skip("/opt missing")
    link = "/opt/.grant-test-link-" + os.urandom(4).hex()
    os.symlink(str(tmp_path), link)
    try:
        with pytest.raises(ag.GrantError, match="symlink"):
            ag.validate_spec({"project": "p", "actions": ["create"], "ttl_secs": 600,
                              "create": {"worktrees": {"w1": link}, "session_prefix": "s-",
                                         "max_sessions": 1}})
    finally:
        os.unlink(link)


# ── Telegram owner channel ─────────────────────────────────────────────────────

@pytest.fixture
def tg(env, monkeypatch):
    monkeypatch.setenv("OWNER_GRANT_TELEGRAM_BOT_TOKEN", "123:SECRET-TOKEN")
    monkeypatch.setenv("OWNER_GRANT_TELEGRAM_USER_ID", str(OWNER_TG))
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        sent.append(json.loads(req.data))
        return Resp(json.dumps({"ok": True, "result": []}).encode())
    monkeypatch.setattr(ot.urllib.request, "urlopen", opener)
    return sent


def _code_from(sent, gid):
    import re
    msg = [m for m in sent if gid in m.get("text", "")][-1]
    return re.search(rf"/approve {gid} ([A-Za-z0-9_-]{{12}})", msg["text"]).group(1), msg


def upd(text, uid=OWNER_TG, chat=None, ctype="private", **extra):
    return {"update_id": 1, "message": {"text": text, "from": {"id": uid, "is_bot": False},
                                        "chat": {"id": chat or uid, "type": ctype}, **extra}}


def test_telegram_request_shows_the_server_summary_and_owner_approves(env, tg):
    g = ag.request(SPEC, requested_by=API)
    code, msg = _code_from(tg, g["id"])
    assert msg["chat_id"] == OWNER_TG and ag.summary(ag.get(g["id"])["spec"]) in msg["text"]
    assert ot.handle_update(upd(f"/approve {g['id']} {code}")) == {"approved": True,
                                                                   "grant": g["id"]}
    assert ag.get(g["id"])["status"] == "active"
    assert ot.handle_update(upd(f"/approve {g['id']} {code}"))["approved"] is False   # replay


@pytest.mark.parametrize("kind", ["other_user", "group_chat", "forwarded", "edited", "bot",
                                  "wrong_code", "no_code"])
def test_telegram_non_owner_or_indirect_messages_never_approve(env, tg, kind):
    g = ag.request(SPEC, requested_by=API)
    code, _ = _code_from(tg, g["id"])
    text = f"/approve {g['id']} {code}"
    u = {"other_user": upd(text, uid=999), "group_chat": upd(text, chat=-100, ctype="group"),
         "forwarded": upd(text, forward_date=1),
         "edited": {"update_id": 1, "edited_message": upd(text)["message"]},
         "bot": {"update_id": 1, "message": {**upd(text)["message"],
                                             "from": {"id": OWNER_TG, "is_bot": True}}},
         "wrong_code": upd(f"/approve {g['id']} {'A' if code[0] != 'A' else 'B'}{code[1:]}"),
         "no_code": upd(f"/approve {g['id']}")}[kind]
    ot.handle_update(u)
    assert ag.get(g["id"])["status"] == "pending"


def test_telegram_revoke_and_disabled_without_config(env, tg, monkeypatch):
    gid = granted(env)
    assert ot.handle_update(upd(f"/revoke {gid}")) == {"revoked": True}
    monkeypatch.delenv("OWNER_GRANT_TELEGRAM_BOT_TOKEN")
    assert not ot.enabled() and ot.poll_once() == []


def test_telegram_errors_never_leak_the_token(env, tg, monkeypatch):
    def boom(req, timeout):
        raise OSError(f"failed {req.full_url}")
    monkeypatch.setattr(ot.urllib.request, "urlopen", boom)
    r = ot._call("getUpdates", {})
    assert "SECRET-TOKEN" not in json.dumps(r)



# ── binding to the requesting client ───────────────────────────────────────────

@pytest.mark.parametrize("other", ["api:hmac/other-mcp-client", "api:bearer/chatgpt-mcp",
                                   "api:hmac", "api:client/chatgpt-mcp", "api:hmac/chatgpt-mcp2"])
def test_a_grant_is_usable_only_by_the_exact_requesting_actor(env, other):
    gid = granted(env)
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", API, cwd=env["wt"]) == gid
    r = ac.agent_send("owneros-sandbox-w1:0.0", "x", idempotency_key=f"k-{other}", actor=other)
    assert r["refused"] == "automation_target_not_allowlisted"
    r = ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1", actor=other)
    assert r["refused"] == "creation_not_in_any_scope" and created(env["calls"]) == []


@pytest.mark.parametrize("who", ["api:bearer", "api:hmac", "", "owner", "native_supervisor"])
def test_unidentified_callers_cannot_request(env, who):
    with pytest.raises(ag.GrantError, match="identified client"):
        ag.request(SPEC, requested_by=who)


def test_binding_is_recorded_and_shown(env):
    g = ag.request(SPEC, requested_by=API)
    spec = ag.get(g["id"])["spec"]
    assert spec["bound_to"] == API and spec["binding"] == "declared"
    assert f"only for {API} (declared)" in g["summary"]
    g2 = ag.request(SPEC, requested_by="api:client/chatgpt-connector")
    assert ag.get(g2["id"])["spec"]["binding"] == "authenticated"


def test_require_authenticated_client_fails_closed(env, monkeypatch):
    monkeypatch.setenv("GRANT_REQUIRE_AUTHENTICATED_CLIENT", "1")
    with pytest.raises(ag.GrantError, match="authenticated client"):
        ag.request(SPEC, requested_by=API)
    assert ag.request(SPEC, requested_by="api:client/chatgpt-connector")["status"] == "pending"


@pytest.fixture
def client_tokens(tmp_path, monkeypatch):
    import hashlib
    f = tmp_path / "runtime-clients.json"
    f.write_text(json.dumps({"chatgpt-connector": hashlib.sha256(b"tok-A").hexdigest()}))
    f.chmod(0o600)
    monkeypatch.setenv("RUNTIME_CLIENT_TOKENS_FILE", str(f))
    return f


def _v1():
    return _load("api_v1_client_test", "api/v1.py")


def test_client_token_authenticates_and_overrides_the_declared_name(env, client_tokens,
                                                                    monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    v1 = _v1()
    monkeypatch.setattr(v1, "_TOKEN", "shared-runtime-token")
    monkeypatch.setattr(v1, "agent_control", ac)
    monkeypatch.delenv("GRANT_REQUIRE_AUTHENTICATED_CLIENT", raising=False)
    app = FastAPI()
    app.include_router(v1.router)
    c = TestClient(app)
    hdr = lambda t, name: {"Authorization": f"Bearer {t}", "X-Runtime-Actor": name}  # noqa: E731
    # client token: identity comes from the token, the declared name is ignored
    r = c.post("/api/v1/grants/request", json=SPEC, headers=hdr("tok-A", "owner")).json()
    g = ag.get(r["id"])
    assert g["requested_by"] == "api:client/chatgpt-connector"
    assert g["spec"]["binding"] == "authenticated"
    # with a client-token file present, the shared token can no longer request grants
    r2 = c.post("/api/v1/grants/request", json=SPEC,
                headers=hdr("shared-runtime-token", "chatgpt-connector"))
    assert r2.status_code == 400 and "authenticated client" in r2.json()["detail"]
    # the shared token cannot impersonate the client id by declaring it
    assert v1.caller_identity(type("R", (), {"state": type("S", (), {
        "auth_method": "bearer"})(), "client": None})(), "client/chatgpt-connector")[0] == \
        "api:bearer/client/chatgpt-connector"
    # wrong token -> 401
    assert c.get("/api/v1/grants", headers=hdr("tok-B", "x")).status_code == 401


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_unsafe_client_token_file_is_ignored(env, client_tokens, mode):
    client_tokens.chmod(mode)
    assert _v1().client_for_bearer("tok-A") is None


# ── end to end through the REAL seo MCP server code, patched (not the live repo) ──

SEO = pathlib.Path("/opt/seo/backend/services")
PATCH = ROOT / "integrations" / "seo-mcp" / "owner-os-grants.patch"


@pytest.fixture
def mcp(env, tmp_path, monkeypatch):
    import subprocess
    import types
    if not (SEO / "mcp_server.py").exists():
        pytest.skip("seo MCP server source not present")
    root = tmp_path / "seo"
    (root / "backend" / "services").mkdir(parents=True)
    for f in ("mcp_server.py", "runtime_client.py"):
        (root / "backend" / "services" / f).write_text((SEO / f).read_text())
    (root / "backend" / "services" / "__init__.py").write_text("")
    r = subprocess.run(["git", "apply", "-p1", str(PATCH)], cwd=root, capture_output=True,
                       text=True)
    if r.returncode != 0:
        pytest.skip(f"patch no longer applies to /opt/seo: {r.stderr[:200]}")

    class _Row:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    models = types.ModuleType("models")
    models.McpAudit, models.McpToken = _Row, _Row
    monkeypatch.setitem(sys.modules, "models", models)
    for m in [k for k in sys.modules if k == "services" or k.startswith("services.")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.syspath_prepend(str(root / "backend"))
    import httpx
    from fastapi import FastAPI
    v1 = _load("api_v1_mcp_e2e", "api/v1.py")
    monkeypatch.setattr(v1, "_TOKEN", "shared-runtime-token")
    monkeypatch.setattr(v1, "agent_control", ac)
    app = FastAPI()
    app.include_router(v1.router)
    import services.runtime_client as rc
    import services.mcp_server as ms
    monkeypatch.setattr(rc, "RUNTIME_URL", "http://runtime/api/v1")
    monkeypatch.setattr(rc, "RUNTIME_TOKEN", "shared-runtime-token")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(rc.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime", **kw))

    class DB:
        audits = []

        def add(self, row):
            self.audits.append(row)

        async def commit(self):
            pass
    tok = lambda i: models.McpToken(id=i, label=f"conn{i}", scopes=["read", "write"])  # noqa: E731
    return {"ms": ms, "rc": rc, "db": DB(), "A": tok(7), "B": tok(8)}


def call(m, tok, name, params):
    import asyncio
    try:
        return asyncio.run(m["ms"].call_tool(m["db"], tok, name, params))
    except Exception as e:  # noqa: BLE001
        return {"exception": f"{type(e).__name__}: {e}"}


def test_mcp_tools_are_listed_and_there_is_no_approve_tool(mcp):
    names = set(mcp["ms"].TOOLS)
    assert {"grant_request", "grant_list", "grant_revoke"} <= names
    assert not any("approve" in n and "grant" in n for n in names)


def test_mcp_request_owner_approval_then_scoped_control_end_to_end(mcp, env):
    spec = {"project": "owneros-sandbox", "actions": ["send", "answer", "create"],
            "create": {"worktrees": {"w1": "/opt/sandbox"}, "session_prefix": "owneros-sandbox-",
                       "max_sessions": 1}, "ttl_secs": 3600}
    req = call(mcp, mcp["A"], "grant_request", spec)
    assert req["status"] == "pending", req
    g = ag.get(req["id"])
    assert g["requested_by"] == "api:bearer/mcp-token-7" and g["spec"]["binding"] == "declared"
    # before the owner acts: nothing
    assert "error" in call(mcp, mcp["A"], "agent_resume",
                           {"project_dir": env["wt"], "session_name": "owneros-sandbox-w1"}) \
        or not created(env["calls"])
    # owner approves out of band (host CLI on a TTY)
    assert cli_approve(req["id"])[0] == 0
    r = call(mcp, mcp["A"], "agent_resume",
             {"project_dir": env["wt"], "session_name": "owneros-sandbox-w1"})
    assert r.get("agent_created") and len(created(env["calls"])) == 1, r
    r = call(mcp, mcp["A"], "agent_send", {"target": "owneros-sandbox-w1:0.0", "text": "go"})
    assert r.get("delivered") and r.get("authorized_by") == f"grant:{req['id']}", r
    # another MCP connector, other agents, the payment agent: refused
    r = call(mcp, mcp["B"], "agent_send", {"target": "owneros-sandbox-w1:0.0", "text": "x"})
    assert r.get("refused") == "automation_target_not_allowlisted", r
    for target in ("bitwhite-backend-v3:0.0", "payorch-ha-fresh:0.0"):
        r = call(mcp, mcp["A"], "agent_send", {"target": target, "text": "x"})
        assert r.get("refused") in ("automation_target_not_allowlisted", "hard_denied_target"), r
    r = call(mcp, mcp["B"], "agent_resume",
             {"project_dir": env["wt"], "session_name": "owneros-sandbox-w1"})
    assert not r.get("agent_created") and len(created(env["calls"])) == 1
    # list, revoke, then denied
    assert any(x["id"] == req["id"] and x["effective"] == "active"
               for x in call(mcp, mcp["A"], "grant_list", {})["grants"])
    assert call(mcp, mcp["A"], "grant_revoke", {"grant_id": req["id"]}) == {"revoked": True}
    r = call(mcp, mcp["A"], "agent_send", {"target": "owneros-sandbox-w1:0.0", "text": "again"})
    assert r.get("refused") == "automation_target_not_allowlisted", r
    sends = [c for c in env["calls"] if c and c[0] == "paste-buffer"]
    assert len(sends) == 1


def test_mcp_actor_comes_from_the_verified_token_not_from_params(mcp):
    req = call(mcp, mcp["B"], "grant_request",
               {"project": "p", "actions": ["send"], "targets": ["vtb"], "ttl_secs": 600,
                "actor": "mcp-token-7"})
    assert "exception" in req or ag.get(req["id"])["requested_by"] == "api:bearer/mcp-token-8"
    assert mcp["rc"].CURRENT_ACTOR.get() == ""            # reset after the call


def test_mcp_without_write_scope_cannot_request(mcp):
    import types
    ro = types.SimpleNamespace(id=9, label="ro", scopes=["read"])
    r = call(mcp, ro, "grant_request", {"project": "p", "actions": ["send"],
                                        "targets": ["vtb"], "ttl_secs": 600})
    assert "write" in r.get("exception", "")
    assert "grants" in call(mcp, ro, "grant_list", {})



# ── independent review fixes: no self-approval, rename-proof deny, cwd-bound sessions ──

def test_in_process_callers_cannot_self_approve_without_the_owner_secret(env):
    """An agent (or a tool ChatGPT can make an agent run) calling the Python API directly."""
    g = ag.request(SPEC, requested_by=API)
    h = ag.get(g["id"])["spec_hash"]
    for kw in ({}, {"passphrase": ""}, {"passphrase": "guess-guess-guess"},
               {"code": "000000"}):
        with pytest.raises(ag.GrantError):
            ag.approve(g["id"], via="owner_cli", expected_hash=h, **kw)
    with pytest.raises(ag.GrantError):
        ag.approve(g["id"], via="telegram_verified", expected_hash=h, code="x" * 12)
    assert ag.get(g["id"])["status"] == "pending"


def test_repeated_failures_lock_approvals_even_for_the_right_secret(env):
    g = ag.request(SPEC, requested_by=API)
    h = ag.get(g["id"])["spec_hash"]
    for _ in range(ag.MAX_FAILED_APPROVALS):
        with pytest.raises(ag.GrantError, match="wrong"):
            ag.approve(g["id"], via="owner_cli", expected_hash=h, passphrase="nope-nope-nope")
    with pytest.raises(ag.GrantError, match="locked"):
        ag.approve(g["id"], via="owner_cli", expected_hash=h, passphrase=PASS)


@pytest.mark.parametrize("setup", ["missing", "world_readable", "symlink"])
def test_cli_approval_fails_closed_without_a_safe_passphrase_file(env, setup):
    f = pathlib.Path(os.environ["OWNER_GRANT_PASSPHRASE_FILE"])
    if setup == "missing":
        f.unlink()
    elif setup == "world_readable":
        f.chmod(0o644)
    else:
        real = f.with_name("real.json")
        f.rename(real)
        f.symlink_to(real)
    g = ag.request(SPEC, requested_by=API)
    with pytest.raises(ag.GrantError, match="no owner passphrase"):
        ag.approve(g["id"], via="owner_cli", expected_hash=ag.get(g["id"])["spec_hash"],
                   passphrase=PASS)


def test_passphrase_is_never_overwritten_and_must_be_long(env):
    with pytest.raises(ag.GrantError, match="exists"):
        ag.set_passphrase("another long passphrase")
    os.unlink(os.environ["OWNER_GRANT_PASSPHRASE_FILE"])
    with pytest.raises(ag.GrantError, match="12 characters"):
        ag.set_passphrase("short")


def test_telegram_code_is_high_entropy_and_not_sha256_guessable(env, monkeypatch):
    import hashlib
    sent = []
    monkeypatch.setattr(ag, "_notify_owner", lambda gid, spec, code: sent.append(code))
    g = ag.request(SPEC, requested_by=API)
    code = sent[0]
    assert len(code) == 12
    stored = ag.get(g["id"])["code_hash"]
    assert stored != hashlib.sha256(f"{g['id']}:{code}".encode()).hexdigest()
    assert stored == ag._scrypt(code, g["id"])


@pytest.mark.parametrize("session", ["owneros-sandbox-w1", "owner-os-supervisor-clean"])
def test_a_renamed_session_in_the_payment_directory_stays_denied(env, session, monkeypatch):
    granted(env)
    env["cwd_of"][session] = "/opt/payment-orchestrator"        # rename trick
    r = ac.agent_send(f"{session}:0.0", "x", idempotency_key=f"k{session}", actor=API)
    assert r["refused"] == "hard_denied_target" and typed(env["calls"]) == []
    assert ac.send_control_keys(f"{session}:0.0", ["Enter"], actor=API)["refused"] == \
        "hard_denied_target"


def test_a_granted_name_outside_its_worktree_is_not_covered(env):
    granted(env)
    env["cwd_of"]["owneros-sandbox-w1"] = "/opt/somewhere-else"   # same name, other place
    r = ac.agent_send("owneros-sandbox-w1:0.0", "x", idempotency_key="k", actor=API)
    assert r["refused"] == "automation_target_not_allowlisted"


def test_explicit_targets_still_work_by_name(env):
    g = ag.request({"project": "p", "actions": ["send"], "targets": ["vtb"], "ttl_secs": 600},
                   requested_by=API)
    assert cli_approve(g["id"])[0] == 0
    r = ac.agent_send("vtb:0.0", "status?", idempotency_key="k", actor=API)
    assert r["delivered"] and r["authorized_by"] == f"grant:{g['id']}"



def test_sandbox_request_spec_validates_and_verifier_reports_covered_actions(env, capsys):
    spec = json.loads((ROOT / "integrations" / "sandbox-grant" / "request.json").read_text())
    assert spec["create"]["max_sessions"] == 1 and spec["ttl_secs"] == 3600
    assert spec["create"]["worktrees"] == {"worker-20261027":
                                           "/opt/owneros-sandbox-worker-20261027"}
    gid = granted(env)
    assert ac.agent_resume(env["wt"], session_name="owneros-sandbox-w1", actor=API)["agent_created"]
    assert ac.agent_send("owneros-sandbox-w1:0.0", "go", idempotency_key="v1", actor=API)["delivered"]
    v = _load("grant_verify", "tools/grant_verify.py")
    assert v.main([gid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {r["action"] for r in out["covered_actions"]} == {"agent_resume", "agent_send"}
    assert out["unexpected"] == [] and out["binding"] == "declared"
