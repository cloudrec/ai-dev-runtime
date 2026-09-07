"""Per-AGENT exclusion from native supervision.

The project denylist answers "this project is value-bearing". It cannot answer "leave this
one stale pane alone", because panes and projects do not correspond: on 2026-09-07 a peer
session asked for its agent to stop being continued, and the only project-level answer
would have been to denylist `seo` — which also hosts the MCP connector backend. Silencing
one pane would have cost supervision of a whole project.

`NATIVE_SUPERVISOR_DENY_TARGETS` is that missing tool. It is empty by default and can only
ever REMOVE supervision, which is what makes it safe to ship without a rollout.

The load-bearing case is the LAST one: `NATIVE_SUPERVISOR_TARGETS="*"` must not bypass it.
That is the exact shape of the bug the project denylist already had — a rollout switch
that silently became a denylist bypass — and the comment in `is_supervised` says a test
written to assert the opposite is what found it.
"""
from __future__ import annotations

import importlib

import pytest

from core import native_supervisor as ns


def _reload(monkeypatch, *, deny_targets="", targets="*", db=None):
    """Re-import the module so its env-derived module constants are rebuilt."""
    monkeypatch.setenv("NATIVE_SUPERVISOR_DENY_TARGETS", deny_targets)
    monkeypatch.setenv("NATIVE_SUPERVISOR_TARGETS", targets)
    if db is not None:
        monkeypatch.setenv("CONTROL_PLANE_DB", str(db))
    return importlib.reload(ns)


@pytest.fixture(autouse=True)
def _restore():
    yield
    importlib.reload(ns)          # never leak a patched module into another test


# ── the helper's own semantics ──────────────────────────────────────────────
def test_no_exclusions_configured_denies_nothing(monkeypatch):
    m = _reload(monkeypatch, deny_targets="")
    assert m.DENY_TARGETS == set()
    assert m.target_denied("anything:0.0") is False


def test_an_exact_target_is_denied(monkeypatch):
    m = _reload(monkeypatch, deny_targets="mess-postsignup-cleanup-sonnet-v4:0.0")
    assert m.target_denied("mess-postsignup-cleanup-sonnet-v4:0.0") is True
    assert m.target_denied("mess-postsignup-cleanup-sonnet-v4:0.1") is False


def test_the_session_name_alone_also_denies(monkeypatch):
    """An operator lists the name tmux shows them; that must be what they meant."""
    m = _reload(monkeypatch, deny_targets="mess-postsignup-cleanup-sonnet-v4")
    assert m.target_denied("mess-postsignup-cleanup-sonnet-v4:0.0") is True
    assert m.target_denied("mess-postsignup-cleanup-sonnet-v4:1.2") is True


def test_the_session_match_is_equality_not_prefix(monkeypatch):
    """`mess` must not silently capture every mess-* agent on the host."""
    m = _reload(monkeypatch, deny_targets="mess")
    assert m.target_denied("mess:0.0") is True
    assert m.target_denied("mess-ru-54582145-resumed:0.0") is False
    assert m.target_denied("mess-postsignup-cleanup-sonnet-v4:0.0") is False


def test_an_empty_target_fails_closed(monkeypatch):
    """Fail closed in the direction that matters: an unnamed pane is not supervised."""
    m = _reload(monkeypatch, deny_targets="something:0.0")
    assert m.target_denied("") is True
    assert m.target_denied(None) is True


def test_whitespace_and_blank_entries_are_ignored(monkeypatch):
    m = _reload(monkeypatch, deny_targets="  a:0.0 , ,, b:0.0  ")
    assert m.DENY_TARGETS == {"a:0.0", "b:0.0"}
    assert m.target_denied("a:0.0") and m.target_denied("b:0.0")


# ── the gate ────────────────────────────────────────────────────────────────
def test_an_excluded_target_is_not_supervised(monkeypatch, tmp_path):
    m = _reload(monkeypatch, deny_targets="quiet:0.0", db=tmp_path / "cp.db")
    assert m.is_supervised("quiet:0.0", project="seo") is False
    assert m.is_supervised("noisy:0.0", project="seo") is True


def test_the_wildcard_rollout_does_not_bypass_the_exclusion(monkeypatch, tmp_path):
    """The bug the project denylist already had, asserted for this one before it happens.

    `NATIVE_SUPERVISOR_TARGETS="*"` returns True from the wildcard branch before any
    later filter runs, so an exclusion checked after it would never execute.
    """
    m = _reload(monkeypatch, deny_targets="quiet:0.0", targets="*", db=tmp_path / "cp.db")
    assert "*" in m.allowed_targets(), "the wildcard rollout is not actually active"
    assert m.is_supervised("quiet:0.0", project="seo") is False, (
        "NATIVE_SUPERVISOR_TARGETS='*' bypassed the per-target exclusion — a rollout "
        "switch must never be a denylist bypass")


def test_an_explicit_allowlist_entry_does_not_bypass_it_either(monkeypatch, tmp_path):
    m = _reload(monkeypatch, deny_targets="quiet:0.0", targets="quiet:0.0,other:0.0",
                db=tmp_path / "cp.db")
    assert m.is_supervised("quiet:0.0") is False
    assert m.is_supervised("other:0.0") is True


def test_the_block_reason_names_the_exclusion(monkeypatch):
    """The journal must distinguish this from a value-bearing project or a stranger."""
    m = _reload(monkeypatch, deny_targets="quiet:0.0")
    assert m.send_block_reason("quiet:0.0", "seo") == "target_excluded"
    assert m.send_block_reason("other:0.0", "seo") == "not_registered"
    assert m.send_block_reason("other:0.0", "payment-orchestrator") == "value_bearing_send_blocked"


# ── registry effects ────────────────────────────────────────────────────────
def test_an_excluded_target_is_never_auto_registered(monkeypatch, tmp_path):
    m = _reload(monkeypatch, deny_targets="quiet:0.0", db=tmp_path / "cp.db")
    agents = [{"target": "quiet:0.0", "is_agent": True, "alive": True, "cwd": "/opt/seo"},
              {"target": "loud:0.0", "is_agent": True, "alive": True, "cwd": "/opt/seo"}]
    out = m.auto_register(agents)
    assert [r["target"] for r in out["registered"]] == ["loud:0.0"]
    assert any(s.get("why") == "deny_listed_target" and s["target"] == "quiet:0.0"
               for s in out["skipped"])


def test_excluding_a_target_purges_an_existing_registration(monkeypatch, tmp_path):
    """Adding an exclusion must take effect without anyone remembering to clean up."""
    db = tmp_path / "cp.db"
    m = _reload(monkeypatch, deny_targets="", db=db)
    m.auto_register([{"target": "quiet:0.0", "is_agent": True, "alive": True,
                      "cwd": "/opt/seo"}])
    assert "quiet:0.0" in m.registered_targets()

    m = _reload(monkeypatch, deny_targets="quiet:0.0", db=db)   # operator adds it later
    assert "quiet:0.0" in m.purge_denied()
    assert "quiet:0.0" not in m.registered_targets()
    assert m.is_supervised("quiet:0.0") is False


def test_registered_targets_filters_the_exclusion_on_read(monkeypatch, tmp_path):
    """Belt and braces: even before a purge runs, a read must not report it."""
    db = tmp_path / "cp.db"
    m = _reload(monkeypatch, deny_targets="", db=db)
    m.auto_register([{"target": "quiet:0.0", "is_agent": True, "alive": True,
                      "cwd": "/opt/seo"}])
    m = _reload(monkeypatch, deny_targets="quiet:0.0", db=db)
    assert "quiet:0.0" not in m.registered_targets()            # no purge called here


# ── the project denylist must be untouched by this ──────────────────────────
def test_the_project_denylist_still_works(monkeypatch, tmp_path):
    m = _reload(monkeypatch, deny_targets="quiet:0.0", db=tmp_path / "cp.db")
    assert m.is_supervised("anything:0.0", project="payment-orchestrator") is False
    assert m.is_supervised("anything:0.0", project=m.SELF_PROJECT) is False
    assert m.SELF_PROJECT in m.AUTO_REGISTER_DENY_PROJECTS
