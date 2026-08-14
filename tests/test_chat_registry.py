"""Chat registry: discovery is inventory, binding is evidence, deadness is proof.

The registry exists so the owner stops pasting URLs — but automation that can point a
project's doorbell at a chat must fail closed everywhere. These tests pin every refusal:
ambiguity, existing bindings, dead chats, malformed URLs. And they pin the one fallback
that keeps events alive when a bound chat dies: dead_route -> owner-os, named.
"""
from __future__ import annotations

import sqlite3

import pytest

from core import chat_registry as cr
from core import wake_bridge as wb
from core import wake_routes as wr

MESS = "https://chatgpt.com/c/mess-work-chat"
OWNER = "https://chatgpt.com/c/owner-os-control"


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    monkeypatch.setenv("WAKE_BRIDGE_ENABLED", "1")
    monkeypatch.setenv("WAKE_BRIDGE_KILL_SWITCH", "0")
    yield


def _seed_project(key):
    """A project the system 'knows': one event row carrying its project_id, written into
    the REAL control-plane schema (initialized first, so the shape is authentic)."""
    wr.list_routes()                         # touching the DB runs the schema init
    import os
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    conn.execute("INSERT INTO event (ts, source, type, project_id) VALUES (?,?,?,?)",
                 ("2026-08-14T00:00:00Z", "test", "test", key))
    conn.commit(); conn.close()


# ── inventory ───────────────────────────────────────────────────────────────
def test_discovery_upserts_only_chatgpt_conversations():
    tabs = [
        {"type": "page", "url": MESS, "title": "МЕССЕНДЖЕР"},
        {"type": "page", "url": "https://chatgpt.com/", "title": "ChatGPT"},
        {"type": "page", "url": "https://example.com/c/evil", "title": "not chatgpt"},
        {"type": "background_page", "url": MESS, "title": "not a page"},
    ]
    res = cr.discover_from_tabs(tabs)
    assert [d["conversation"] for d in res["discovered"]] == [MESS]
    rows = cr.list_chats()
    assert len(rows) == 1 and rows[0]["title"] == "МЕССЕНДЖЕР"


def test_first_seen_is_preserved_and_last_seen_advances():
    cr.upsert_chat(MESS, title="a", now=1000.0)
    cr.upsert_chat(MESS, title="b", now=2000.0)
    row = cr.list_chats()[0]
    assert row["title"] == "b"
    import os
    conn = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    fs, ls = conn.execute("SELECT first_seen_ts, last_seen_ts FROM chat_registry").fetchone()
    assert fs == 1000.0 and ls == 2000.0


def test_a_generic_spa_title_never_overwrites_a_real_one():
    cr.upsert_chat(MESS, title="МЕССЕНДЖЕР")
    cr.discover_from_tabs([{"type": "page", "url": MESS, "title": "ChatGPT"}])
    assert cr.list_chats()[0]["title"] == "МЕССЕНДЖЕР"


def test_the_registry_survives_a_restart_because_it_lives_in_the_database():
    cr.upsert_chat(MESS, title="МЕССЕНДЖЕР")
    import os
    fresh = sqlite3.connect(os.environ["CONTROL_PLANE_DB"])
    assert fresh.execute("SELECT title FROM chat_registry").fetchone()[0] == "МЕССЕНДЖЕР"
    fresh.close()


# ── auto-bind: deterministic or refused ─────────────────────────────────────
def test_a_single_explicit_title_marker_binds_an_unbound_route():
    _seed_project("jobhunter-ai")
    r = cr.consider_auto_bind(MESS, title="JobHunter-ai Media Start Check")
    assert r["bound"] is True and r["route_key"] == "jobhunter-ai"
    assert wr.get_route("jobhunter-ai")["conversation"] == MESS
    assert wr.get_route("jobhunter-ai")["bound_by"] == "auto-discovery"


def test_no_marker_means_no_bind():
    _seed_project("mess")
    r = cr.consider_auto_bind(MESS, title="Общий чат про всё")
    assert r["bound"] is False and r["reason"] == "no_project_marker_in_title"
    assert wr.list_routes() == []


def test_two_markers_are_ambiguous_and_refused():
    _seed_project("mess"); _seed_project("gaika-video")
    r = cr.consider_auto_bind(MESS, title="mess and gaika-video planning")
    assert r["bound"] is False and r["reason"] == "ambiguous_project_markers"
    assert sorted(r["candidates"]) == ["gaika-video", "mess"]
    assert wr.list_routes() == []


def test_a_substring_is_not_a_marker():
    """`mess` must not match `messenger` — substring matching is how a guess sneaks in."""
    _seed_project("mess")
    r = cr.consider_auto_bind(MESS, title="messenger roadmap")
    assert r["bound"] is False and r["reason"] == "no_project_marker_in_title"


def test_an_existing_route_is_never_rebound_by_discovery():
    """Newer is not evidence. Moving a project's doorbell stays an explicit owner act."""
    _seed_project("mess")
    wr.bind_route("mess", MESS)
    newer = "https://chatgpt.com/c/mess-newer-chat"
    r = cr.consider_auto_bind(newer, title="mess continuation")
    assert r["bound"] is False and r["reason"] == "route_already_bound"
    assert wr.get_route("mess")["conversation"] == MESS


def test_a_dead_chat_is_never_auto_bound():
    _seed_project("mess")
    cr.mark_dead(MESS, reason="composer_did_not_clear_after_send")
    r = cr.consider_auto_bind(MESS, title="mess planning")
    assert r["bound"] is False and r["reason"] == "chat_known_dead"


def test_the_owner_os_route_is_never_auto_bound():
    """owner-os is not a project key; it is bound only through bind_chat/the CLI."""
    r = cr.consider_auto_bind(MESS, title="owner-os control")
    assert r["bound"] is False


# ── deadness feeds routing ──────────────────────────────────────────────────
def test_a_dead_bound_route_falls_back_to_owner_os_with_a_named_reason():
    wr.bind_route("mess", MESS)
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    cr.mark_dead(MESS, reason="composer_did_not_clear_after_send")
    r = wr.resolve(project_id="mess")
    assert r["bound"] is True and r["conversation"] == OWNER
    assert r["route_key"] == wr.FALLBACK_ROUTE
    assert r["route_reason"] == "dead_route:mess"


def test_a_dead_owner_os_route_refuses_rather_than_guessing():
    wr.bind_route(wr.FALLBACK_ROUTE, OWNER)
    cr.mark_dead(OWNER, reason="composer_did_not_clear_after_send")
    r = wr.resolve(project_id="")
    assert r["bound"] is False and r["reason"] == "dead_route:owner-os"


def test_a_refused_send_marks_the_chat_dead_via_the_delivery_ledger():
    wb.record_delivery("companion", event_id=71, delivered=False,
                       reason="composer_did_not_clear_after_send",
                       conversation=MESS, route_key="mess")
    assert cr.is_dead(MESS) is True
    assert cr.list_chats()[0]["dead_reason"] == "composer_did_not_clear_after_send"


def test_a_timeout_is_not_death():
    cr.upsert_chat(MESS, title="МЕССЕНДЖЕР")
    wb.record_delivery("companion", event_id=72, delivered=False,
                       reason="cdp_error:WebSocketTimeoutException",
                       conversation=MESS, route_key="mess")
    assert cr.is_dead(MESS) is False


def test_a_verified_delivery_proves_writable_and_revives_the_row():
    cr.mark_dead(MESS, reason="composer_did_not_clear_after_send")
    wb.record_delivery("companion", event_id=73, delivered=True,
                       reason="submitted_and_user_turn_appeared",
                       conversation=MESS, route_key="mess")
    row = cr.list_chats()[0]
    assert row["writable"] is True and row["active"] is True and row["dead_reason"] == ""


# ── sidebar discovery: the account-visible surface ─────────────────────────
def test_a_chat_created_on_another_device_is_discovered_from_sidebar_links():
    """No server tab ever existed for this conversation — it arrives as a sidebar link
    once ChatGPT syncs the account, and that alone must be enough to inventory it."""
    res = cr.discover_from_links([
        {"url": "https://chatgpt.com/c/phone-created-chat", "title": "НОВЫЙ ПРОЕКТ"},
        {"url": "https://chatgpt.com/c/another-chat/", "title": "ChatGPT"},  # generic title
        {"url": "https://example.com/c/evil", "title": "x"},                 # wrong host
    ])
    urls = [d["conversation"] for d in res["discovered"]]
    assert "https://chatgpt.com/c/phone-created-chat" in urls
    assert "https://chatgpt.com/c/another-chat" in urls
    assert not any("example.com" in u for u in urls)
    rows = {r["conversation"]: r for r in cr.list_chats()}
    assert rows["https://chatgpt.com/c/phone-created-chat"]["title"] == "НОВЫЙ ПРОЕКТ"
    assert rows["https://chatgpt.com/c/phone-created-chat"]["source"] == "sidebar_discovery"


# ── curated aliases: owner vocabulary, still fail-closed ───────────────────
def test_a_curated_alias_is_strong_evidence():
    r = cr.consider_auto_bind(MESS, title="МЕССЕНДЖЕР")
    assert r["bound"] is True and r["route_key"] == "mess"


def test_the_payments_alias_binds_payment_orchestrator():
    url = "https://chatgpt.com/c/payments-chat"
    r = cr.consider_auto_bind(url, title="ПЛАТЁЖКА 2")
    assert r["bound"] is True and r["route_key"] == "payment-orchestrator"


def test_two_alias_hits_are_still_ambiguity():
    r = cr.consider_auto_bind(MESS, title="ПЛАТЁЖКА и ВИДЕО общий план")
    assert r["bound"] is False and r["reason"] == "ambiguous_project_markers"
    assert sorted(r["candidates"]) == ["gaika-video", "payment-orchestrator"]


def test_an_alias_is_token_bounded_too():
    """`видео` must not fire inside `видеонаблюдение`-style words."""
    r = cr.consider_auto_bind(MESS, title="видеонаблюдение план")
    assert r["bound"] is False and r["reason"] == "no_project_marker_in_title"


# ── combination markers: generic words only count together ─────────────────
def test_a_generic_word_alone_never_binds():
    """`корзина` names nothing by itself — that is exactly the guess the combo rule
    exists to prevent."""
    for title in ("Разработка корзины", "корзина покупок", "Сравнение корзин"):
        r = cr.consider_auto_bind(MESS, title=title)
        assert r["bound"] is False, title
        assert r["reason"] == "no_project_marker_in_title"


def test_the_gaika_basket_combination_is_strong_evidence():
    r = cr.consider_auto_bind(MESS, title="GAIKA корзина план")
    assert r["bound"] is True and r["route_key"] == "gaika-drop"


def test_the_atb_varus_basket_combination_is_strong_evidence():
    r = cr.consider_auto_bind(MESS, title="Корзины АТБ и Варус сравнение")
    assert r["bound"] is True and r["route_key"] == "gaika-drop"


def test_a_combo_plus_another_project_marker_is_still_ambiguity():
    r = cr.consider_auto_bind(MESS, title="GAIKA корзина и ВИДЕО")
    assert r["bound"] is False and r["reason"] == "ambiguous_project_markers"
    assert sorted(r["candidates"]) == ["gaika-drop", "gaika-video"]


# ── continuation: dead old + strong new = auto-rebind; healthy old = never ─
def test_a_dead_route_with_a_strong_new_chat_is_auto_rebound_with_audit():
    wr.bind_route("mess", MESS)
    cr.mark_dead(MESS, reason="composer_did_not_clear_after_send")
    newer = "https://chatgpt.com/c/mess-continued"
    r = cr.consider_auto_bind(newer, title="МЕССЕНДЖЕР продолжение")
    assert r["bound"] is True and r["continuation"] is True and r["previous"] == MESS
    assert wr.get_route("mess")["conversation"] == newer
    hist = [h for h in wr.route_history() if h["route_key"] == "mess"][0]
    assert hist["previous"] == MESS and "recorded dead" in hist["note"]


def test_a_healthy_route_is_never_replaced_by_a_newer_chat():
    wr.bind_route("mess", MESS)
    r = cr.consider_auto_bind("https://chatgpt.com/c/mess-newer",
                              title="МЕССЕНДЖЕР новый")
    assert r["bound"] is False and r["reason"] == "route_already_bound"
    assert wr.get_route("mess")["conversation"] == MESS


# ── explicit continuation rebind stays available and audited ────────────────
def test_explicit_rebind_still_moves_a_route_and_records_the_previous_chat():
    wr.bind_route("mess", MESS)
    newer = "https://chatgpt.com/c/mess-newer-chat"
    res = wr.bind_route("mess", newer, by="owner", note="old chat died; continue here")
    assert res["action"] == "rebind" and res["previous"] == MESS
    hist = [h for h in wr.route_history() if h["route_key"] == "mess"][0]
    assert hist["previous"] == MESS and "continue here" in hist["note"]
