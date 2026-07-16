"""Tests for the Prospect Audit batch runner.

They build a throwaway source database that mirrors the shape of
``company_websites`` and assert both the produced artifacts and the safety
rules (source stays untouched, nothing is written under /opt/email, no mail
transport is imported).
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import prospect_audit_batch as pab  # noqa: E402


def _make_source_db(path: Path, rows: int = 30) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE company_websites ("
        "id INTEGER PRIMARY KEY, company TEXT, website TEXT, status TEXT, "
        "has_ssl INTEGER, has_contact_form INTEGER, is_mobile_friendly INTEGER, "
        "response_time_ms INTEGER)"
    )
    for i in range(1, rows + 1):
        conn.execute(
            "INSERT INTO company_websites VALUES (?,?,?,?,?,?,?,?)",
            (
                i,
                f"Acme {i} GmbH",
                f"https://acme{i}.example",
                "active" if i % 5 else "paused",
                i % 2,
                (i + 1) % 2,
                1,
                800 + (i * 100),
            ),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def source_db(tmp_path: Path) -> Path:
    return _make_source_db(tmp_path / "prospects.db")


@pytest.fixture()
def cfg(tmp_path: Path, source_db: Path) -> pab.BatchConfig:
    return pab.BatchConfig(
        source_db=str(source_db),
        batch_size=25,
        output_root=str(tmp_path / "out"),
        handoff_dir=str(tmp_path / "handoff"),
        report_dir=str(tmp_path / "reports"),
        batch_id="batch-test-001",
    )


def test_selects_only_active_rows_up_to_batch_size(cfg):
    conn = pab.open_readonly(cfg.source_db)
    try:
        prospects = pab.select_active_prospects(conn, cfg.source_table, cfg.batch_size)
    finally:
        conn.close()
    assert len(prospects) == 24  # 30 rows, every 5th is paused
    assert all(p.raw["status"] == "active" for p in prospects)


def test_run_batch_produces_full_artifact_set(cfg):
    result = pab.run_batch(cfg)

    assert result.selected == result.succeeded
    assert result.failed == 0
    assert result.failures == []
    assert result.emails_sent is False

    for audit in result.audits:
        short = Path(audit.short_report_path).read_text(encoding="utf-8")
        full = Path(audit.full_report_path).read_text(encoding="utf-8")
        handoff = json.loads(Path(audit.handoff_path).read_text(encoding="utf-8"))

        assert audit.public_url in full
        assert cfg.contact_form_url in short and cfg.contact_form_url in full
        assert pab.DEFAULT_WIDGET_SCRIPT_URL in full
        assert f'data-audit-id="{audit.token}"' in full
        assert audit.public_url in audit.outreach_text
        assert handoff["delivery"]["send"] is False
        assert handoff["audit"]["public_url"] == audit.public_url
        assert handoff["outreach"]["text"] == audit.outreach_text


def test_public_urls_are_unique_and_deterministic(cfg):
    first = pab.run_batch(cfg)
    urls = [a.public_url for a in first.audits]
    assert len(set(urls)) == len(urls)

    second = pab.run_batch(cfg)
    assert [a.public_url for a in second.audits] == urls

    cfg.url_salt = "different-salt"
    third = pab.run_batch(cfg)
    assert [a.public_url for a in third.audits] != urls


def test_source_database_is_never_modified(cfg):
    before = hashlib.sha256(Path(cfg.source_db).read_bytes()).hexdigest()
    pab.run_batch(cfg)
    after = hashlib.sha256(Path(cfg.source_db).read_bytes()).hexdigest()
    assert before == after


def test_readonly_connection_rejects_writes(cfg):
    conn = pab.open_readonly(cfg.source_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE company_websites SET status = 'x'")
    finally:
        conn.close()


def test_writing_under_email_prefix_is_refused(cfg):
    cfg.handoff_dir = "/opt/email/spool"
    with pytest.raises(pab.BatchSafetyError):
        pab.run_batch(cfg)


def test_send_emails_flag_aborts_the_batch(cfg):
    cfg.send_emails = True
    with pytest.raises(pab.BatchSafetyError):
        pab.run_batch(cfg)


def test_module_imports_no_mail_transport():
    source = Path(pab.__file__).read_text(encoding="utf-8")
    for banned in ("import smtplib", "import requests", "import urllib.request"):
        assert banned not in source


def test_bad_row_is_reported_but_does_not_sink_the_batch(tmp_path, cfg):
    conn = sqlite3.connect(cfg.source_db)
    conn.execute(
        "INSERT INTO company_websites VALUES (?,?,?,?,?,?,?,?)",
        (999, "No Site Ltd", "", "active", 1, 1, 1, 500),
    )
    conn.commit()
    conn.close()

    conn = pab.open_readonly(cfg.source_db)
    try:
        with pytest.raises(pab.BatchSourceError):
            pab.row_to_prospect({"id": 999, "company": "No Site Ltd", "website": ""})
    finally:
        conn.close()

    result = pab.run_batch(cfg)
    assert result.succeeded >= 24


def test_operational_report_records_counts_and_paths(cfg):
    result = pab.run_batch(cfg)
    paths = pab.write_operational_report(result, cfg.report_dir)

    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))

    assert "Emails sent: no" in markdown
    assert result.batch_id in markdown
    assert data["counts"]["succeeded"] == result.succeeded
    assert data["counts"]["failed"] == result.failed
    assert data["emails_sent"] is False
    assert data["email_db_writes"] == 0
    assert set(data["output_paths"]) == {
        "batch_dir",
        "short_reports",
        "full_reports",
        "outreach",
        "handoff",
    }


def test_scoring_and_findings(cfg):
    prospect = pab.Prospect(
        row_id="1",
        company="Acme",
        domain="acme.example",
        website="https://acme.example",
        raw={"has_ssl": 1, "has_contact_form": 0, "response_time_ms": 4000},
    )
    findings = pab.build_findings(prospect)
    by_key = {f.key: f for f in findings}
    assert by_key["https"].status == "ok"
    assert by_key["contact_form"].status == "issue"
    assert by_key["response"].status == "issue"
    assert by_key["privacy_policy"].status == "not_assessed"
    assert pab.score_findings(findings) == 33
