#!/usr/bin/env python3
"""One supervision pass, run detached from a lifecycle hook.

The companion's tick still runs this on a timer; this entry point exists so a turn
boundary does not have to WAIT for that timer. It is deliberately tiny and silent: it is
spawned by a hook, so anything it prints or raises would be noise in an agent's terminal.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/root/ai-dev-runtime")


def main() -> int:
    try:
        sys.path.insert(0, "/root/ai-dev-runtime/hooks")
        from owneros_hook import _load_runtime_env
        _load_runtime_env()
        from core import native_supervisor as ns
        ns.scan()
    except Exception:  # noqa: BLE001 — the polled tick is the fallback
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
