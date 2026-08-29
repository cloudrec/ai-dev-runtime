"""Windows bridge — server half (task 220).

Every test here is a security or truthfulness property, not a happy path:
enrollment codes that cannot be reused, signatures that cannot be replayed or
re-pointed, an action allowlist that cannot be widened by a parameter, a
workspace that cannot be addressed until the owner enrolled it ON the Windows
machine, results that cannot carry a credential into the control plane, and an
offline device that produces a refusal rather than a hang.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core import windows_bridge as wb


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.fixture()
def device():
    code = wb.create_enrollment_code("owner laptop")["code"]
    d = wb.enroll(code, device_name="OWNER-PC", os_version="Windows 11",
                  agent_version="0.1.0")
    wb.report_workspaces(d["device_id"], [
        {"workspace_id": "gaika-basket", "label": "GAIKA basket extension",
         "path_hint": r"C:\Users\owner\Desktop\gaika-basket-extension",
         "state": "idle"}])
    return d


def _sign(device, ts, nonce, path, body):
    return wb.sign(device["secret"], device["device_id"], ts, nonce, path, body)


# ── enrollment ──────────────────────────────────────────────────────────────

def test_enrollment_code_is_single_use():
    code = wb.create_enrollment_code("laptop")["code"]
    wb.enroll(code, device_name="PC-1")
    with pytest.raises(wb.AuthError, match="already used"):
        wb.enroll(code, device_name="PC-2")


def test_enrollment_code_expires():
    out = wb.create_enrollment_code("laptop", ttl_secs=60)
    with pytest.raises(wb.AuthError, match="expired"):
        wb.enroll(out["code"], device_name="PC", now=out["expires_ts"] + 1)


def test_unknown_enrollment_code_is_refused():
    with pytest.raises(wb.AuthError, match="unknown"):
        wb.enroll("OOS-AAAAA-BBBBB-CCCCC", device_name="PC")


def test_enrollment_code_is_stored_only_as_a_hash(tmp_path):
    code = wb.create_enrollment_code("laptop")["code"]
    rows = list(sqlite3.connect(str(tmp_path / "cp.db"))
                .execute("SELECT * FROM win_enrollment"))
    blob = json.dumps(rows)
    assert code not in blob
    assert code.replace("OOS-", "") not in blob


def test_each_device_gets_its_own_secret():
    a = wb.enroll(wb.create_enrollment_code()["code"], device_name="A")
    b = wb.enroll(wb.create_enrollment_code()["code"], device_name="B")
    assert a["device_id"] != b["device_id"]
    assert a["secret"] != b["secret"]
    assert len(a["secret"]) == 64


# ── request authentication ──────────────────────────────────────────────────

PATH = "/api/v1/windows/poll"
BODY = b'{"wait":5}'


def test_a_valid_signature_authenticates(device):
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    got = wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                            BODY, sig, now=1000)
    assert got["device_id"] == device["device_id"]


def test_a_replayed_nonce_is_refused(device):
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH, BODY,
                      sig, now=1000)
    with pytest.raises(wb.AuthError, match="replay"):
        wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                          BODY, sig, now=1000)


def test_a_signature_cannot_be_re_pointed_at_another_route(device):
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    with pytest.raises(wb.AuthError, match="bad signature"):
        wb.verify_request(device["device_id"], "1000", "nonce-bbbb2222",
                          "/api/v1/windows/result", BODY, sig, now=1000)


def test_a_tampered_body_invalidates_the_signature(device):
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    with pytest.raises(wb.AuthError, match="bad signature"):
        wb.verify_request(device["device_id"], "1000", "nonce-bbbb2222", PATH,
                          b'{"wait":50}', sig, now=1000)


def test_a_stale_timestamp_is_refused(device):
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    with pytest.raises(wb.AuthError, match="stale"):
        wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                          BODY, sig, now=1000 + wb.CLOCK_SKEW_SECS + 1)


def test_another_devices_secret_cannot_sign_for_this_one(device):
    other = wb.enroll(wb.create_enrollment_code()["code"], device_name="OTHER")
    sig = wb.sign(other["secret"], device["device_id"], "1000", "nonce-aaaa1111",
                  PATH, BODY)
    with pytest.raises(wb.AuthError, match="bad signature"):
        wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                          BODY, sig, now=1000)


def test_rotation_invalidates_the_previous_secret(device):
    old_sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    new = wb.rotate_secret(device["device_id"])
    assert new["secret"] != device["secret"]
    with pytest.raises(wb.AuthError, match="bad signature"):
        wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                          BODY, old_sig, now=1000)
    fresh = wb.sign(new["secret"], device["device_id"], "1000", "nonce-cccc3333",
                    PATH, BODY)
    assert wb.verify_request(device["device_id"], "1000", "nonce-cccc3333", PATH,
                             BODY, fresh, now=1000)


def test_a_revoked_device_cannot_authenticate_or_be_commanded(device):
    wb.revoke_device(device["device_id"], reason="laptop lost")
    sig = _sign(device, "1000", "nonce-aaaa1111", PATH, BODY)
    with pytest.raises(wb.AuthError, match="revoked"):
        wb.verify_request(device["device_id"], "1000", "nonce-aaaa1111", PATH,
                          BODY, sig, now=1000)
    with pytest.raises(wb.WindowsBridgeError, match="revoked"):
        wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")


def test_revoking_expires_that_devices_pending_commands(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.revoke_device(device["device_id"])
    assert wb.get_command(cmd["command_id"])["status"] == "expired"


def test_malformed_credentials_are_refused_before_any_lookup(device):
    for bad in [("", "nonce-aaaa1111", "sig"), (device["device_id"], "sh", "sig"),
                (device["device_id"], "nonce-aaaa1111", "not-hex")]:
        with pytest.raises(wb.AuthError):
            wb.verify_request(bad[0], "1000", bad[1], PATH, BODY, bad[2] * 8, now=1000)


# ── the action allowlist ────────────────────────────────────────────────────

def test_the_remote_surface_has_no_shell_verb():
    """The property that matters most: there is no way to ask a Windows device
    to run a command of the caller's choosing.

    The set is pinned deliberately. Adding a verb has to be a decision someone
    makes here, in a test, not a side effect of editing a dict — which is why
    `workspace.inspect` (read-only git/tree facts, fixed argv on the device)
    appears in this list rather than slipping in unnoticed."""
    assert set(wb.ACTIONS) == {"workspace.list", "workspace.inspect",
                               "agent.status", "agent.read",
                               "agent.start", "agent.send", "agent.stop"}
    joined = " ".join(wb.ACTIONS).lower()
    for forbidden in ("exec", "shell", "cmd", "powershell", "script", "eval"):
        assert forbidden not in joined
    # No action takes a parameter that could carry a command or a path.
    for params in wb.ACTIONS.values():
        assert not ({"cmd", "command", "args", "argv", "shell", "path", "cwd"}
                    & set(params))


def test_an_unknown_action_is_refused(device):
    with pytest.raises(wb.WindowsBridgeError, match="unknown action"):
        wb.enqueue(device["device_id"], "shell.exec", workspace_id="gaika-basket",
                   params={"cmd": "whoami"})


def test_unknown_params_are_refused_rather_than_ignored(device):
    with pytest.raises(wb.WindowsBridgeError, match="not accepted"):
        wb.enqueue(device["device_id"], "agent.send", workspace_id="gaika-basket",
                   params={"text": "hi", "cwd": r"C:\Windows\System32"})


def test_a_command_cannot_carry_a_path(device):
    """No action anywhere in the surface accepts a path parameter."""
    for params in wb.ACTIONS.values():
        assert not {"path", "cwd", "dir", "file", "workspace_path"} & set(params)


def test_stop_requires_explicit_confirmation(device):
    with pytest.raises(wb.WindowsBridgeError, match="confirm"):
        wb.enqueue(device["device_id"], "agent.stop", workspace_id="gaika-basket",
                   params={"confirm": False})
    assert wb.enqueue(device["device_id"], "agent.stop", workspace_id="gaika-basket",
                      params={"confirm": True})["status"] == "pending"


def test_oversized_and_nul_bearing_text_is_refused(device):
    with pytest.raises(wb.WindowsBridgeError, match="exceeds"):
        wb.enqueue(device["device_id"], "agent.send", workspace_id="gaika-basket",
                   params={"text": "x" * (wb.MAX_TEXT_BYTES + 1)})
    with pytest.raises(wb.WindowsBridgeError, match="NUL"):
        wb.enqueue(device["device_id"], "agent.send", workspace_id="gaika-basket",
                   params={"text": "hi\x00there"})


def test_line_counts_are_clamped_not_trusted(device):
    cmd = wb.enqueue(device["device_id"], "agent.read", workspace_id="gaika-basket",
                     params={"lines": 10 ** 9})
    assert cmd["params"]["lines"] == wb.MAX_LINES


@pytest.mark.parametrize("bad", [
    "../../../Windows/System32", r"..\..\Users", "gaika/../../etc", "C:\\Users\\x",
    "gaika basket", "GAIKA-BASKET", "", "-leading", "x" * 65,
])
def test_traversal_shaped_workspace_ids_are_refused(device, bad):
    with pytest.raises(wb.WindowsBridgeError, match="workspace"):
        wb.enqueue(device["device_id"], "agent.status", workspace_id=bad)


def test_a_workspace_the_device_never_enrolled_is_unreachable(device):
    with pytest.raises(wb.WindowsBridgeError, match="not enrolled"):
        wb.enqueue(device["device_id"], "agent.status", workspace_id="some-other-repo")


def test_a_disabled_workspace_is_unreachable_without_touching_the_device(device):
    wb.set_workspace_enabled(device["device_id"], "gaika-basket", False)
    with pytest.raises(wb.WindowsBridgeError, match="disabled"):
        wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.set_workspace_enabled(device["device_id"], "gaika-basket", True)
    assert wb.enqueue(device["device_id"], "agent.status",
                      workspace_id="gaika-basket")["status"] == "pending"


def test_a_device_wide_action_refuses_a_workspace(device):
    with pytest.raises(wb.WindowsBridgeError, match="addresses the device"):
        wb.enqueue(device["device_id"], "workspace.list", workspace_id="gaika-basket")
    assert wb.enqueue(device["device_id"], "workspace.list")["status"] == "pending"


# ── the queue ───────────────────────────────────────────────────────────────

def test_the_same_command_id_never_runs_twice(device):
    a = wb.enqueue(device["device_id"], "agent.send", workspace_id="gaika-basket",
                   params={"text": "one"}, command_id="11111111-2222-3333-4444-555555555555")
    b = wb.enqueue(device["device_id"], "agent.send", workspace_id="gaika-basket",
                   params={"text": "two"}, command_id="11111111-2222-3333-4444-555555555555")
    assert a["command_id"] == b["command_id"]
    assert b["params"]["text"] == "one"          # the replay did not overwrite
    assert len(wb.lease(device["device_id"])) == 1


def test_lease_hands_a_command_over_exactly_once(device):
    wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    assert len(wb.lease(device["device_id"])) == 1
    assert wb.lease(device["device_id"]) == []


def test_a_device_cannot_complete_another_devices_command(device):
    other = wb.enroll(wb.create_enrollment_code()["code"], device_name="OTHER")
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    with pytest.raises(wb.AuthError, match="different device"):
        wb.complete(other["device_id"], cmd["command_id"], ok=True, result={})


def test_completing_twice_is_idempotent(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.complete(device["device_id"], cmd["command_id"], ok=True, result={"state": "idle"})
    again = wb.complete(device["device_id"], cmd["command_id"], ok=False,
                        error="second answer")
    assert again["ok"] is True
    assert again["result"] == {"state": "idle"}


def test_a_result_is_redacted_and_still_valid_json(device):
    """Redaction runs INSIDE the structure. Running it over a serialized
    document would eat the closing quote of a `KEY=value` match and leave the
    row unparseable — which is exactly what happened before this test existed."""
    cmd = wb.enqueue(device["device_id"], "agent.read", workspace_id="gaika-basket")
    out = wb.complete(device["device_id"], cmd["command_id"], ok=True, result={
        "output": "ANTHROPIC_API_KEY=sk-ant-abcdefgh12345678\nBearer eyJhbGciOi.JzdWIiOiJ",
        "lines": 2})
    assert isinstance(out["result"], dict)
    assert out["result"]["lines"] == 2
    assert "sk-ant-abcdefgh12345678" not in json.dumps(out["result"])
    assert "REDACTED" in json.dumps(out["result"])


def test_an_oversized_result_is_replaced_not_stored(device):
    cmd = wb.enqueue(device["device_id"], "agent.read", workspace_id="gaika-basket")
    out = wb.complete(device["device_id"], cmd["command_id"], ok=True,
                      result={"output": "x" * (wb.MAX_RESULT_BYTES + 10)})
    assert out["result"] == {"truncated": True,
                             "note": f"result exceeded {wb.MAX_RESULT_BYTES} bytes"}


def test_an_uncollected_command_expires_instead_of_hanging(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.expire_stale(now=wb.now_ts() + wb.COMMAND_TTL_SECS + 1)
    got = wb.get_command(cmd["command_id"])
    assert got["status"] == "expired"
    assert "did not collect" in got["error"]


def test_dispatch_reports_a_sleeping_device_as_timed_out_not_failed(device):
    out = wb.dispatch(device["device_id"], "agent.status", workspace_id="gaika-basket",
                      wait_secs=0.5)
    assert out["timed_out"] is True
    assert out["status"] == "pending"       # still queued; nothing was invented


def test_dispatch_returns_the_devices_answer(device):
    import threading

    cmd_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def answer():
        for _ in range(40):
            leased = wb.lease(device["device_id"])
            if leased:
                wb.complete(device["device_id"], leased[0]["command_id"], ok=True,
                            result={"state": "idle", "session_id": "sess-1"})
                return
            import time as _t
            _t.sleep(0.05)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    out = wb.dispatch(device["device_id"], "agent.status", workspace_id="gaika-basket",
                      command_id=cmd_id, wait_secs=5)
    t.join(timeout=5)
    assert out["timed_out"] is False
    assert out["ok"] is True
    assert out["result"]["session_id"] == "sess-1"


# ── inventory ───────────────────────────────────────────────────────────────

def test_workspaces_are_device_reported_and_un_reporting_removes_them(device):
    wb.report_workspaces(device["device_id"], [{"workspace_id": "other-repo",
                                                "state": "idle"}])
    ids = {w["workspace_id"] for w in
           wb.list_workspaces(device["device_id"])["workspaces"]}
    assert ids == {"other-repo"}          # gaika-basket was un-enrolled locally


def test_reported_workspaces_are_validated(device):
    with pytest.raises(wb.WindowsBridgeError, match="bad workspace_id"):
        wb.report_workspaces(device["device_id"], [{"workspace_id": "../etc"}])
    with pytest.raises(wb.WindowsBridgeError, match="too many"):
        wb.report_workspaces(device["device_id"],
                             [{"workspace_id": f"w{i}"} for i in range(wb.MAX_WORKSPACES + 1)])


def test_inventory_marks_a_silent_device_offline_rather_than_hiding_it(device):
    live = wb.inventory()
    assert live[0]["ref"] == f"win:{device['device_id']}:gaika-basket"
    assert live[0]["platform"] == "windows"
    assert live[0]["alive"] is True
    stale = wb.inventory(now=wb.now_ts() + wb.DEVICE_ONLINE_SECS + 60)
    assert stale[0]["online"] is False
    assert stale[0]["alive"] is False


def test_policy_dump_describes_the_whole_surface():
    p = wb.policy()
    assert p["no_shell"] is True
    assert p["paths_on_the_wire"] is False
    assert set(p["actions"]) == set(wb.ACTIONS)
    assert p["ref_format"] == "win:<device_id>:<workspace_id>"


# ── workspace.inspect: read-only, still not a shell ────────────────────────

def test_inspect_is_allowlisted_and_addresses_one_workspace(device):
    cmd = wb.enqueue(device["device_id"], "workspace.inspect",
                     workspace_id="gaika-basket", params={"max_files": 50})
    assert cmd["status"] == "pending"
    assert cmd["params"]["max_files"] == 50


def test_inspect_max_files_is_clamped(device):
    cmd = wb.enqueue(device["device_id"], "workspace.inspect",
                     workspace_id="gaika-basket", params={"max_files": 10 ** 7})
    assert cmd["params"]["max_files"] == 2000


def test_inspect_takes_no_command_or_path_parameter(device):
    """The reconciliation needs facts from the Windows repo, and the way NOT to
    get them is a parameter that reaches a command line."""
    assert set(wb.ACTIONS["workspace.inspect"]) == {"max_files"}
    for bad in ({"cmd": "git log"}, {"args": ["-C", "/etc"]}, {"path": "C:\\Windows"}):
        with pytest.raises(wb.WindowsBridgeError, match="not accepted"):
            wb.enqueue(device["device_id"], "workspace.inspect",
                       workspace_id="gaika-basket", params=bad)


def test_inspect_still_requires_an_enrolled_workspace(device):
    with pytest.raises(wb.WindowsBridgeError, match="not enrolled"):
        wb.enqueue(device["device_id"], "workspace.inspect",
                   workspace_id="some-other-repo")


# ── a command for a device that never polls must still expire ────────────────
# expire_stale() ran ONLY inside lease(), i.e. only when the device polled. The
# existing test above proves expiry works when expire_stale is called by hand —
# nothing in production called it for a device that never comes back, so such a
# command hung as `pending` forever: wait_for_result could never observe
# `expired` and always exited via timed_out, and the status read stayed
# `pending` indefinitely. That contradicts this module's contract ("if the
# laptop is asleep the command simply expires ... never a hang") in exactly the
# case the contract is about. Live shape: the enrolled device
# win-92840f98d82ad3fe has been offline since 2026-08-27.

def _backdate(command_id):
    """Age a command past COMMAND_TTL_SECS without touching its status."""
    c, own = wb._conn(None)
    try:
        c.execute("UPDATE win_command SET created_ts=? WHERE command_id=?",
                  (wb.now_ts() - wb.COMMAND_TTL_SECS - 1, command_id))
        c.commit()
    finally:
        if own:
            c.close()


def test_get_command_expires_what_the_device_never_collected(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    assert wb.get_command(cmd["command_id"])["status"] == "pending"
    _backdate(cmd["command_id"])                       # device never polls again
    got = wb.get_command(cmd["command_id"])
    assert got["status"] == "expired", "a never-collected command hung as pending"
    assert "did not collect" in (got["error"] or "")


def test_wait_for_result_reports_expired_rather_than_a_bare_timeout(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    _backdate(cmd["command_id"])
    res = wb.wait_for_result(cmd["command_id"], timeout_secs=2, poll_secs=0.05)
    assert res["status"] == "expired"
    assert res["timed_out"] is False


def test_a_fresh_command_is_not_expired_by_being_read(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    for _ in range(3):
        assert wb.get_command(cmd["command_id"])["status"] == "pending"


def test_a_finished_command_is_never_rewritten_by_a_read(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.lease(device["device_id"])
    wb.complete(device["device_id"], cmd["command_id"], ok=True, result={"state": "idle"})
    _backdate(cmd["command_id"])                       # old, but already terminal
    got = wb.get_command(cmd["command_id"])
    assert got["status"] == "done"


# ── a late result must not resurrect an expired command ──────────────────────
# complete() treated only ("done","failed") as terminal, so `expired` was
# writable. expire_stale retires `leased` commands too, so a device that took
# work and then went dark mid-execution could come back and flip
# expired -> done. The owner has already been told the command was refused and
# may have re-issued it on that basis, so that silently converts a refusal into
# a success and hides a double execution — the "half-applied action" this
# module's contract disclaims.

def test_a_late_result_cannot_overwrite_an_expired_command(device):
    cmd = wb.enqueue(device["device_id"], "agent.start", workspace_id="gaika-basket",
                     params={"text": "do the thing"})
    wb.lease(device["device_id"])                       # taken, then the device dies
    wb.expire_stale(now=wb.now_ts() + wb.COMMAND_TTL_SECS + 1)
    assert wb.get_command(cmd["command_id"])["status"] == "expired"

    out = wb.complete(device["device_id"], cmd["command_id"], ok=True,
                      result={"started": True})
    assert out["status"] == "expired", "a late result resurrected an expired command"
    final = wb.get_command(cmd["command_id"])
    assert final["status"] == "expired"
    assert final["ok"] in (None, 0)
    # the refusal the owner was shown is still intact
    assert "did not collect" in (final["error"] or "")


def test_a_late_failure_also_cannot_overwrite_an_expired_command(device):
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.lease(device["device_id"])
    wb.expire_stale(now=wb.now_ts() + wb.COMMAND_TTL_SECS + 1)
    out = wb.complete(device["device_id"], cmd["command_id"], ok=False, error="boom")
    assert out["status"] == "expired"
    assert "boom" not in (wb.get_command(cmd["command_id"])["error"] or "")


def test_a_normal_result_still_completes(device):
    """Guard: the expiry rule must not block the ordinary path."""
    cmd = wb.enqueue(device["device_id"], "agent.status", workspace_id="gaika-basket")
    wb.lease(device["device_id"])
    out = wb.complete(device["device_id"], cmd["command_id"], ok=True,
                      result={"state": "idle"})
    assert out["status"] == "done"
    assert wb.get_command(cmd["command_id"])["status"] == "done"
