"""The background worker loops must not do blocking I/O on the event loop.

`api/main.py` starts eight loops with `asyncio.create_task`, in the SAME process
that serves the MCP control path. Seven of them already hand every tick to
`asyncio.to_thread`. Two call sites did not, and both blocked:

* `agent_orchestrator.run_loop` called `wake_bridge.register_worker` directly —
  sqlite writes plus `_module_fingerprint`, which opens and SHA-256-hashes the
  worker's source files off disk, every 45s tick.
* `wake_bridge.pipeline_watch_loop` called `pipeline_health` directly — sqlite
  reads, every watch interval.

Both now go through `to_thread`, and these tests pin that by the only thing that
actually distinguishes the two arrangements at runtime: WHICH THREAD the call
lands on. `asyncio.run` drives the loop on the main thread, so a call executing
on the main thread is a call executing on the event loop.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from core import agent_orchestrator as ao
from core import wake_bridge as wb


def _thread_recorder(record: list, result):
    """A double that records whether it ran on the main (event-loop) thread."""

    def fn(*_a, **_k):
        record.append(threading.current_thread() is threading.main_thread())
        return result

    return fn


# ── wake_bridge.pipeline_watch_loop ─────────────────────────────────────────
def _drive_watch_loop(monkeypatch, statuses):
    on_main: list[bool] = []
    calls = {"n": 0}
    health = {"status": "ok", "reasons": ["r"], "pending_count": 1,
              "pending_oldest_age_secs": 700, "pending_oldest_route": "mess",
              "last_claim_attempt_age_secs": 5}

    def fake_health(*_a, **_k):
        on_main.append(threading.current_thread() is threading.main_thread())
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        return {**health, "status": statuses[i]}

    async def fake_sleep(_s):
        if calls["n"] >= len(statuses):
            raise asyncio.CancelledError

    monkeypatch.setattr(wb, "pipeline_health", fake_health)
    try:
        asyncio.run(wb.pipeline_watch_loop(log=lambda _l, _m: None, sleep=fake_sleep))
    except asyncio.CancelledError:
        pass
    return on_main


def test_pipeline_health_is_not_called_on_the_event_loop(monkeypatch):
    on_main = _drive_watch_loop(monkeypatch, ["ok"])
    assert on_main, "pipeline_health was never called — the loop did not run"
    assert not any(on_main), (
        "pipeline_health ran on the main thread, i.e. ON the event loop. It reads "
        "sqlite, and this loop shares its process with the MCP control path — "
        "hand it to asyncio.to_thread like every sibling call.")


def test_the_watch_loop_still_sees_a_patched_health(monkeypatch):
    """to_thread must resolve the global at call time, or the double is bypassed."""
    on_main = _drive_watch_loop(monkeypatch, ["stuck", "ok"])
    assert len(on_main) == 2, f"expected two ticks, saw {len(on_main)}"


# ── agent_orchestrator.run_loop ─────────────────────────────────────────────
def test_register_worker_is_not_called_on_the_event_loop(monkeypatch):
    """register_worker writes sqlite AND hashes source files off disk."""
    on_main: list[bool] = []
    ticks = {"n": 0}

    monkeypatch.setattr(ao, "ENABLED", True)
    monkeypatch.setattr(ao, "load_config", lambda *_a, **_k: None)
    monkeypatch.setattr(wb, "register_worker", _thread_recorder(on_main, {"ok": True}))

    def fake_refresh(*_a, **_k):
        ticks["n"] += 1
        return {}

    monkeypatch.setattr(ao, "refresh_and_resolve", fake_refresh)

    async def fake_sleep(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(ao.run_loop())
    except asyncio.CancelledError:
        pass

    assert on_main, "register_worker was never called — the loop did not run"
    assert not any(on_main), (
        "register_worker ran on the main thread, i.e. ON the event loop. It does "
        "sqlite writes and reads+hashes the worker's source files from disk, on a "
        "45s cadence, in the process that also serves the MCP control path.")


# ── the seven that were already right must stay right ───────────────────────
@pytest.mark.parametrize("module_name,loop_name", [
    ("core.agent_supervisor", "run_loop"),
    ("core.agent_orchestrator", "run_loop"),
    ("core.control_plane.engine", "run_loop"),
    ("core.agent_continuation_watchdog", "run_loop"),
    ("core.commander_autopilot", "run_loop"),
    ("core.project_supervisor", "run_loop"),
    ("core.context_budget", "run_loop"),
    ("core.wake_bridge", "pipeline_watch_loop"),
])
def test_every_worker_loop_offloads_its_tick(module_name, loop_name):
    """A structural pin: each loop body must actually CALL to_thread.

    Coarse on purpose — it cannot prove a specific call was offloaded, which is
    what the two tests above do for the two that were wrong. What it catches is a
    new loop, or a rewritten one, that goes back to calling its tick inline.

    Parsed rather than grepped. A substring check passes on a body whose only
    remaining `to_thread` is the word inside a comment — which is exactly what
    happened while writing these tests: reverting the wake_bridge fix left its
    explanatory comment behind, and a `"to_thread" in src` assertion stayed green
    over the restored defect.
    """
    import ast
    import importlib
    import inspect
    import textwrap

    mod = importlib.import_module(module_name)
    tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(mod, loop_name))))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "to_thread"]
    assert calls, (
        f"{module_name}.{loop_name} contains no call to asyncio.to_thread — "
        f"blocking work there runs on the event loop and stalls every request in "
        f"this process, the MCP control path included.")
