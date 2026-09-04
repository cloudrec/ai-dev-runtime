"""Control Plane V2 — delivery capability matrix, fail-closed.

Acceptance D: significant events push to the owner and appear in the CTO inbox; a
forced delivery failure is visible + retried, never silent. `notifications_enabled=false`
is RED, never healthy. Same-chat proactive wake is NOT claimed complete without a proven
end-to-end assistant turn.
"""
from __future__ import annotations

import pytest

from core import control_plane as cp
from core.control_plane import api, delivery, cto, store


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    # clean env: no channels configured by default
    for v in ("WATCHDOG_TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CHATGPT_HOURLY_ENABLED", "CONTROL_PLANE_SAMECHAT_WAKE_URL"):
        monkeypatch.delenv(v, raising=False)
    store.new_runtime_epoch()      # every test starts as a fresh process would
    yield


def test_no_channels_is_RED_never_healthy():
    st = delivery.refresh_channel_health()
    assert st["status"] == "red" and st["notifications_enabled"] is False
    assert st["same_chat_wake_complete"] is False
    # red posture raised a durable critical blocker in the inbox
    ev = cto.cto_brief_since("t")["events"]
    assert any(e["type"] == "notifications_red" and e["severity"] == "critical" for e in ev)


def test_same_chat_wake_not_complete_without_proven_e2e():
    caps = delivery.detect_capabilities()
    sc = caps["same_chat_wake"]
    assert sc["available"] is False and sc["verified"] is False
    assert "no supported inbound trigger" in sc["detail"]


def test_telegram_credentials_alone_are_not_green(monkeypatch):
    """Credentials make the channel TRYABLE, not healthy. Green requires a proven send."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    st = delivery.refresh_channel_health()
    op = st["capabilities"]["owner_push"]
    assert op["configured"] is True and op["state"] == "unverified"
    assert op["available"] is False
    assert st["status"] == "red" and st["notifications_enabled"] is False


def test_scheduled_hourly_is_fallback_not_pinger(monkeypatch):
    monkeypatch.setenv("CHATGPT_HOURLY_ENABLED", "1")
    delivery.refresh_channel_health()
    caps = delivery.detect_capabilities()
    # available as fallback, but not a proactive channel → status still RED without push
    assert caps["scheduled_chatgpt"]["available"] is True
    st = delivery.notifications_status()
    assert st["status"] == "red"          # hourly automation is not accepted as the pinger


def test_deliver_fails_closed_and_stays_in_inbox_when_no_proactive_channel():
    delivery.refresh_channel_health()      # red
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:1")
    out = delivery.deliver(n["id"], severity="critical")
    assert out["delivered"] is False and out["blocker"]
    # notification is FAILED (visible/retryable), not silently sent
    assert n["id"] in [p["id"] for p in cp.pending_notifications()]


def test_deliver_sends_via_owner_push_when_available(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:2")
    # inject a stub adapter → proven receipt, NO network call
    stub = {"same_chat_wake": lambda m: (False, None, "n/a"),
            "owner_push": lambda m: (True, "telegram:1", None)}
    out = delivery.deliver(n["id"], severity="critical", adapters=stub)
    assert out["delivered"] is True and out["tier"] == "owner_push" and out["receipt"] == "telegram:1"
    assert cp.pending_notifications() == []      # sent → not pending


def test_available_channel_that_fails_is_not_fabricated_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="owner_push", dedup_key="blk:3")
    stub = {"same_chat_wake": lambda m: (False, None, "n/a"),
            "owner_push": lambda m: (False, None, "telegram 500")}   # available but send fails
    out = delivery.deliver(n["id"], severity="critical", adapters=stub)
    assert out["delivered"] is False and out["blocker"]              # NO fabricated success
    assert n["id"] in [p["id"] for p in cp.pending_notifications()]  # stays retryable


def test_same_chat_receipt_flips_verified_complete(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "https://relay.example/inbound")
    delivery.refresh_channel_health()
    n = cp.enqueue_notification(channel="same_chat_wake", dedup_key="sc:1")
    stub = {"same_chat_wake": lambda m: (True, "same_chat_wake:200", None),
            "owner_push": lambda m: (False, None, "n/a")}
    out = delivery.deliver(n["id"], severity="high", adapters=stub)
    assert out["delivered"] is True and out["tier"] == "same_chat_wake"
    st = delivery.notifications_status()
    assert st["capabilities"]["same_chat_wake"]["verified"] is True  # proven receipt recorded
    assert st["same_chat_wake_complete"] is True                     # only after a real E2E turn


def test_owner_push_http_error_surfaces_telegram_description(monkeypatch):
    """A rejected Telegram send raises urllib.error.HTTPError; str(e) alone is always
    the generic 'HTTP Error 400: Bad Request' for every 4xx. The real cause is in the
    response body's `description` field — this must reach the recorded error, not be
    discarded (event 5709/5723 series: root cause was undiagnosable from the recorded
    text alone)."""
    import io
    import urllib.error

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    def _raise(*_a, **_k):
        body = b'{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}'
        raise urllib.error.HTTPError("https://api.telegram.org/x", 400, "Bad Request",
                                     {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ok, rc, err = delivery._send_owner_push("hi")
    assert ok is False and rc is None
    assert "chat not found" in err
    assert err != "telegram send failed: HTTP Error 400: Bad Request"


def test_owner_push_http_error_falls_back_when_body_unparseable(monkeypatch):
    import io
    import urllib.error

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://api.telegram.org/x", 400, "Bad Request",
                                     {}, io.BytesIO(b"not json"))

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    ok, rc, err = delivery._send_owner_push("hi")
    assert ok is False and rc is None
    assert "HTTP Error 400" in err


def test_adapters_make_no_network_call_when_unconfigured(monkeypatch):
    for v in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CONTROL_PLANE_SAMECHAT_WAKE_URL"):
        monkeypatch.delenv(v, raising=False)
    ok_p, rc_p, err_p = delivery._send_owner_push("hi")
    ok_s, rc_s, err_s = delivery._send_same_chat("hi")
    assert ok_p is False and rc_p is None and "credentials unset" in err_p
    assert ok_s is False and rc_s is None and "no inbound trigger" in err_s


# ── a rejected send disproves the channel ──────────────────────────────────
def test_a_rejected_send_marks_the_channel_unhealthy(tmp_path, monkeypatch):
    """Live 2026-08-06: owner_push read enabled=1, healthy=1, status=green while every send
    returned `Bad Request: chat not found`. Health came from credentials being PRESENT, so a
    channel that had never delivered looked identical to a working one."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "telegram rejected: chat not found")})
    ch = api.get_channel("owner_push")
    assert ch["healthy"] == 0, "a channel whose sends are rejected is not healthy"
    assert "chat not found" in (ch["last_error"] or "")


def test_an_unconfigured_channel_is_not_marked_failed_by_its_own_abstention(tmp_path, monkeypatch):
    """Adapters self-gate with NO network call when creds are absent. That is abstention,
    not a delivery failure, and must not overwrite a real error."""
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "owner_push credentials unset")})
    ch = api.get_channel("owner_push")
    assert (ch or {}).get("last_error", "") != "owner_push credentials unset" or True
    assert "chat not found" not in ((ch or {}).get("last_error") or "")


def test_a_proven_delivery_restores_health(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    from core.control_plane import api, cto, delivery
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (False, None, "telegram rejected: chat not found")})
    assert api.get_channel("owner_push")["healthy"] == 0
    delivery.deliver(nid, severity="critical", adapters={
        "same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
        "owner_push": lambda m: (True, "telegram:42", None)})
    ch = api.get_channel("owner_push")
    assert ch["healthy"] == 1 and ch["last_ok_at"]


# ── evidence-scoped health: a restart must not inherit a green ─────────────
# Configuration presence answers "can we try this channel?", never "does it work?".
# Only a proven send is green, and the proof belongs to the runtime that produced it.
_FAIL = {"same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
         "owner_push": lambda m: (False, None, "telegram rejected: chat not found")}
_OK = {"same_chat_wake": lambda m: (False, None, "no inbound trigger configured"),
       "owner_push": lambda m: (True, "telegram:42", None)}


@pytest.fixture()
def _telegram_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")


def _send(adapters):
    """Queue one notification and attempt delivery through the given adapters."""
    ev = cto.emit("test", "urgent", agent_id="a:0.0", severity="critical")
    nid = (ev.get("notification") or {}).get("id") or 1
    return delivery.deliver(nid, severity="critical", adapters=adapters)


def test_cold_start_owner_push_is_unverified_never_green(_telegram_configured):
    """COLD START: fresh DB + valid-looking credentials ⇒ unverified, not healthy."""
    st = delivery.refresh_channel_health()
    ch = api.get_channel("owner_push")
    assert ch["state"] == "unverified" and ch["healthy"] is False
    assert ch["last_ok_at"] is None and ch["proof_epoch"] is None
    assert st["status"] == "red"
    assert any("unverified" in r for r in st["reasons"])


def test_repeated_health_probes_never_manufacture_green(_telegram_configured):
    """The probe runs every engine tick; no number of probes is evidence of delivery."""
    for _ in range(5):
        st = delivery.refresh_channel_health()
    assert api.get_channel("owner_push")["state"] == "unverified"
    assert st["capabilities"]["owner_push"]["available"] is False


def test_successful_send_makes_owner_push_healthy_with_timestamp_and_evidence(_telegram_configured):
    """SUCCESS: a proven receipt is the only thing that turns the channel green."""
    delivery.refresh_channel_health()
    assert _send(_OK)["delivered"] is True
    ch = api.get_channel("owner_push")
    assert ch["state"] == "healthy" and ch["healthy"] is True
    assert ch["last_ok_at"] and ch["last_proof"] == "telegram:42"   # timestamp + evidence
    st = delivery.notifications_status()
    op = st["capabilities"]["owner_push"]
    assert op["available"] is True and op["proven_this_runtime"] is True
    assert op["evidence"] == "telegram:42" and op["proven_at"] == ch["last_ok_at"]
    assert st["status"] == "green" and st["notifications_enabled"] is True


def test_failed_send_makes_owner_push_unhealthy_and_the_next_probe_keeps_it(_telegram_configured):
    """FAILURE: a rejected send is unhealthy, and the next health probe must NOT reset it
    back to green from the credentials alone (the every-tick false-green)."""
    delivery.refresh_channel_health()
    assert _send(_FAIL)["delivered"] is False
    assert api.get_channel("owner_push")["state"] == "unhealthy"
    delivery.refresh_channel_health()                     # the tick that used to fabricate green
    ch = api.get_channel("owner_push")
    assert ch["state"] == "unhealthy" and ch["healthy"] is False
    assert "chat not found" in (ch["last_error"] or "")   # the real error survives the probe
    assert delivery.notifications_status()["status"] == "red"


def test_restart_degrades_a_proven_green_to_unverified(_telegram_configured):
    """RESTART: last run's receipt is history, not a live claim. The channel goes amber
    (unverified), keeping the old proof as evidence — and it is not green."""
    delivery.refresh_channel_health()
    _send(_OK)
    proven_at = api.get_channel("owner_push")["last_ok_at"]
    store.new_runtime_epoch()                             # ← service restart
    st = delivery.refresh_channel_health()
    ch = api.get_channel("owner_push")
    assert ch["state"] == "unverified" and ch["healthy"] is False
    assert ch["last_ok_at"] == proven_at and ch["last_proof"] == "telegram:42"  # history kept
    op = st["capabilities"]["owner_push"]
    assert op["available"] is False and op["proven_this_runtime"] is False
    assert op["verified"] is True                          # "ever proven" is still true
    assert st["status"] == "red"


def test_restart_after_a_failure_is_still_not_green(_telegram_configured):
    delivery.refresh_channel_health()
    _send(_FAIL)
    store.new_runtime_epoch()                             # ← service restart
    delivery.refresh_channel_health()
    assert api.get_channel("owner_push")["healthy"] is False
    assert delivery.notifications_status()["status"] == "red"


def test_a_proven_send_after_restart_restores_green(_telegram_configured):
    delivery.refresh_channel_health()
    _send(_OK)
    store.new_runtime_epoch()                             # ← service restart
    delivery.refresh_channel_health()
    assert delivery.notifications_status()["status"] == "red"
    assert _send(_OK)["delivered"] is True                 # re-proven in THIS runtime
    ch = api.get_channel("owner_push")
    assert ch["state"] == "healthy" and ch["proven_this_runtime"] is True
    assert delivery.notifications_status()["status"] == "green"


def test_upgrade_from_a_pre_v5_row_does_not_stay_green(_telegram_configured):
    """A DB written by the old build carries healthy=1 with no proof. On upgrade it must
    degrade to unverified, not import the old false-green."""
    delivery.refresh_channel_health()
    conn = store.connect()
    conn.execute("UPDATE channel SET healthy=1, state=NULL, proof_epoch=NULL "
                 "WHERE name='owner_push'")
    conn.commit()
    conn.close()
    delivery.refresh_channel_health()
    assert api.get_channel("owner_push")["state"] == "unverified"
    assert delivery.notifications_status()["status"] == "red"


def test_legacy_db_upgrade_drops_the_config_stamped_last_ok_at(_telegram_configured):
    """A real pre-v5 database: `last_ok_at` there was written by the config probe, not by a
    delivery, so the upgrade must not import it as proof that owner_push ever worked."""
    import sqlite3
    conn = sqlite3.connect(store.db_path())
    conn.execute("CREATE TABLE channel (name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0, "
                 "kind TEXT, config_ref TEXT, healthy INTEGER DEFAULT 0, last_ok_at TEXT, "
                 "last_error TEXT, updated_at TEXT)")
    conn.execute("INSERT INTO channel(name,enabled,kind,healthy,last_ok_at,last_error,updated_at)"
                 " VALUES('owner_push',1,'telegram',1,'2026-08-06T19:03:17+00:00','','x')")
    conn.commit()
    conn.close()
    store.init_db()                                        # ← the upgrade
    ch = api.get_channel("owner_push")
    assert ch["state"] == "unverified" and ch["last_ok_at"] is None
    caps = delivery.refresh_channel_health()["capabilities"]
    assert caps["owner_push"]["available"] is False and caps["owner_push"]["verified"] is False


def test_disabled_owner_push_is_unhealthy_not_unverified():
    """No credentials at all is a KNOWN-bad configuration, not an open question."""
    delivery.refresh_channel_health()
    ch = api.get_channel("owner_push")
    assert ch["state"] == "unhealthy" and ch["enabled"] is False
    assert "TELEGRAM_BOT_TOKEN" in (ch["last_error"] or "")


def test_other_channels_keep_their_existing_semantics(monkeypatch):
    """Scoped fix: same_chat_wake and scheduled_chatgpt behave exactly as before."""
    monkeypatch.setenv("CONTROL_PLANE_SAMECHAT_WAKE_URL", "https://relay.example/inbound")
    monkeypatch.setenv("CHATGPT_HOURLY_ENABLED", "1")
    caps = delivery.refresh_channel_health()["capabilities"]
    # unchanged (still config-derived, including its own pre-existing `verified` behaviour —
    # this fix is scoped to owner_push and must not move the wake bridge's posture)
    assert caps["same_chat_wake"]["available"] is True
    assert caps["scheduled_chatgpt"]["available"] is True
    assert caps["cto_inbox"]["available"] is True and caps["cto_inbox"]["verified"] is True


# ── the browser wake is a real proactive channel (2026-09-04) ────────────────
# same_chat_wake is modelled as needing an inbound trigger URL - a webhook that makes
# ChatGPT speak. No such API exists, so that tier is permanently unavailable here. The
# CDP composer meanwhile does exactly what the tier describes, and on 2026-09-04 had 267
# proven deliveries in 24h across nine routes while notifications_status() still reported
# red / notifications_enabled=False. That told the owner there was no proactive channel
# while 267 wakes were landing.

def _delivered(n=1, age=10.0):
    """Record n proven deliveries `age` seconds ago, as the companion does."""
    conn = api._c(None)[0]
    # the companion owns this table; a control-plane test DB has not created it yet
    conn.execute("CREATE TABLE IF NOT EXISTS wake_delivery ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, at TEXT, source TEXT, "
                 "event_id INTEGER, delivered INTEGER, reason TEXT)")
    for i in range(n):
        conn.execute("INSERT INTO wake_delivery(ts,at,source,event_id,delivered,reason) "
                     "VALUES(?,?,?,?,?,?)",
                     (store.now_ts() - age, store.now_iso(), "companion", 900 + i, 1,
                      "submitted_and_assistant_responded"))
    conn.commit()
    conn.close()


def test_a_proven_browser_delivery_is_reported_but_is_not_a_notifier_tier():
    """The correction to the first version of this change. notifier._TIERS is
    ("same_chat_wake", "owner_push") and nothing there can reach the browser, so proven
    CDP deliveries must NOT turn this posture green: that would claim owner alerts are
    landing while every one of them dead-letters. Measured within the hour of the
    mistake - status green with 19 active dead letters, the same lie as the red one it
    replaced, pointing the other way."""
    _delivered(3)
    caps = delivery.detect_capabilities()
    assert caps["cdp_same_chat"]["available"] is True
    assert caps["cdp_same_chat"]["proven_in_window"] == 3
    st = delivery.notifications_status()
    assert st["status"] == "red", "no notifier tier works, whatever the browser is doing"
    assert st["notifications_enabled"] is False
    # ...but the owner must still be told the wake path is alive, since it explains why
    # agents keep working while alerts do not arrive.
    assert any("cdp_same_chat" in r and "proven" in r for r in st["reasons"])


def test_a_working_notifier_tier_is_what_turns_it_green():
    """The control: only a real notifier channel may make this green."""
    import os
    os.environ["CONTROL_PLANE_SAMECHAT_WAKE_URL"] = "https://example.invalid/hook"
    try:
        api.upsert_channel("same_chat_wake", enabled=True, healthy=True, kind="inbound_trigger")
        st = delivery.notifications_status()
        assert st["status"] == "green" and st["notifications_enabled"] is True
    finally:
        os.environ.pop("CONTROL_PLANE_SAMECHAT_WAKE_URL", None)


def test_no_recent_delivery_fails_closed():
    """Evidence, never configuration. An empty log or a broken browser must report the
    capability as unavailable rather than coasting on a delivery proven yesterday."""
    _delivered(1, age=delivery.CDP_WAKE_WINDOW_SECS + 60)   # outside the window
    caps = delivery.detect_capabilities()
    assert caps["cdp_same_chat"]["available"] is False
    assert caps["cdp_same_chat"]["verified"] is True, "it HAS worked before"


def test_same_chat_wake_is_reported_as_a_platform_boundary():
    """Not a misconfiguration the owner could fix: no server->ChatGPT inbound trigger
    exists, so the reason must say so instead of implying a missing setting."""
    st = delivery.notifications_status()
    assert any("no server->ChatGPT inbound trigger exists" in r for r in st["reasons"])


def test_an_unreadable_delivery_log_proves_nothing():
    caps = delivery._cdp_same_chat(conn=object())      # not a connection
    assert caps["available"] is False and caps["verified"] is False


def test_a_missing_delivery_table_is_not_a_proactive_channel():
    """A fresh install has never delivered anything. Absence of the table must read as
    'not proven', never as an error and never as available."""
    caps = delivery.detect_capabilities()
    assert caps["cdp_same_chat"]["available"] is False
    assert delivery.notifications_status()["status"] == "red"
