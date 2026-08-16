"""Server-down Telegram alerting for the managed payment fleet.

Root cause this exists to fix: the FI/NL WireGuard outage on 2026-08-16
(`/opt/payment-orchestrator/reports/INCIDENT_NL_EDGE_WG_504_2026-08-16.md`) produced
NO Telegram alert because no host/edge health-to-Telegram monitor existed anywhere.
`payorch-cert-monitor.service` and `payorch-replication-monitor.service` are explicit
that "no approved ops channel exists yet" and write only to the journal/state files.
This module is that missing channel, reusing the SAME Telegram transport Owner OS
already has proven working (`core.control_plane.delivery._send_owner_push`,
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `configs/.env`) instead of a new bot.

State is a small JSON file, deliberately NOT `control_plane.db` — this monitor is not
one of the Owner OS agent-control-plane workers and has no reason to take the
advisory-lock path those 11 workers share; keeping its state file-private avoids any
lock contention with them entirely.

Design (anti-flap + dedupe):
  - a host must fail `fail_threshold` consecutive probes before it is declared DOWN
    and alerted (default 3) — a single blip never pages;
  - a DOWN host must pass `recovery_threshold` consecutive probes before RECOVERED is
    sent (default 2) — flapping back to a single good probe does not clear the alert;
  - while still down, at most one reminder per `reminder_interval_secs` (default 6h) —
    conservative, no per-probe-interval spam;
  - `first_fail_ts` is stamped at the FIRST failing probe of the streak, so the DOWN
    alert's "first failure" time predates the alert itself by up to the threshold
    window, and RECOVERED reports true outage duration, not alert-to-alert duration.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

DEFAULT_TOPOLOGY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                     "config", "fleet_topology.yaml")
DEFAULT_STATE_PATH = os.getenv("FLEET_HEALTH_STATE_PATH",
                               "/root/ai-dev-runtime/state/fleet_health_state.json")

DEFAULT_FAIL_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 2
DEFAULT_REMINDER_INTERVAL_SECS = 6 * 3600
DEFAULT_PROBE_TIMEOUT_SECS = 8


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_duration(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


# --------------------------------------------------------------------------- topology

def load_topology(path: str = DEFAULT_TOPOLOGY_PATH) -> list:
    """Load the fleet list from YAML without a hard PyYAML dependency (Owner OS venv
    may not have it). The file's structure is a small, fixed shape, so a minimal
    parser is safer than adding a new dependency for one config file."""
    hosts = []
    host = None
    check = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent == 0 and stripped == "hosts:":
                continue
            if indent == 2 and stripped.startswith("- id:"):
                if host is not None:
                    hosts.append(host)
                host = {"id": stripped.split(":", 1)[1].strip(), "checks": []}
                check = None
                continue
            if host is None:
                continue
            if indent == 4 and stripped.startswith("checks:"):
                continue
            if indent == 4 and ":" in stripped:
                k, v = stripped.split(":", 1)
                host[k.strip()] = v.strip().strip('"').strip("'")
                continue
            if indent == 6 and stripped.startswith("- type:"):
                check = {"type": stripped.split(":", 1)[1].strip()}
                host["checks"].append(check)
                continue
            if indent == 8 and check is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                check[k.strip()] = v.strip().strip('"').strip("'")
                continue
        if host is not None:
            hosts.append(host)
    return hosts


# ----------------------------------------------------------------------------- probes

def _check_tcp(ip: str, port: int, timeout: float) -> tuple:
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True, f"tcp:{port} open"
    except OSError as e:
        return False, f"tcp:{port} failed: {e}"


def _check_http(ip: str, port: int, path: str, host_header: str, scheme: str,
                expect_status: int, timeout: float) -> tuple:
    """Service-aware probe: connects to the IP directly (never a DNS/GeoDNS hop) but
    sends the real Host/SNI, so it exercises the actual vhost/TLS the way
    `docs/INFRASTRUCTURE_TOPOLOGY.md` verified the edges (`--resolve`-style checks),
    not just "is the port open"."""
    cmd = [
        "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "--max-time", str(int(timeout)),
        "--resolve", f"{host_header}:{port}:{ip}",
        f"{scheme}://{host_header}:{port}{path}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return False, f"{scheme}:{path} timed out"
    except FileNotFoundError:
        return False, "curl not available on this host"
    code = (r.stdout or "").strip()
    if r.returncode != 0:
        return False, f"{scheme}:{path} curl exit {r.returncode}: {(r.stderr or '').strip()[:160]}"
    if code != str(expect_status):
        return False, f"{scheme}:{path} -> {code or 'no response'} (expected {expect_status})"
    return True, f"{scheme}:{path} -> {code}"


def probe_host(host: dict, *, timeout: float = DEFAULT_PROBE_TIMEOUT_SECS) -> dict:
    """Run every configured check for one host. DOWN only if ALL checks fail — a host
    with several checks that partially fail is degraded, not necessarily down, and this
    module alerts on down/recovered, not on degradation."""
    ip = host["ip"]
    results = []
    for chk in host.get("checks", []):
        ctype = chk.get("type")
        if ctype == "tcp":
            ok, detail = _check_tcp(ip, int(chk.get("port", 22)), timeout)
        elif ctype in ("http", "https"):
            ok, detail = _check_http(
                ip, int(chk.get("port", 443 if ctype == "https" else 80)),
                chk.get("path", "/"), chk.get("host") or host.get("hostname") or ip,
                ctype, int(chk.get("expect_status", 200)), timeout)
        else:
            ok, detail = False, f"unknown check type {ctype!r}"
        results.append({"ok": ok, "detail": detail})
    any_ok = any(r["ok"] for r in results) if results else False
    return {
        "ok": any_ok,
        "checks": results,
        "summary": "; ".join(r["detail"] for r in results) or "no checks configured",
    }


# --------------------------------------------------------------------- state machine

def _default_state() -> dict:
    return {"state": "unknown", "consecutive_fail": 0, "consecutive_ok": 0,
            "first_fail_ts": None, "last_alert_ts": None, "last_detail": ""}


def evaluate_host(host_id: str, probe_result: dict, prior: Optional[dict], *,
                  now_ts: float, fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
                  recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
                  reminder_interval_secs: int = DEFAULT_REMINDER_INTERVAL_SECS) -> tuple:
    """Pure state transition: (new_state_dict, alert_or_None). `alert` is
    {"kind": "down"|"recovered"|"reminder", "detail": str, "first_fail_ts": float,
    "duration_secs": float} when an alert should fire, else None."""
    st = dict(prior) if prior else _default_state()
    ok = probe_result["ok"]
    detail = probe_result["summary"]
    st["last_detail"] = detail
    alert = None

    if ok:
        st["consecutive_ok"] = st.get("consecutive_ok", 0) + 1
        st["consecutive_fail"] = 0
        if st["state"] == "down" and st["consecutive_ok"] >= recovery_threshold:
            first_fail = st.get("first_fail_ts")
            duration = now_ts - first_fail if first_fail else 0.0
            alert = {"kind": "recovered", "detail": detail,
                     "first_fail_ts": first_fail, "duration_secs": duration}
            st["state"] = "up"
            st["first_fail_ts"] = None
            st["last_alert_ts"] = now_ts
        elif st["state"] != "down":
            st["state"] = "up"
            st["first_fail_ts"] = None
    else:
        st["consecutive_fail"] = st.get("consecutive_fail", 0) + 1
        st["consecutive_ok"] = 0
        if st["state"] != "down":
            if not st.get("first_fail_ts"):
                st["first_fail_ts"] = now_ts
            if st["consecutive_fail"] >= fail_threshold:
                alert = {"kind": "down", "detail": detail,
                         "first_fail_ts": st["first_fail_ts"],
                         "duration_secs": now_ts - st["first_fail_ts"]}
                st["state"] = "down"
                st["last_alert_ts"] = now_ts
        else:
            last_alert = st.get("last_alert_ts") or 0
            if now_ts - last_alert >= reminder_interval_secs:
                first_fail = st.get("first_fail_ts") or now_ts
                alert = {"kind": "reminder", "detail": detail,
                         "first_fail_ts": first_fail,
                         "duration_secs": now_ts - first_fail}
                st["last_alert_ts"] = now_ts

    return st, alert


def format_alert(host: dict, alert: dict) -> str:
    label = host.get("label", host.get("id"))
    ip = host.get("ip", "?")
    role = host.get("role", "")
    kind = alert["kind"]
    header = {"down": "DOWN", "recovered": "RECOVERED", "reminder": "STILL DOWN"}[kind]
    lines = [f"[fleet-health] {header}: {label} ({ip}){' - ' + role if role else ''}"]
    if kind == "down":
        lines.append(f"First failure: {_iso(alert['first_fail_ts'])}")
        lines.append(f"Failed probes: {alert['detail']}")
    elif kind == "reminder":
        lines.append(f"Down since: {_iso(alert['first_fail_ts'])} "
                     f"(ongoing {_fmt_duration(alert['duration_secs'])})")
        lines.append(f"Failed probes: {alert['detail']}")
    else:  # recovered
        lines.append(f"Down since: {_iso(alert['first_fail_ts'])}")
        lines.append(f"Recovered after: {_fmt_duration(alert['duration_secs'])}")
        lines.append(f"Last probe: {alert['detail']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- state io

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------- run

def run_once(hosts: list, *, probe_fn: Callable = probe_host,
            send_fn: Optional[Callable[[str], tuple]] = None,
            state_path: str = DEFAULT_STATE_PATH,
            now_fn: Callable[[], float] = time.time,
            fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
            recovery_threshold: int = DEFAULT_RECOVERY_THRESHOLD,
            reminder_interval_secs: int = DEFAULT_REMINDER_INTERVAL_SECS) -> dict:
    """Probe every host, update state, send any alerts that should fire. Returns a
    summary dict for logging/tests. Never raises on a probe failure — a probe failure
    IS the signal being monitored for."""
    state = load_state(state_path)
    results = []
    any_down = False
    for host in hosts:
        hid = host["id"]
        try:
            probe_result = probe_fn(host)
        except Exception as e:  # noqa: BLE001 — a probe crash must not stop the run
            probe_result = {"ok": False, "checks": [], "summary": f"probe error: {e}"}
        new_state, alert = evaluate_host(hid, probe_result, state.get(hid),
                                         now_ts=now_fn(), fail_threshold=fail_threshold,
                                         recovery_threshold=recovery_threshold,
                                         reminder_interval_secs=reminder_interval_secs)
        state[hid] = new_state
        sent = None
        if alert is not None:
            msg = format_alert(host, alert)
            if send_fn is not None:
                sent = send_fn(msg)
            else:
                sent = (False, None, "no send_fn configured")
        if new_state["state"] == "down":
            any_down = True
        results.append({"host_id": hid, "state": new_state["state"],
                        "probe": probe_result, "alert": alert, "sent": sent})
    save_state(state_path, state)
    return {"results": results, "any_down": any_down}


def send_telegram(message: str) -> tuple:
    """The one real transport this module uses — Owner OS's already-proven Telegram
    push (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), not a second bot."""
    from core.control_plane.delivery import _send_owner_push
    return _send_owner_push(message)
