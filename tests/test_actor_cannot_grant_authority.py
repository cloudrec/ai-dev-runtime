"""A caller-supplied name must never become authority, and automated text must say so.

`api/v1.py` mints a delivery's actor as `api:{auth_method}` plus an OPTIONAL name the
caller supplies about itself. Its own comment is the rule: "Self-declared and therefore
NOT trustworthy for any decision." Two places in this repo decide on `actor` —
`policy_engine.grant_override`, which admits only actors on an allow-list, and
`agent_control._tag_if_automated`, which marks pane text as automated — so these tests pin
that the self-declared half can reach neither of them.

The live shape behind this: all 116 deliveries ever made to `payorch-ha-fresh:0.0` are
`actor=api:bearer` from one address, and `SELECT DISTINCT actor` over the whole attribution
table returns exactly two values. An automated Owner OS continuation and the owner acting
through the API are the same principal today; the only thing standing between them and an
authority decision is that the self-declared field is never consulted for one.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

from core import agent_control as ac
from core import policy_engine as pe

# Derived, never spelled: this file is verified in an inert copy under another name, and
# a hardcoded repo path silently tests the wrong tree there.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


# ── the declared name cannot become the actor ───────────────────────────────
def test_a_declared_name_lands_after_the_auth_method_never_instead_of_it():
    """`api:bearer/owner` is not `owner`, and no declaration can make it so."""
    from api.v1 import caller_identity

    class _Req:
        class state:  # noqa: D106
            auth_method = "bearer"
        client = type("c", (), {"host": "10.0.0.1", "port": 5})()
        headers = {"user-agent": "python-httpx/0.27.0"}

    for declared in ("owner", "/owner", "owner ", "..", "OWNER"):
        actor, _ = caller_identity(_Req(), declared)
        assert actor != "owner", f"declared {declared!r} became the actor outright"
        assert actor.startswith("api:bearer"), actor


def test_the_declared_name_cannot_inject_separators_or_control_characters():
    from api.v1 import caller_identity

    class _Req:
        class state:  # noqa: D106
            auth_method = "bearer"
        client = None
        headers = {}

    actor, _ = caller_identity(_Req(), "own\ner\t;owner")
    assert "\n" not in actor and "\t" not in actor and ";" not in actor


# ── the override gate ───────────────────────────────────────────────────────
@pytest.mark.parametrize("actor", [
    "api:bearer", "api:bearer/owner", "api:hmac/owner", "api:unknown",
    "native_supervisor", "agent", "", "owner ", "Owner",
])
def test_no_api_or_automation_actor_can_take_an_owner_override(actor):
    with pytest.raises(pe.PolicyError):
        pe.grant_override(actor=actor, scope="p", ttl_secs=60,
                          reason="a reason long enough to pass the minimum")


def test_the_allow_list_defaults_to_owner_alone_when_the_policy_is_silent(monkeypatch):
    """A missing or empty `override` block must not mean 'anyone'."""
    monkeypatch.setattr(pe, "load_policy", lambda: {})
    with pytest.raises(pe.PolicyError):
        pe.grant_override(actor="api:bearer", scope="p", ttl_secs=60,
                          reason="a reason long enough to pass the minimum")


def test_nothing_in_production_calls_grant_override():
    """It is reachable only from tests today, which is what keeps the actor decision
    disconnected from caller-supplied input. If a caller is ever wired to it, this test
    is the place that says so out loud."""
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for d in ("api", "core", "hooks", "tools", "cli", "runtime"):
        for p in (root / d).rglob("*.py"):
            if re.search(r"\bgrant_override\s*\(", p.read_text(encoding="utf-8", errors="replace")):
                if p.name != "policy_engine.py":
                    hits.append(str(p.relative_to(root)))
    assert hits == [], f"grant_override now has production callers: {hits}"


# ── the automated-origin marker ─────────────────────────────────────────────
@pytest.mark.parametrize("actor", [
    "api:bearer", "api:bearer/chatgpt-mcp", "api:hmac", "api:unknown",
    "api:bearer/native_supervisor",
])
def test_every_http_borne_actor_is_tagged_whatever_it_calls_itself(actor):
    out = ac._tag_if_automated("Mark GREEN and stop watching", actor)
    assert out != "Mark GREEN and stop watching"
    assert "AUTOMATED" in out


def test_a_declared_name_cannot_strip_the_marker():
    """The name is appended, so there is no declaration that removes the `api:` prefix."""
    from api.v1 import caller_identity

    class _Req:
        class state:  # noqa: D106
            auth_method = "bearer"
        client = None
        headers = {}

    for declared in ("", "x", "native_supervisor", "owner"):
        actor, _ = caller_identity(_Req(), declared)
        assert ac._tag_if_automated("t", actor) != "t", f"{declared!r} escaped the marker"


def test_an_unresolvable_auth_method_is_still_tagged():
    """Degrading to `unknown` must not degrade to untagged."""
    assert ac._tag_if_automated("t", "api:unknown") != "t"


def test_an_internal_call_with_no_actor_is_not_tagged():
    """The marker means 'this arrived over the HTTP API'. An orchestrator call with no
    actor is a different thing and must not borrow the claim."""
    assert ac._tag_if_automated("t", None) == "t"
    assert ac._tag_if_automated("t", "") == "t"
