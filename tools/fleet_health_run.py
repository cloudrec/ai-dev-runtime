#!/usr/bin/env python3
"""Oneshot CLI entrypoint for server-down Telegram alerting. Run by
owner-os-fleet-health.service (systemd timer). See core/fleet_health.py for the
state machine and reports/SERVER_DOWN_TELEGRAM_ALERTS_2026-08-16.md for why this
exists.

Exit codes (deliberately NOT normalized to 0, matching payorch-cert-monitor.service's
reasoning: a `systemctl --failed` sighting is the backstop if Telegram itself is down):
  0 = all hosts healthy
  1 = at least one host currently down
  2 = the run itself could not evaluate (topology/config error)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fleet_health import DEFAULT_STATE_PATH, DEFAULT_TOPOLOGY_PATH, load_topology, run_once, send_telegram  # noqa: E402


def main() -> int:
    topology_path = os.getenv("FLEET_HEALTH_TOPOLOGY_PATH", DEFAULT_TOPOLOGY_PATH)
    state_path = os.getenv("FLEET_HEALTH_STATE_PATH", DEFAULT_STATE_PATH)
    try:
        hosts = load_topology(topology_path)
    except OSError as e:
        print(f"[fleet-health] could not load topology {topology_path}: {e}", file=sys.stderr)
        return 2
    if not hosts:
        print(f"[fleet-health] topology {topology_path} produced zero hosts", file=sys.stderr)
        return 2

    fail_threshold = int(os.getenv("FLEET_HEALTH_FAIL_THRESHOLD", "3"))
    recovery_threshold = int(os.getenv("FLEET_HEALTH_RECOVERY_THRESHOLD", "2"))
    reminder_interval = int(os.getenv("FLEET_HEALTH_REMINDER_INTERVAL_SECS", str(6 * 3600)))

    summary = run_once(hosts, send_fn=send_telegram, state_path=state_path,
                       fail_threshold=fail_threshold, recovery_threshold=recovery_threshold,
                       reminder_interval_secs=reminder_interval)

    for r in summary["results"]:
        alert_note = f" ALERT={r['alert']['kind']} sent={r['sent']}" if r["alert"] else ""
        print(f"[fleet-health] {r['host_id']}: {r['state']} — {r['probe']['summary']}{alert_note}")

    return 1 if summary["any_down"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
