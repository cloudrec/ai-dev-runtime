#!/usr/bin/env python3
"""MCP side of the send-queue E2E: calls the seo MCP tools (real schemas and handlers)
against the disposable runtime. Run with the seo backend venv, cwd = seo backend.
argv[1] = step name; prints one JSON line."""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, ".")
from services import mcp_server as mcp  # noqa: E402


async def call(name, params):
    schema = {t["name"]: t["inputSchema"] for t in mcp.tool_schemas()}[name]
    extra = set(params) - set(schema["properties"])
    assert not extra, f"schema rejects {extra}"
    return await mcp.TOOLS[name]["fn"](None, params)


STEPS = {
    "old_send": ("agent_send", {"target": "toy-agent:0.0", "text": "old client",
                                "idempotency_key": "e2e-old"}),
    "queue_send": ("agent_send", {"target": "toy-agent:0.0", "text": "queued task",
                                  "idempotency_key": "e2e-q1", "queue_if_busy": True,
                                  "queue_ttl_secs": 600}),
    "queue_dup_other_text": ("agent_send", {"target": "toy-agent:0.0", "text": "other",
                                            "idempotency_key": "e2e-q1",
                                            "queue_if_busy": True}),
    "status": ("agent_send_queue_status", {"idempotency_key": "e2e-q1"}),
    "pay": ("agent_send", {"target": "pay-synthetic:0.0", "text": "x",
                           "idempotency_key": "e2e-pay", "queue_if_busy": True}),
    "queue_cancel_me": ("agent_send", {"target": "toy-agent:0.0", "text": "never",
                                       "idempotency_key": "e2e-c1", "queue_if_busy": True}),
    "cancel": ("agent_send_queue_cancel", {"idempotency_key": "e2e-c1"}),
    "status_c": ("agent_send_queue_status", {"idempotency_key": "e2e-c1"}),
}

if __name__ == "__main__":
    name, params = STEPS[sys.argv[1]]
    print(json.dumps(asyncio.run(call(name, params)), default=str))
