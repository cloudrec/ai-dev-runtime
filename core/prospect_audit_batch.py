"""Prospect Audit batch runner.

Generates a production batch of prospect audits from a *read-only* source
database (``company_websites WHERE status='active'``) and emits, per prospect:

* a short report (HTML)
* a full report (HTML) with the Clients.Help widget snippet and contact-form link
* a unique, unguessable public URL
* manual outreach text
* a JSON handoff record intended for the /opt/email pipeline

Hard safety rules enforced here (not merely documented):

* the source database is opened with ``mode=ro`` and is never written to;
* no email is sent and no mail transport module is imported;
* outputs may not be written under any forbidden prefix (e.g. /opt/email), so
  the email database is never touched. Handoff records are dropped in a local
  spool directory for the email project to pick up separately.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_SOURCE_DB = "/opt/prospect-audit/data/prospects.db"
DEFAULT_SOURCE_TABLE = "company_websites"
DEFAULT_BATCH_SIZE = 25
DEFAULT_PUBLIC_BASE_URL = "https://audit.clients.help/r"
DEFAULT_CONTACT_FORM_URL = "https://clients.help/contact"
DEFAULT_WIDGET_SCRIPT_URL = "https://widget.clients.help/v1/widget.js"

# Writing anywhere under these prefixes is refused: the email project owns them.
FORBIDDEN_OUTPUT_PREFIXES: Tuple[str, ...] = ("/opt/email",)


class BatchSafetyError(RuntimeError):
    """Raised when a requested operation would break a batch safety rule."""


class BatchSourceError(RuntimeError):
    """Raised when the read-only source cannot be used as expected."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(value: Optional[str], maxlen: int = 48) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    cleaned = cleaned[:maxlen].strip("-")
    return cleaned or "prospect"


def public_token(domain: str, salt: str) -> str:
    """Deterministic, unguessable-per-salt token used in the public URL."""
    digest = hashlib.sha256(f"{domain}|{salt}".encode("utf-8")).hexdigest()
    return digest[:16]


def assert_safe_output(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    text = str(resolved)
    for prefix in FORBIDDEN_OUTPUT_PREFIXES:
        if text == prefix or text.startswith(prefix.rstrip("/") + "/"):
            raise BatchSafetyError(
                f"refusing to write under protected prefix {prefix!r}: {text}"
            )
    return resolved


@dataclass
class BatchConfig:
    source_db: str = DEFAULT_SOURCE_DB
    source_table: str = DEFAULT_SOURCE_TABLE
    batch_size: int = DEFAULT_BATCH_SIZE
    output_root: str = "var/prospect_audit/batches"
    handoff_dir: str = "var/prospect_audit/handoff"
    report_dir: str = "reports/runtime"
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    contact_form_url: str = DEFAULT_CONTACT_FORM_URL
    widget_script_url: str = DEFAULT_WIDGET_SCRIPT_URL
    url_salt: str = "prospect-audit-batch-001"
    batch_id: str = ""
    send_emails: bool = False

    def resolved_batch_id(self) -> str:
        return self.batch_id or f"batch-{_today()}-001"


@dataclass
class Prospect:
    row_id: str
    company: str
    domain: str
    website: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    key: str
    title: str
    status: str  # ok | issue | not_assessed
    detail: str


@dataclass
class AuditArtifacts:
    prospect: Prospect
    token: str
    slug: str
    public_url: str
    score: Optional[int]
    findings: List[Finding]
    short_report_path: str
    full_report_path: str
    handoff_path: str
    outreach_text: str


@dataclass
class BatchResult:
    batch_id: str
    generated_at: str
    source_db: str
    selected: int
    succeeded: int
    failed: int
    audits: List[AuditArtifacts] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)
    output_paths: Dict[str, str] = field(default_factory=dict)
    emails_sent: bool = False


# --------------------------------------------------------------------------
# Read-only source access
# --------------------------------------------------------------------------

def open_readonly(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise BatchSourceError(f"source database not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pick(row: Dict[str, Any], names: Sequence[str]) -> Optional[Any]:
    for name in names:
        for key, value in row.items():
            if key.lower() == name and value not in (None, ""):
                return value
    return None


def _domain_of(website: str) -> str:
    text = (website or "").strip()
    text = re.sub(r"^[a-z]+://", "", text, flags=re.IGNORECASE)
    text = text.split("/", 1)[0].split("?", 1)[0]
    return text.lower().lstrip("www.") or "unknown.invalid"


def row_to_prospect(row: Dict[str, Any]) -> Prospect:
    website = str(
        _pick(row, ("website", "url", "site", "domain", "homepage")) or ""
    ).strip()
    if not website:
        raise BatchSourceError("row has no website/domain column value")
    domain = _domain_of(website)
    company = str(
        _pick(row, ("company", "company_name", "name", "title")) or domain
    ).strip()
    row_id = str(_pick(row, ("id", "rowid", "pk")) or domain)
    if not website.lower().startswith(("http://", "https://")):
        website = f"https://{domain}"
    return Prospect(row_id=row_id, company=company, domain=domain, website=website, raw=dict(row))


def select_active_prospects(conn: sqlite3.Connection, table: str, limit: int,
                            rejects: Optional[List[Dict[str, str]]] = None) -> List[Prospect]:
    """Select active prospects, skipping rows that cannot be parsed.

    A row missing a website/domain is a data defect, not a batch failure: it is
    appended to `rejects` (when provided) and skipped so the remaining rows are
    still audited.
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table or ""):
        raise BatchSourceError(f"unsafe table name: {table!r}")
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE status = 'active' ORDER BY 1 LIMIT ?",  # noqa: S608
        (int(limit),),
    )
    prospects: List[Prospect] = []
    for raw in cur.fetchall():
        row = dict(raw)
        try:
            prospects.append(row_to_prospect(row))
        except BatchSourceError as exc:
            if rejects is not None:
                rejects.append({
                    "source_row_id": str(row.get("id") or row.get("rowid") or "unknown"),
                    "domain": "(unparseable)",
                    "error": f"{type(exc).__name__}: {exc}",
                })
    return prospects


# --------------------------------------------------------------------------
# Audit content
# --------------------------------------------------------------------------

CHECKS: Tuple[Tuple[str, str, Tuple[str, ...], str, str], ...] = (
    (
        "https",
        "HTTPS / valid certificate",
        ("has_ssl", "ssl", "https", "is_https"),
        "Traffic is encrypted end to end.",
        "Visitors may see a browser security warning before they ever read your offer.",
    ),
    (
        "contact_form",
        "Working contact form",
        ("has_contact_form", "contact_form", "form"),
        "A reachable contact form is present.",
        "There is no reliable way for a ready-to-buy visitor to reach you.",
    ),
    (
        "privacy_policy",
        "Privacy policy / legal pages",
        ("has_privacy_policy", "privacy_policy", "privacy"),
        "Legal pages are published.",
        "Missing legal pages hurt trust and can block ad platform approval.",
    ),
    (
        "mobile",
        "Mobile-friendly layout",
        ("is_mobile_friendly", "mobile_friendly", "mobile", "responsive"),
        "The layout adapts to small screens.",
        "Most traffic is mobile; a broken layout costs the majority of your leads.",
    ),
    (
        "response",
        "Fast first response",
        ("response_time_ms", "load_time_ms", "speed_ms"),
        "The site responds quickly.",
        "Slow pages lose visitors before the content renders.",
    ),
)


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "ok"):
            return True
        if text in ("0", "false", "no", "n"):
            return False
    return None


def build_findings(prospect: Prospect) -> List[Finding]:
    findings: List[Finding] = []
    for key, title, columns, ok_text, issue_text in CHECKS:
        value = _pick(prospect.raw, columns)
        if key == "response" and value is not None:
            try:
                millis = float(value)
            except (TypeError, ValueError):
                findings.append(Finding(key, title, "not_assessed", "No measurement available."))
                continue
            good = millis <= 1500
            findings.append(
                Finding(
                    key,
                    title,
                    "ok" if good else "issue",
                    f"First response {int(millis)} ms. " + (ok_text if good else issue_text),
                )
            )
            continue
        flag = _as_bool(value)
        if flag is None:
            findings.append(
                Finding(key, title, "not_assessed", "Not covered by the source record.")
            )
        else:
            findings.append(
                Finding(key, title, "ok" if flag else "issue", ok_text if flag else issue_text)
            )
    return findings


def score_findings(findings: Sequence[Finding]) -> Optional[int]:
    assessed = [f for f in findings if f.status in ("ok", "issue")]
    if not assessed:
        return None
    ok = sum(1 for f in assessed if f.status == "ok")
    return round(100 * ok / len(assessed))


def widget_snippet(widget_script_url: str, token: str, company: str) -> str:
    return (
        f'<script src="{html.escape(widget_script_url)}" '
        f'data-clients-help-widget="1" data-audit-id="{html.escape(token)}" '
        f'data-company="{html.escape(company)}" async></script>'
    )


def render_short_report(audit_ctx: Dict[str, Any]) -> str:
    company = html.escape(audit_ctx["company"])
    issues = audit_ctx["issues"]
    score = audit_ctx["score"]
    score_text = "n/a" if score is None else f"{score}/100"
    items = "\n".join(
        f"    <li><strong>{html.escape(f.title)}</strong> &mdash; {html.escape(f.detail)}</li>"
        for f in issues[:3]
    ) or "    <li>No blocking issues detected in the checks we could run.</li>"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"robots\" content=\"noindex,nofollow\">\n"
        f"  <title>Website audit summary &mdash; {company}</title>\n"
        "</head>\n<body>\n"
        f"  <h1>Website audit summary: {company}</h1>\n"
        f"  <p>Site: <a href=\"{html.escape(audit_ctx['website'])}\">{html.escape(audit_ctx['website'])}</a></p>\n"
        f"  <p>Health score: <strong>{score_text}</strong></p>\n"
        "  <h2>Top findings</h2>\n  <ul>\n"
        f"{items}\n"
        "  </ul>\n"
        f"  <p><a href=\"{html.escape(audit_ctx['public_url'])}\">Read the full audit</a></p>\n"
        f"  <p><a href=\"{html.escape(audit_ctx['contact_form_url'])}\">Ask us a question</a></p>\n"
        "</body>\n</html>\n"
    )


def render_full_report(audit_ctx: Dict[str, Any]) -> str:
    company = html.escape(audit_ctx["company"])
    score = audit_ctx["score"]
    score_text = "n/a" if score is None else f"{score}/100"
    rows = "\n".join(
        "      <tr>"
        f"<td>{html.escape(f.title)}</td>"
        f"<td>{html.escape(f.status)}</td>"
        f"<td>{html.escape(f.detail)}</td>"
        "</tr>"
        for f in audit_ctx["findings"]
    )
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <meta name=\"robots\" content=\"noindex,nofollow\">\n"
        f"  <title>Full website audit &mdash; {company}</title>\n"
        "</head>\n<body>\n"
        f"  <h1>Full website audit: {company}</h1>\n"
        f"  <p>Site: <a href=\"{html.escape(audit_ctx['website'])}\">{html.escape(audit_ctx['website'])}</a></p>\n"
        f"  <p>Generated: {html.escape(audit_ctx['generated_at'])} &middot; Report ID: {html.escape(audit_ctx['token'])}</p>\n"
        f"  <p>Health score: <strong>{score_text}</strong></p>\n"
        "  <h2>Checks</h2>\n"
        "  <table>\n    <thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>\n    <tbody>\n"
        f"{rows}\n"
        "    </tbody>\n  </table>\n"
        "  <h2>Next step</h2>\n"
        f"  <p><a href=\"{html.escape(audit_ctx['contact_form_url'])}\">Send us a message via the contact form</a> "
        "and we will walk through the findings with you.</p>\n"
        "  <div id=\"clients-help-widget\"></div>\n"
        f"  {audit_ctx['widget_snippet']}\n"
        f"  <p><small>Permanent link to this report: {html.escape(audit_ctx['public_url'])}</small></p>\n"
        "</body>\n</html>\n"
    )


def render_outreach_text(audit_ctx: Dict[str, Any]) -> str:
    issues = audit_ctx["issues"]
    if issues:
        headline = issues[0].title.lower()
        bullets = "\n".join(f"- {f.title}: {f.detail}" for f in issues[:3])
    else:
        headline = "a few smaller improvements"
        bullets = "- No blocking issues found; the report lists the smaller wins."
    score = audit_ctx["score"]
    score_line = "" if score is None else f"Overall health score: {score}/100.\n"
    return (
        f"Hi {audit_ctx['company']} team,\n\n"
        f"We ran a free technical audit of {audit_ctx['domain']} and found an issue with "
        f"{headline}.\n\n"
        f"{bullets}\n\n"
        f"{score_line}"
        f"Full report (no signup): {audit_ctx['public_url']}\n"
        f"Questions? {audit_ctx['contact_form_url']}\n\n"
        "If this is not useful, just ignore this message and we will not follow up.\n"
    )


# --------------------------------------------------------------------------
# Batch execution
# --------------------------------------------------------------------------

def _write(path: Path, text: str) -> str:
    safe = assert_safe_output(path)
    safe.parent.mkdir(parents=True, exist_ok=True)
    safe.write_text(text, encoding="utf-8")
    return str(safe)


def build_audit(prospect: Prospect, cfg: BatchConfig, batch_dir: Path, handoff_dir: Path) -> AuditArtifacts:
    token = public_token(prospect.domain, cfg.url_salt)
    slug = slugify(prospect.company)
    public_url = f"{cfg.public_base_url.rstrip('/')}/{slug}-{token}"
    findings = build_findings(prospect)
    issues = [f for f in findings if f.status == "issue"]
    score = score_findings(findings)
    ctx: Dict[str, Any] = {
        "company": prospect.company,
        "domain": prospect.domain,
        "website": prospect.website,
        "token": token,
        "public_url": public_url,
        "contact_form_url": cfg.contact_form_url,
        "findings": findings,
        "issues": issues,
        "score": score,
        "generated_at": _utcnow_iso(),
        "widget_snippet": widget_snippet(cfg.widget_script_url, token, prospect.company),
    }
    base = f"{slug}-{token}"
    short_path = _write(batch_dir / "short" / f"{base}.html", render_short_report(ctx))
    full_path = _write(batch_dir / "full" / f"{base}.html", render_full_report(ctx))
    outreach = render_outreach_text(ctx)
    _write(batch_dir / "outreach" / f"{base}.txt", outreach)

    handoff = {
        "schema_version": 1,
        "event": "prospect_audit.ready",
        "batch_id": cfg.resolved_batch_id(),
        "generated_at": ctx["generated_at"],
        "prospect": {
            "source_row_id": prospect.row_id,
            "company": prospect.company,
            "domain": prospect.domain,
            "website": prospect.website,
        },
        "audit": {
            "token": token,
            "public_url": public_url,
            "score": score,
            "issue_count": len(issues),
            "short_report_path": short_path,
            "full_report_path": full_path,
        },
        "outreach": {
            "mode": "manual",
            "text": outreach,
            "contact_form_url": cfg.contact_form_url,
        },
        "delivery": {
            "send": False,
            "emails_sent": False,
            "note": "Generated read-only. The email project owns all sending decisions.",
        },
    }
    handoff_path = _write(
        handoff_dir / f"{base}.json", json.dumps(handoff, indent=2, ensure_ascii=False) + "\n"
    )
    return AuditArtifacts(
        prospect=prospect,
        token=token,
        slug=slug,
        public_url=public_url,
        score=score,
        findings=findings,
        short_report_path=short_path,
        full_report_path=full_path,
        handoff_path=handoff_path,
        outreach_text=outreach,
    )


def run_batch(cfg: BatchConfig) -> BatchResult:
    if cfg.send_emails:
        raise BatchSafetyError("send_emails must stay False: this runner never sends mail")
    batch_id = cfg.resolved_batch_id()
    batch_dir = assert_safe_output(Path(cfg.output_root) / batch_id)
    handoff_dir = assert_safe_output(Path(cfg.handoff_dir) / batch_id)

    rejects: List[Dict[str, str]] = []
    conn = open_readonly(cfg.source_db)
    try:
        prospects = select_active_prospects(conn, cfg.source_table, cfg.batch_size, rejects)
    finally:
        conn.close()

    result = BatchResult(
        batch_id=batch_id,
        generated_at=_utcnow_iso(),
        source_db=str(Path(cfg.source_db).expanduser()),
        selected=len(prospects) + len(rejects),
        succeeded=0,
        failed=len(rejects),
    )
    result.failures.extend(rejects)
    seen_tokens: Dict[str, str] = {}
    for prospect in prospects:
        try:
            audit = build_audit(prospect, cfg, batch_dir, handoff_dir)
            if audit.token in seen_tokens:
                raise BatchSafetyError(
                    f"duplicate public token for {prospect.domain} and {seen_tokens[audit.token]}"
                )
            seen_tokens[audit.token] = prospect.domain
            result.audits.append(audit)
            result.succeeded += 1
        except Exception as exc:  # one bad row must not sink the batch
            result.failed += 1
            result.failures.append(
                {
                    "source_row_id": prospect.row_id,
                    "domain": prospect.domain,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    result.output_paths = {
        "batch_dir": str(batch_dir),
        "short_reports": str(batch_dir / "short"),
        "full_reports": str(batch_dir / "full"),
        "outreach": str(batch_dir / "outreach"),
        "handoff": str(handoff_dir),
    }
    return result


# --------------------------------------------------------------------------
# Operational report
# --------------------------------------------------------------------------

def result_to_dict(result: BatchResult) -> Dict[str, Any]:
    return {
        "batch_id": result.batch_id,
        "generated_at": result.generated_at,
        "source_db": result.source_db,
        "source_query": "SELECT * FROM company_websites WHERE status = 'active'",
        "source_access": "read-only (sqlite mode=ro)",
        "counts": {
            "selected": result.selected,
            "succeeded": result.succeeded,
            "failed": result.failed,
        },
        "emails_sent": result.emails_sent,
        "forms_submitted": False,
        "email_db_writes": 0,
        "output_paths": result.output_paths,
        "audits": [
            {
                "company": a.prospect.company,
                "domain": a.prospect.domain,
                "public_url": a.public_url,
                "score": a.score,
                "short_report_path": a.short_report_path,
                "full_report_path": a.full_report_path,
                "handoff_path": a.handoff_path,
            }
            for a in result.audits
        ],
        "failures": result.failures,
    }


def render_operational_report(result: BatchResult) -> str:
    data = result_to_dict(result)
    lines = [
        f"# Prospect Audit &mdash; batch {result.batch_id}",
        "",
        f"- Generated: {result.generated_at}",
        f"- Source: `{result.source_db}` (read-only, `status='active'`)",
        f"- Selected: {result.selected}",
        f"- Succeeded: {result.succeeded}",
        f"- Failed: {result.failed}",
        "- Emails sent: no | Forms submitted: no | Email DB writes: 0",
        "",
        "## Output paths",
        "",
    ]
    for key, value in data["output_paths"].items():
        lines.append(f"- {key}: `{value}`")
    lines += ["", "## Audits", "", "| Company | Domain | Score | Public URL |", "| --- | --- | --- | --- |"]
    for audit in data["audits"]:
        score = "n/a" if audit["score"] is None else str(audit["score"])
        lines.append(
            f"| {audit['company']} | {audit['domain']} | {score} | {audit['public_url']} |"
        )
    lines += ["", "## Failures", ""]
    if data["failures"]:
        for failure in data["failures"]:
            lines.append(f"- `{failure['domain']}` (row {failure['source_row_id']}): {failure['error']}")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def write_operational_report(result: BatchResult, report_dir: str) -> Dict[str, str]:
    stem = f"PROSPECT_AUDIT_BATCH_{_today()}"
    base = Path(report_dir)
    md_path = _write(base / f"{stem}.md", render_operational_report(result))
    json_path = _write(
        base / f"{stem}.json",
        json.dumps(result_to_dict(result), indent=2, ensure_ascii=False) + "\n",
    )
    return {"markdown": md_path, "json": json_path}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Prospect Audit batch (read-only source, no email).")
    parser.add_argument("--source-db", default=DEFAULT_SOURCE_DB)
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--output-root", default="var/prospect_audit/batches")
    parser.add_argument("--handoff-dir", default="var/prospect_audit/handoff")
    parser.add_argument("--report-dir", default="reports/runtime")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--url-salt", default="prospect-audit-batch-001")
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    parser.add_argument("--contact-form-url", default=DEFAULT_CONTACT_FORM_URL)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    cfg = BatchConfig(
        source_db=args.source_db,
        source_table=args.source_table,
        batch_size=args.batch_size,
        output_root=args.output_root,
        handoff_dir=args.handoff_dir,
        report_dir=args.report_dir,
        batch_id=args.batch_id,
        url_salt=args.url_salt,
        public_base_url=args.public_base_url,
        contact_form_url=args.contact_form_url,
    )
    try:
        result = run_batch(cfg)
    except (BatchSafetyError, BatchSourceError) as exc:
        print(f"prospect-audit batch aborted: {exc}", file=sys.stderr)
        return 2
    paths = write_operational_report(result, cfg.report_dir)
    print(
        json.dumps(
            {
                "batch_id": result.batch_id,
                "selected": result.selected,
                "succeeded": result.succeeded,
                "failed": result.failed,
                "report": paths,
                "output_paths": result.output_paths,
            },
            indent=2,
        )
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
