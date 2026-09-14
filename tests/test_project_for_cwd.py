"""A project is not the directory an agent happens to be standing in.

Every project-mapping site took the LAST path segment of the cwd, which is correct only
while an agent sits at its project root. The moment it works in a subdirectory it acquires
a phantom project named after that directory — and a phantom project is on no denylist.

Live on 2026-09-14: `payorch-ha-fresh`, session `6db4919a-460a-4b62-9e0d-c9fff356a7be`,
working in `/opt/payment-orchestrator/deploy/infra/dns`. Every hook event it produced was
filed under project `dns`, including event 51041 (`agent_process_failed`, critical).
`payment-orchestrator` and `payorch` are both deny-listed. `dns` is not. So the denylist
protecting payment lapsed exactly when the agent changed directory — fail-open, in the one
place that has to fail closed.
"""
from __future__ import annotations

import pytest

from core import wake_routes as wr

CFG = {"sessions": {
    "seo-audit": {"root": "/opt/seo", "project": "seo"},
    "nested": {"root": "/opt/seo/sub", "project": "seo-sub"},
    "named-by-session": {"root": "/opt/thing"},
}}


# ── the live case ───────────────────────────────────────────────────────────
def test_a_subdirectory_belongs_to_its_project():
    assert wr.project_for_cwd("/opt/payment-orchestrator/deploy/infra/dns", CFG) \
        == "payment-orchestrator"


def test_the_project_root_itself_is_unchanged():
    assert wr.project_for_cwd("/opt/payment-orchestrator", CFG) == "payment-orchestrator"


def test_the_answer_does_not_move_when_the_agent_does():
    """The property that was broken: cd must not change which project you are in."""
    base = "/opt/payment-orchestrator"
    answers = {wr.project_for_cwd(p, CFG) for p in (
        base, base + "/deploy", base + "/deploy/infra", base + "/deploy/infra/dns")}
    assert answers == {"payment-orchestrator"}, answers


# ── the consequence that makes it matter ────────────────────────────────────
def test_an_agent_in_a_payment_subdirectory_is_deny_listed():
    from core import native_supervisor as ns
    proj = ns._project_of({"claude_cwd": "/opt/payment-orchestrator/deploy/infra/dns"})
    assert proj == "payment-orchestrator"
    assert proj in ns.AUTO_REGISTER_DENY_PROJECTS, \
        "changing directory must not walk an agent out of the denylist"


def test_the_supervisor_and_the_resolver_agree(monkeypatch):
    from core import native_supervisor as ns
    for cwd in ("/opt/diamond/x", "/opt/payment-orchestrator/y", "/root/ai-dev-runtime/core"):
        assert ns._project_of({"cwd": cwd}) == wr.project_for_cwd(cwd)


# ── configured roots win, and the longest one wins ──────────────────────────
def test_a_configured_root_beats_the_directory_convention():
    assert wr.project_for_cwd("/opt/seo/reports/2026", CFG) == "seo"


def test_the_longest_matching_root_wins():
    """Otherwise a nested project is swallowed by its parent."""
    assert wr.project_for_cwd("/opt/seo/sub/deeper", CFG) == "seo-sub"


def test_a_root_without_an_explicit_project_uses_its_session_name():
    assert wr.project_for_cwd("/opt/thing/a/b", CFG) == "named-by-session"


def test_a_root_is_matched_on_a_path_boundary_not_a_prefix():
    """/opt/seonext must not be read as a subdirectory of /opt/seo."""
    assert wr.project_for_cwd("/opt/seonext/x", CFG) == "seonext"


# ── the workspace convention, and the fallback ──────────────────────────────
@pytest.mark.parametrize("cwd,expected", [
    ("/opt/diamond", "diamond"),
    ("/opt/hostsecure/src/app", "hostsecure"),
    ("/root/ai-dev-runtime/core/control_plane", "ai-dev-runtime"),
    ("/srv/thing/deep/deeper", "thing"),
])
def test_the_first_segment_under_a_workspace_root_is_the_project(cwd, expected):
    assert wr.project_for_cwd(cwd, CFG) == expected


def test_a_path_under_nothing_known_keeps_the_old_answer():
    """Previous behaviour is the fallback, not a failure."""
    assert wr.project_for_cwd("/tmp/scratch/thing", CFG) == "thing"


def test_empty_and_degenerate_inputs():
    assert wr.project_for_cwd("", CFG) == ""
    assert wr.project_for_cwd("/", CFG) == ""
    assert wr.project_for_cwd("/opt", CFG) == "opt"       # a workspace root is not a project


def test_a_trailing_slash_changes_nothing():
    assert wr.project_for_cwd("/opt/payment-orchestrator/deploy/", CFG) \
        == wr.project_for_cwd("/opt/payment-orchestrator/deploy", CFG)


def test_an_unreadable_config_never_breaks_routing(monkeypatch):
    """Classification must degrade to the convention, not raise into a hook."""
    import core.control_plane.discovery as d
    monkeypatch.setattr(d, "_load_config",
                        lambda: (_ for _ in ()).throw(OSError("gone")))
    assert wr.project_for_cwd("/opt/payment-orchestrator/deploy") == "payment-orchestrator"


def test_the_result_is_a_usable_route_key():
    """Whatever comes back has to survive normalize_key, or routing silently falls back."""
    for cwd in ("/opt/payment-orchestrator/deploy/infra/dns", "/opt/seo/x", "/tmp/a/b"):
        got = wr.project_for_cwd(cwd, CFG)
        assert got and wr.normalize_key(got) == got


# ── the hook files the corrected project ────────────────────────────────────
def test_the_hook_identity_uses_the_resolver():
    import sys
    sys.path.insert(0, "/root/ai-dev-runtime/hooks")
    from owneros_hook import _identity
    ident = _identity({"cwd": "/opt/payment-orchestrator/deploy/infra/dns",
                       "session_id": "6db4919a-460a-4b62-9e0d-c9fff356a7be"})
    assert ident["project"] == "payment-orchestrator", \
        f"event 51041 filed this as {ident['project']!r}"
    assert ident["cwd"] == "/opt/payment-orchestrator/deploy/infra/dns", \
        "the cwd itself is evidence and must survive unchanged"


# ── a more accurate name must never open a gate ─────────────────────────────
# Correcting the mapping had a consequence pointing the wrong way. An agent in
# /opt/diamond/auction used to resolve to `auction`, which IS deny-listed; correctly
# resolved it is `diamond`, which the running environment's list does not carry. The
# better answer would have REMOVED a protection. So denial is tested against the
# resolved project AND every path segment: accuracy improves routing, and is not allowed
# to widen what automation may touch.

def test_a_corrected_project_name_cannot_undeny_an_agent(monkeypatch):
    from core import native_supervisor as ns
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"auction"})
    cwd = "/opt/diamond/auction"
    assert ns._project_of({"cwd": cwd}) == "diamond", "the accurate answer"
    assert ns._denied_for("diamond", cwd) is True, \
        "the directory is still called auction and auction is denied"


def test_denial_widens_and_never_narrows(monkeypatch):
    from core import native_supervisor as ns
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"payorch"})
    # denied by a mid-path segment nobody would have picked as the project
    assert ns._denied_for("x", "/opt/payorch/sub/deep") is True
    # and an unrelated tree stays allowed
    assert ns._denied_for("hostsecure", "/opt/hostsecure/src") is False


def test_the_block_reason_still_names_a_deliberate_exclusion(monkeypatch):
    """Otherwise the journal downgrades an owner-set exclusion to 'nobody registered it'."""
    from core import native_supervisor as ns
    monkeypatch.setattr(ns, "AUTO_REGISTER_DENY_PROJECTS", {"auction"})
    assert ns.send_block_reason("diamond-auction:0.0", "diamond",
                                cwd="/opt/diamond/auction") == "value_bearing_send_blocked"


def test_the_self_reference_reason_survives_the_candidate_rule(monkeypatch):
    from core import native_supervisor as ns
    assert ns.send_block_reason("x:0.0", ns.SELF_PROJECT,
                                cwd=f"/root/{ns.SELF_PROJECT}/core") == \
        "supervisor_self_reference"


def test_candidates_never_include_empty_or_unusable_names():
    from core import native_supervisor as ns
    assert "" not in ns.project_candidates("", "/opt//weird///path/")
    assert all(c == wr.normalize_key(c) or c == "diamond"
               for c in ns.project_candidates("diamond", "/opt/diamond/auction"))
