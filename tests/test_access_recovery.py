"""Payment access-recovery classification: an SSH key/connection failure on RU-PROD/NL-edge is
INTERNAL key-selection recovery (keys already installed per authenticated owner truth), NOT an
owner credential gate. The owner is never repeatedly pinged to install keys; escalation happens
ONLY on exhaustive absence/revocation proof.
"""
from __future__ import annotations

import pytest

from core.control_plane import access_recovery as ar
from core.control_plane import event_pipeline as ep
from core.control_plane import cto
from core.control_plane import api as cp
from core import agent_control as ac


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("AGENT_CONTROL_DB", str(tmp_path / "ac.db"))
    for v in ("CONTROL_PLANE_SAMECHAT_WAKE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(v, raising=False)
    yield


# ── classification ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "ssh root@ru-prod: Permission denied (publickey).",
    "ssh: could not resolve hostname nl-edge: Name or service not known",
    "no such identity: /root/.ssh/id_ed25519_ruprod",
    "Host key verification failed for ru-prod",
    "Too many authentication failures for root@nl-edge",
])
def test_key_selection_is_internal_recovery(text):
    c = ar.classify("payment:0.0", text)
    assert c["class"] == "internal_recovery"


@pytest.mark.parametrize("text", [
    "ru-prod: all ssh keys removed from authorized_keys",
    "access permanently revoked on nl-edge",
    "account disabled for payment on ru-prod",
    "exhausted all known keys, users, aliases and configs — no identity files remaining",
])
def test_exhaustive_absence_escalates(text):
    assert ar.should_escalate("payment:0.0", text) is True
    assert ar.classify("payment:0.0", text)["class"] == "escalate"


def test_other_agent_not_reclassified():
    c = ar.classify("email:0.0", "Permission denied (publickey) on ru-prod")
    assert c["class"] == "none"


def test_non_access_text_is_none():
    assert ar.classify("payment:0.0", "compiling module foo")["class"] == "none"


# ── recovery tracking: no owner notification ─────────────────────────────────
def test_note_recovery_tracks_task_without_notifying_owner():
    t = ar.note_recovery("payment:0.0", host="ru-prod", detail="publickey denied")
    assert t["state"] == "recovering" and t["attempts"] == 1
    t2 = ar.note_recovery("payment:0.0", host="ru-prod", detail="retry")
    assert t2["attempts"] == 2                       # same task, attempt counter advances
    tasks = ar.get_recovery_tasks()
    assert tasks[0]["agent"] == "payment:0.0" and tasks[0]["host"] == "ru-prod"
    # the recorded events are inbox-only + NOT owner-actionable (owner not pinged)
    evs = [e for e in cto.cto_brief_since("t")["events"] if e["type"] == "access_recovery_in_progress"]
    assert evs and all(e["owner_action_required"] is False for e in evs)
    # NO owner-push notification was enqueued for these
    assert cp.pending_notifications() == []


def test_escalate_raises_owner_actionable_event():
    r = ar.escalate("payment:0.0", host="nl-edge", detail="all keys removed")
    assert r["state"] == "escalated" and r["event_id"] > 0
    evs = [e for e in cto.cto_brief_since("t")["events"]
           if e["type"] == "access_material_absent_or_revoked"]
    assert evs and evs[0]["owner_action_required"] is True


# ── pipeline reclassification (the anti-repeat-notify path) ──────────────────
def test_pipeline_blocker_reclassified_no_owner_event():
    r = ep.publish_significant_event(agent="payment:0.0", project="payment-orchestrator",
                                     kind="blocker",
                                     evidence={"summary": "ssh root@ru-prod Permission denied (publickey)"})
    assert r["ok"] is False and r["reason"] == "reclassified_internal_recovery"
    assert r["owner_notified"] is False and r["host"] == "ru-prod"
    # NO 'blocker' owner event was emitted, only the internal recovery record
    types = [e["type"] for e in cto.cto_brief_since("t")["events"]]
    assert "blocker" not in types and "access_recovery_in_progress" in types
    assert cp.pending_notifications() == []          # owner not pinged
    # and it is NOT mirrored to the legacy owner surface as a blocker
    assert [x for x in ac.list_commander_events(limit=50) if x["agent"] == "payment:0.0"] == []


def test_pipeline_blocker_escalates_on_exhaustive_absence():
    r = ep.publish_significant_event(agent="payment:0.0", project="payment-orchestrator",
                                     kind="blocker",
                                     evidence={"summary": "nl-edge: access permanently revoked, all keys removed"})
    assert r["ok"] is True and r["kind"] == "blocker"   # genuine escalation proceeds
    assert r["owner_action_required"] is True


def test_pipeline_non_payment_access_failure_still_owner_event():
    r = ep.publish_significant_event(agent="email:0.0", project="email", kind="blocker",
                                     evidence={"summary": "Permission denied (publickey) on ru-prod"})
    assert r["ok"] is True and r["owner_action_required"] is True   # scoped to payment only


# ── authenticated owner truth ────────────────────────────────────────────────
def test_record_owner_truth_is_trusted():
    d = ar.record_owner_truth()
    assert d["trusted"] is True and d["id"]
    evs = [e for e in cto.cto_brief_since("t")["events"] if e["type"] == "owner_truth_recorded"]
    assert evs and evs[0]["owner_action_required"] is False
