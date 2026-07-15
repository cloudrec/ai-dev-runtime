"""Tests for the read-only GitHub portfolio backlog generator.

These tests exercise the pure helper functions and the report rendering with
synthetic issue data, so they never touch the network or the `gh` CLI.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "reports" / "generate_github_portfolio_backlog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gh_portfolio_backlog", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _issue(**kw):
    base = {
        "number": 1,
        "title": "Sample",
        "state": "open",
        "labels": [],
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-07-14T00:00:00Z",
        "url": "https://example.test/1",
        "body": "",
        "repo": "cloudrec/ai-dev-runtime",
    }
    base.update(kw)
    return base


def test_categorize_books():
    assert mod.categorize(_issue(title="New book translation illustrations")) == "books-translations-publishing"


def test_categorize_promotion():
    assert mod.categorize(_issue(title="TikTok and YouTube promotion")) == "cross-platform-promotion"


def test_categorize_uncategorized():
    assert mod.categorize(_issue(title="totally unrelated widget", repo="cloudrec/misc")) == "uncategorized"


def test_derive_priority_from_label():
    assert mod.derive_priority(_issue(labels=[{"name": "P0"}])) == "P0"
    assert mod.derive_priority(_issue(labels=[{"name": "high"}])) == "P1"
    assert mod.derive_priority(_issue(labels=[])) == "P2"


def test_build_record_shape():
    rec = mod.build_record(_issue(title="SEO OS improvements", labels=[{"name": "P1"}]))
    for key in ("repo", "number", "title", "state", "labels", "created_at",
                "updated_at", "linked", "project_path_domain", "category",
                "priority", "next_action"):
        assert key in rec
    assert rec["priority"] == "P1"


def test_reconcile_detects_duplicates_and_completed():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    records = [
        mod.build_record(_issue(number=1, title="Backup migration")),
        mod.build_record(_issue(number=2, title="Backup migration")),
        mod.build_record(_issue(number=3, title="Old task", state="closed")),
    ]
    rec = mod.reconcile(records, now)
    assert len(rec["duplicate_candidates"]) == 2
    assert len(rec["completed_recently_closed"]) == 1
    assert "jobhunter-ai" in rec["missing_domains_without_issue"]


def test_is_stale():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    fresh = mod.build_record(_issue(updatedAt="2026-07-14T00:00:00Z"))
    old = mod.build_record(_issue(updatedAt="2026-05-01T00:00:00Z"))
    assert not mod.is_stale(fresh, now)
    assert mod.is_stale(old, now)


def test_render_markdown_contains_counts():
    payload = {
        "metadata": {"repo_count": 2, "issue_count": 1, "open_count": 1, "closed_count": 0},
        "issues": [mod.build_record(_issue(title="Email platform inbox"))],
        "reconciliation": mod.reconcile([mod.build_record(_issue())], datetime(2026, 7, 15, tzinfo=timezone.utc)),
    }
    md = mod.render_markdown(payload)
    assert "GitHub Portfolio Backlog" in md
    assert "Repositories scanned: 2" in md
