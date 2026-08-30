"""End-to-end proof of the Windows bridge, and the concurrency defect it caught.

`tools/windows_bridge_sim.py` runs the real /api/v1 routes on a loopback port,
drives them with the real client from clients/windows/owner_os_agent.py, and
lets a fake `claude` executable stand in for Claude Code. Nothing between the
owner's request and the "device" is stubbed, which is why it caught a bug no
unit test could: the owner-side wait used to run its blocking poll loop ON the
event loop, starving the device's own long-poll, so every command timed out
even though both halves were individually correct.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core import windows_bridge as wb


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))


def test_owner_command_does_not_block_the_event_loop(tmp_path):
    """The regression, isolated from HTTP.

    `answer()` only ever runs if the event loop is free while
    windows_command() is waiting. If the handler blocks the loop — as it did
    before asyncio.to_thread was introduced — nothing answers the command and
    this test times out, exactly as the live device did."""
    from api import v1

    device = wb.enroll(wb.create_enrollment_code()["code"], device_name="PC")
    wb.report_workspaces(device["device_id"],
                         [{"workspace_id": "gaika-basket", "state": "idle"}])

    class _Req:
        client = None
        state = type("S", (), {"auth_method": "bearer"})()
        headers: dict = {}

    async def scenario():
        async def answer():
            for _ in range(200):
                leased = await asyncio.to_thread(wb.lease, device["device_id"])
                if leased:
                    await asyncio.to_thread(
                        wb.complete, device["device_id"], leased[0]["command_id"],
                        ok=True, result={"reply": "done"})
                    return True
                await asyncio.sleep(0.05)
            return False

        task = asyncio.create_task(answer())
        req = v1.WinCommandReq(device_id=device["device_id"], action="agent.send",
                               workspace_id="gaika-basket",
                               params={"text": "hello"}, wait_secs=15)
        out = await v1.windows_command(req, _Req(), None, True)
        assert await task is True
        return out

    out = asyncio.run(asyncio.wait_for(scenario(), 25))
    assert out["timed_out"] is False
    assert out["ok"] is True
    assert out["result"]["reply"] == "done"


def test_fabric_windows_verbs_are_offloaded_but_tmux_stays_inline():
    """The offload is scoped to `win:` refs on purpose: tmux and runtime refs
    keep the exact inline path they had before this bridge existed."""
    from api import v1

    seen = {}

    def probe(marker):
        seen["marker"] = marker
        seen["thread"] = threading.current_thread().name
        return {"ok": True}

    main_thread = threading.current_thread().name
    assert asyncio.run(v1._fabric_call_async("tmux:g:0.0", probe, "tmux")) == {"ok": True}
    assert seen["thread"] == main_thread          # inline, unchanged

    assert asyncio.run(v1._fabric_call_async("win:win-1:ws", probe, "win")) == {"ok": True}
    assert seen["thread"] != main_thread          # offloaded to a worker


def test_full_simulation_enroll_send_read_replay_revoke():
    """The whole path over real HTTP (~15s — it starts a real server): mint a code, enroll a device, enroll a
    workspace locally, poll outbound, send a prompt, read the transcript,
    replay a command id, refuse an un-enrolled workspace, revoke the device."""
    import sys
    sys.path.insert(0, "tools")
    from tools import windows_bridge_sim

    out = windows_bridge_sim.run(verbose=False)
    assert out["ok"] is True
    assert out["reply"].startswith("handled:")
    assert out["device_id"].startswith("win-")
