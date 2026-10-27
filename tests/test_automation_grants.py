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
    for k in ("OWNER_GRANT_TELEGRAM_BOT_TOKEN", "OWNER_GRANT_TELEGRAM_USER_ID"):
        monkeypatch.delenv(k, raising=False)
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
    calls, live = [], []

    def tmux(args, stdin=None):
        calls.append(list(args))
        return (1 if args[0] == "has-session" else 0), "", ""
    monkeypatch.setattr(ac, "_tmux", tmux)
    pane = lambda t: {"target": t if ":" in t else f"{t}:0.0",  # noqa: E731
                      "session": t.split(":")[0], "alive": True}
    monkeypatch.setattr(ac, "_pane_is_live_agent", pane)
    monkeypatch.setattr(ac, "agent_list", lambda: {"agents": [
        {"is_agent": True, "alive": True, "claude_cwd": c} for c in live]})
    monkeypatch.setattr(ac, "find_live_agent_for_dir", lambda d: None)
    monkeypatch.setattr(ac, "validate_project_dir", lambda d: os.path.realpath(d))
    monkeypatch.setattr(ac, "_busy_for_send", lambda t: "")
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    return {"tmp": tmp_path, "wt": str(wt), "calls": calls, "live": live}


def typed(calls):
    return [c for c in calls if c and c[0] in ("paste-buffer", "send-keys", "load-buffer")]


def created(calls):
    return [c for c in calls if c and c[0] == "new-session"]


SPEC = {"project": "owneros-sandbox", "actions": ["send", "answer", "create"],
        "targets": [], "create": {"worktrees": {"w1": "/opt/sandbox"},
                                  "session_prefix": "owneros-sandbox-", "max_sessions": 1},
        "ttl_secs": 3600}


def cli_approve(gid, retype=None, isatty=True):
    cli = _load("owner_grant_cli", "tools/owner_grant.py")
    out = io.StringIO()
    rc = cli.main(["approve", gid], stdin=io.StringIO((retype or gid) + "\n"), stdout=out,
                  isatty=isatty)
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


@pytest.mark.parametrize("isatty,retype,expect", [(False, None, "interactive terminal"),
                                                   (True, "gWRONG", "declined")])
def test_cli_refuses_non_interactive_or_wrong_retype(env, isatty, retype, expect):
    g = ag.request(SPEC, requested_by=API)
    rc, out = cli_approve(g["id"], retype=retype, isatty=isatty)
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
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", "api", now=later) is None
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
                   now=1000.0 + ag.PENDING_TTL_SECS + 1)


def test_tampering_with_a_stored_grant_voids_it(env):
    gid = granted(env)
    c = sqlite3.connect(os.environ["AGENT_CONTROL_DB"])
    spec = json.loads(c.execute("SELECT spec FROM automation_grants WHERE id=?",
                                (gid,)).fetchone()[0])
    spec["targets"] = ["bitwhite-backend-v3"]
    c.execute("UPDATE automation_grants SET spec=? WHERE id=?", (json.dumps(spec), gid))
    c.commit()
    c.close()
    assert ag.allows_send("bitwhite-backend-v3", "agent_send", "api") is None
    assert ag.allows_send("owneros-sandbox-w1", "agent_send", "api") is None


def test_content_changed_after_showing_is_refused(env):
    g = ag.request(SPEC, requested_by=API)
    with pytest.raises(ag.GrantError, match="content changed"):
        ag.approve(g["id"], via="owner_cli", expected_hash="0" * 64)


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
    return re.search(rf"/approve {gid} (\d{{6}})", msg["text"]).group(1), msg


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
         "wrong_code": upd(f"/approve {g['id']} {'1' if code[0] != '1' else '2'}{code[1:]}"),
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
