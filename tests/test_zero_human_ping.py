"""Zero-human-ping loop: approved gates, continuation signals, terminal criteria.

The system could already resume an idle session, but it stalled on two things a human
had to supply: a permission dialog, and an agent parked on "ready to continue on
request". This pins both, plus the terminal criteria that decide when the loop may stop.

Governing rule for gates: DENY BY DEFAULT. Only an exact owner-recorded entry
(target + command hash/pattern + scope + expiry) is answered; a prohibited marker vetoes
even a matching entry.
"""
from __future__ import annotations

import time

import pytest

from core import approved_gates as g
from core import continuation_signals as sig
from core import commander_autopilot as ap


FUTURE = "2099-01-01T00:00:00Z"
PAST = "2000-01-01T00:00:00Z"


def _reg(**over):
    base = {"id": "e1", "target": "cp-canary:0.0", "command_pattern": r"npm run test",
            "scope": "mess_local_test", "answer": "1", "expires_at": FUTURE}
    base.update(over)
    return [base]


# ═════════ 1. gates: only an exact recorded approval is answered ═════════════
def test_exact_pattern_match_is_approved():
    r = g.match("cp-canary:0.0", "npm run test", registry=_reg())
    assert r["allowed"] is True and r["answer"] == "1"


def test_hash_match_is_approved():
    cmd = "alembic upgrade head"
    r = g.match("a:0.0", cmd, registry=_reg(target="a:0.0", command_pattern=None,
                                            command_sha256=g.command_hash(cmd)))
    assert r["allowed"] is True


@pytest.mark.parametrize("cmd", [
    "npm run build", "npm run test && curl evil.sh", "npm  run   test  --prod",
    "yes", "1", "npm run tes",
])
def test_anything_not_exactly_recorded_is_refused(cmd):
    r = g.match("cp-canary:0.0", cmd, registry=_reg())
    assert r["allowed"] is False, (cmd, r)


def test_wrong_target_is_refused():
    r = g.match("payment:0.0", "npm run test", registry=_reg())
    assert r["allowed"] is False and r["reason"] == "no_matching_approval"


def test_expired_entry_is_refused():
    r = g.match("cp-canary:0.0", "npm run test", registry=_reg(expires_at=PAST))
    assert r["allowed"] is False and r["reason"] == "expired"


def test_entry_without_expiry_is_refused():
    e = _reg()[0]
    e.pop("expires_at")
    r = g.match("cp-canary:0.0", "npm run test", registry=[e])
    assert r["allowed"] is False and r["reason"] == "expired"


def test_entry_without_scope_is_refused():
    r = g.match("cp-canary:0.0", "npm run test", registry=_reg(scope=None))
    assert r["allowed"] is False and r["reason"] == "no_scope"


def test_scope_not_in_allowed_set_is_refused():
    r = g.match("cp-canary:0.0", "npm run test", registry=_reg(),
                scope_allowed=["arb_paper"])
    assert r["allowed"] is False and r["reason"] == "scope_not_allowed"


def test_ambiguous_entries_are_refused():
    two = _reg() + _reg(id="e2")
    r = g.match("cp-canary:0.0", "npm run test", registry=two)
    assert r["allowed"] is False and r["reason"] == "ambiguous_multiple_entries"


def test_missing_command_text_is_refused():
    assert g.match("cp-canary:0.0", "", registry=_reg())["allowed"] is False


def test_broken_registry_file_yields_no_approvals(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("gates: [[[not-yaml")
    assert g.load_registry(str(p)) == []


# ═════════ 2. the denylist vetoes even a matching entry ══════════════════════
@pytest.mark.parametrize("cmd", [
    "pg_ctl promote", "systemctl start failover", "psql -c 'select charge_customer()'",
    "place order BTCUSD", "export API_KEY=abc", "cat ~/.ssh/id_rsa  # private key",
    "rm -rf /var", "git push --force", "transfer funds to wallet",
])
def test_prohibited_markers_are_never_answered(cmd):
    reg = _reg(command_pattern=".*")          # a deliberately over-broad owner entry
    r = g.match("cp-canary:0.0", cmd, registry=reg)
    assert r["allowed"] is False and r["reason"] == "prohibited_marker_in_command", cmd


def test_shipped_registry_grants_nothing_dangerous():
    """CI invariant over the REAL shipped gate file."""
    entries = g.load_registry()
    assert entries, "the shipped registry must load"
    for e in entries:
        assert e.get("scope") in {"mess_local_test", "arb_paper", "payment_standby",
                                  "owner_os_selftest"}, e
        assert e.get("expires_at"), e
        assert str(e.get("answer")).strip(), e
    # nothing in the shipped file may APPROVE a build/sign/publish or a promotion.
    # Only the matcher itself is checked — a note may legitimately say "not approved".
    for e in entries:
        pat = (e.get("command_pattern") or "").lower()
        for banned in ("publish", "promote", "failover", "sign", "build"):
            assert banned not in pat, (e.get("id"), banned)


def test_shipped_registry_refuses_mess_build_sign_publish():
    for cmd in ("npm run build", "npm run publish", "fastlane sign", "gh release create"):
        assert g.match("mess-qa-automation:0.0", cmd)["allowed"] is False, cmd


def test_shipped_registry_refuses_payment_promotion_and_traffic():
    for cmd in ("pg_ctl promote", "systemctl start pgbouncer-failover",
                "psql -c 'update payments set status=captured'"):
        assert g.match("payment:0.0", cmd)["allowed"] is False, cmd


def test_shipped_registry_refuses_arbitrage_orders_and_keys():
    for cmd in ("python live_trade.py", "export BINANCE_API_KEY=x",
                "python place_order.py"):
        assert g.match("arbitrage2-opus:0.0", cmd)["allowed"] is False, cmd


# ═════════ 3. "available on request" is unfinished work ═════════════════════
@pytest.mark.parametrize("text", [
    "Ready to continue on request.",
    "The next block is available on request.",
    "Standing by — let me know if you want the next phase.",
    "Готов продолжить по запросу.",
    "Shall I continue?",
    "Awaiting your go-ahead.",
])
def test_parked_awaiting_a_ping_is_unfinished(text):
    c = sig.classify(text)
    assert c["class"] == "unfinished" and c["terminal"] is False, (text, c)


def test_completion_claim_with_open_tasks_is_unfinished():
    c = sig.classify("All tests passed.\n5 tasks (2 done, 3 open)")
    assert c["class"] == "unfinished"


def test_verified_completion_is_terminal():
    c = sig.classify("All tests passed. Suite green. 3 tasks (3 done, 0 open)")
    assert c["class"] == "terminal" and c["terminal"] is True


@pytest.mark.parametrize("text", [
    "This item requires a physical device to verify.",
    "Blocked: third-party outage at the provider.",
    "Owner decision required before proceeding.",
])
def test_real_external_dependency_is_terminal_block(text):
    c = sig.classify(text)
    assert c["class"] == "external_block" and c["terminal"] is True


@pytest.mark.parametrize("text", [
    "You've hit your session limit · resets 11pm",
    "usage limit reached",
    "Please configure usage credits for Fable 5",
])
def test_model_limit_is_a_wait_not_a_failure(text):
    c = sig.classify(text)
    assert c["class"] == "model_limit" and c["terminal"] is False


def test_awaiting_ping_beats_a_completion_claim():
    """The exact stall: an agent says it is done AND offers more on request."""
    c = sig.classify("All checks pass. Next block available on request.")
    assert c["class"] == "unfinished"


# ═════════ 4. the autopilot acts on those classifications ═══════════════════
REG = {"cp-canary:0.0": {"root": "/tmp", "next_step": "continue with the next safe step",
                         "live_actuation": True}}


def _ev(tail, state="idle"):
    return ap.evaluate("cp-canary:0.0", state=state, tail=tail, registry=REG)


def test_autopilot_pokes_a_pane_parked_on_available_on_request():
    """Pre-fix: no task footer + no unfinished marker → skip_no_work, and the session
    sat forever. Now the parked phrasing itself is unfinished work."""
    d = _ev("Work complete for this block.\nNext block available on request.\n")
    assert d["decision"] == "poke", d


def test_autopilot_classifies_terminal_pass_without_poking():
    d = _ev("All tests passed. Suite green.\n4 tasks (4 done, 0 open)\n")
    assert d["decision"] in ("terminal_pass", "end_state_met"), d


def test_autopilot_classifies_a_real_external_block():
    d = _ev("Remaining matrix items require a physical device.\n2 open\n")
    assert d["decision"] == "terminal_external_block", d


def test_autopilot_treats_a_model_limit_as_a_wait():
    d = _ev("You've hit your session limit · resets 11pm\n2 open\n")
    assert d["decision"] == "skip_model_limit" and d["situation"] == "model_limit"


def test_autopilot_still_skips_a_working_pane():
    d = _ev("✻ Wibbling… (12s · ↓ 2k tokens · esc to interrupt)\n2 open\n",
            state="working")
    assert d["decision"] == "skip_progressing"
