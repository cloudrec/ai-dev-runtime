"""Windows agents inside the Agent Fabric (task 220, goal 4).

The fabric must show and drive a Windows workspace with the same verbs as a
tmux pane WITHOUT blurring the two: platform stays explicit on every entry, the
tmux path keeps behaving exactly as it did, and a Windows verb that cannot be
delivered says so instead of pretending.
"""
from __future__ import annotations

import threading
import time

import pytest

from core import agent_fabric, windows_bridge as wb


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


@pytest.fixture()
def device():
    d = wb.enroll(wb.create_enrollment_code()["code"], device_name="OWNER-PC")
    wb.report_workspaces(d["device_id"], [
        {"workspace_id": "gaika-basket", "label": "GAIKA",
         "path_hint": r"C:\Users\owner\Desktop\gaika-basket-extension",
         "state": "idle", "session_id": "sess-1"}])
    return d


@pytest.fixture()
def ref(device):
    return f"win:{device['device_id']}:gaika-basket"


def _auto_answer(device_id, result, *, ok=True, rounds=60):
    """Stand in for the Windows agent's poll loop."""
    def loop():
        for _ in range(rounds):
            leased = wb.lease(device_id)
            if leased:
                wb.complete(device_id, leased[0]["command_id"], ok=ok, result=result,
                            error="" if ok else "device refused")
                return
            time.sleep(0.05)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# ── refs ────────────────────────────────────────────────────────────────────

def test_a_windows_ref_parses_into_device_and_workspace(ref, device):
    kind, ident = agent_fabric.parse_ref(ref)
    assert kind == "win"
    assert agent_fabric._win_parts(ident) == (device["device_id"], "gaika-basket")


@pytest.mark.parametrize("bad", ["win:", "win:only-device", "windows:a:b", "nope:x"])
def test_a_malformed_ref_is_refused(bad):
    with pytest.raises(agent_fabric.FabricError):
        agent_fabric.status(bad)


def test_existing_ref_kinds_are_untouched():
    assert agent_fabric.parse_ref("tmux:gaika:0.0") == ("tmux", "gaika:0.0")
    assert agent_fabric.parse_ref("runtime:abc-123") == ("runtime", "abc-123")


# ── inventory ───────────────────────────────────────────────────────────────

def test_the_inventory_lists_windows_workspaces_with_explicit_platform(ref, monkeypatch):
    from core import agent_control, job_store
    monkeypatch.setattr(agent_control, "agent_list", lambda: {"agents": []})
    monkeypatch.setattr(job_store, "list_jobs", lambda limit=100: [])
    out = agent_fabric.list_agents()
    entry = next(e for e in out["agents"] if e["ref"] == ref)
    assert entry["platform"] == "windows"
    assert entry["kind"] == "win"
    assert entry["session_id"] == "sess-1"
    assert "send" in entry["capabilities"]


def test_a_windows_bridge_outage_does_not_blind_the_tmux_inventory(monkeypatch):
    from core import agent_control, job_store
    monkeypatch.setattr(agent_control, "agent_list", lambda: {"agents": [
        {"is_agent": True, "target": "gaika:0.0", "state": "working", "alive": True,
         "claude_cwd": "/opt/project"}]})
    monkeypatch.setattr(job_store, "list_jobs", lambda limit=100: [])
    monkeypatch.setattr(wb, "inventory", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("bridge down")))
    out = agent_fabric.list_agents()
    assert [e["ref"] for e in out["agents"]] == ["tmux:gaika:0.0"]
    assert out["agents"][0]["platform"] == "linux"
    assert any("windows_inventory_unavailable" in e for e in out["errors"])


# ── verbs ───────────────────────────────────────────────────────────────────

def test_send_delivers_to_the_device_and_returns_its_answer(ref, device):
    t = _auto_answer(device["device_id"], {"reply": "done", "session_id": "sess-1"})
    out = agent_fabric.send(ref, "add a badge to the cart button")
    t.join(timeout=5)
    assert out["ok"] is True
    assert out["reply"] == "done"


def test_send_uses_the_idempotency_key_as_the_command_id(ref, device):
    key = "aaaaaaaa-1111-2222-3333-444444444444"
    t = _auto_answer(device["device_id"], {"reply": "ok"})
    out = agent_fabric.send(ref, "hello", idempotency_key=key)
    t.join(timeout=5)
    assert out["command_id"] == key
    # A retry with the same key must not enqueue a second turn.
    again = agent_fabric.send(ref, "hello", idempotency_key=key)
    assert again["command_id"] == key
    assert again["reply"] == "ok"


def test_status_merges_inventory_with_a_live_probe(ref, device):
    t = _auto_answer(device["device_id"], {"state": "working", "running": True})
    out = agent_fabric.status(ref)
    t.join(timeout=5)
    assert out["platform"] == "windows"
    assert out["live"]["ok"] is True
    assert out["live"]["running"] is True


def test_status_of_an_unknown_windows_workspace_is_refused(device):
    with pytest.raises(agent_fabric.FabricError, match="no such windows workspace"):
        agent_fabric.status(f"win:{device['device_id']}:not-enrolled")


def test_stop_requires_confirmation_on_the_windows_path_too(ref):
    with pytest.raises(agent_fabric.FabricError, match="confirm"):
        agent_fabric.stop(ref)


def test_stop_with_confirmation_reaches_the_device(ref, device):
    t = _auto_answer(device["device_id"], {"stopped": True})
    out = agent_fabric.stop(ref, confirm=True)
    t.join(timeout=5)
    assert out["ok"] is True
    assert out["stopped"] is True


def test_result_reads_the_workspace_transcript(ref, device):
    t = _auto_answer(device["device_id"], {"output": "line one\nline two"})
    out = agent_fabric.result(ref)
    t.join(timeout=5)
    assert "line two" in out["output"]


def test_start_or_resume_ref_starts_a_new_session(ref, device):
    t = _auto_answer(device["device_id"], {"session_id": "sess-2", "resumed": False})
    out = agent_fabric.start_or_resume_ref(ref, text="pick up the extension work")
    t.join(timeout=5)
    assert out["ok"] is True
    assert out["session_id"] == "sess-2"


def test_start_or_resume_ref_refuses_a_non_windows_ref():
    with pytest.raises(agent_fabric.FabricError, match="windows refs"):
        agent_fabric.start_or_resume_ref("tmux:gaika:0.0")


def test_an_offline_device_is_a_reported_timeout_not_a_claimed_success(ref, monkeypatch):
    monkeypatch.setattr(agent_fabric, "_WIN_WAIT_SECS", 0.4)
    out = agent_fabric.send(ref, "are you there?")
    assert out["ok"] is False
    assert "did not answer" in out["error"]


def test_a_device_side_failure_is_surfaced_verbatim(ref, device):
    t = _auto_answer(device["device_id"], {"detail": "claude exited 3"}, ok=False)
    out = agent_fabric.send(ref, "break something")
    t.join(timeout=5)
    assert out["ok"] is False
    assert out["error"] == "device refused"


def test_sending_to_a_disabled_workspace_is_refused_by_the_server(ref, device):
    wb.set_workspace_enabled(device["device_id"], "gaika-basket", False)
    with pytest.raises(agent_fabric.FabricError, match="disabled"):
        agent_fabric.send(ref, "hello")
