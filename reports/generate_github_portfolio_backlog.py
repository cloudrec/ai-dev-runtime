#!/usr/bin/env python3
"""Generate GITHUB_PORTFOLIO_BACKLOG_2026-07-15 reports (read-only audit).

Enumerates open and recently closed GitHub issues across the cloudrec org
using the server's already-authenticated `gh` CLI, reconciles them against the
portfolio domains the owner cares about, and writes two reports:

    reports/GITHUB_PORTFOLIO_BACKLOG_2026-07-15.md
    reports/GITHUB_PORTFOLIO_BACKLOG_2026-07-15.json

Strictly READ-ONLY. It never creates, edits, closes, labels or comments on any
GitHub issue, and never modifies Owner OS tasks. It only reads via `gh` and
writes the two local report files.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPORT_DATE = "2026-07-15"
ORG = "cloudrec"
REPORTS_DIR = Path(__file__).resolve().parent

# Repositories that get special attention in the audit.
PRIORITY_REPOS = [
    "cloudrec/ai-dev-runtime",
    "cloudrec/books",
    "cloudrec/email",
    "cloudrec/clients-help",
]

# Portfolio domains -> keyword signals used to categorize each issue.
DOMAINS = {
    "books-translations-publishing": [
        "book", "translation", "translate", "illustration", "illustrat", "publish"
    ],
    "cross-platform-promotion": [
        "youtube", "tiktok", "instagram", "facebook", "reddit", "telegram",
        "blog", "promo", "promotion", "social"
    ],
    "third-party-website-audit": [
        "website audit", "site audit", "ux", "conversion", "performance",
        "outreach", "lighthouse"
    ],
    "portfolio-revenue-os": ["revenue", "portfolio revenue", "monetiz"],
    "project-inventory-map": ["inventory", "project map", "project-map"],
    "migration-grade-backups": ["backup", "migration", "restore"],
    "prospect-scout-lead-gen": ["prospect", "lead", "scout", "lead gen"],
    "clients-help": ["clients.help", "clients-help", "clientshelp", "support"],
    "seo-os": ["seo"],
    "youtube-factory": ["youtube factory", "video factory"],
    "email-platform": ["email", "smtp", "newsletter", "inbox"],
    "jobhunter-ai": ["jobhunter", "job hunter", "job hunt"],
    "partner-system": ["partner"],
}

# Domain -> local project path/domain hint (best-effort, read-only).
DOMAIN_PATHS = {
    "seo-os": "/opt/seo-backend",
    "email-platform": "/opt/email",
    "clients-help": "/opt/clients-help",
    "portfolio-revenue-os": "/opt/revenue-os",
}

PRIORITY_LABELS = {
    "p0": "P0", "critical": "P0", "urgent": "P0",
    "p1": "P1", "high": "P1",
    "p2": "P2", "medium": "P2",
    "p3": "P3", "low": "P3",
}


def _run_gh(args: list[str]) -> str | None:
    """Run a read-only `gh` command, returning stdout or None on failure."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def list_repos() -> list[str]:
    """Return full repo names (owner/name) for the org, priority repos first."""
    out = _run_gh([
        "repo", "list", ORG, "--limit", "200",
        "--json", "nameWithOwner", "-q", ".[].nameWithOwner",
    ])
    repos: list[str] = []
    if out:
        repos = [line.strip() for line in out.splitlines() if line.strip()]
    # Ensure priority repos are present and ordered first.
    ordered = [r for r in PRIORITY_REPOS if r in repos or True]
    for r in repos:
        if r not in ordered:
            ordered.append(r)
    return ordered


def list_issues(repo: str) -> list[dict]:
    """Return open + recently updated closed issues for a repo (read-only)."""
    fields = "number,title,state,labels,createdAt,updatedAt,url,body"
    issues: list[dict] = []
    for state in ("open", "closed"):
        limit = "300" if state == "open" else "60"
        out = _run_gh([
            "issue", "list", "--repo", repo, "--state", state,
            "--limit", limit, "--json", fields,
        ])
        if not out:
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        for item in data:
            item["repo"] = repo
            issues.append(item)
    return issues


def categorize(issue: dict) -> str:
    haystack = " ".join([
        issue.get("title", ""),
        issue.get("body", "") or "",
        " ".join(lbl.get("name", "") for lbl in issue.get("labels", [])),
        issue.get("repo", ""),
    ]).lower()
    for domain, keywords in DOMAINS.items():
        if any(kw in haystack for kw in keywords):
            return domain
    return "uncategorized"


def derive_priority(issue: dict) -> str:
    for lbl in issue.get("labels", []):
        name = lbl.get("name", "").lower()
        if name in PRIORITY_LABELS:
            return PRIORITY_LABELS[name]
    return "P2"


def derive_next_action(issue: dict) -> str:
    if issue.get("state", "").lower() == "closed":
        return "Verify completion; archive if fully delivered"
    return "Triage + assign to owning runtime job"


def find_linked(issue: dict) -> str | None:
    body = (issue.get("body", "") or "") + " " + issue.get("title", "")
    lower = body.lower()
    for marker in ("ownertask", "owner task", "runtime job", "job "):
        idx = lower.find(marker)
        if idx != -1:
            return body[idx:idx + 40].strip()
    return None


def build_record(issue: dict) -> dict:
    domain = categorize(issue)
    return {
        "repo": issue.get("repo"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "labels": [lbl.get("name") for lbl in issue.get("labels", [])],
        "created_at": issue.get("createdAt"),
        "updated_at": issue.get("updatedAt"),
        "url": issue.get("url"),
        "linked": find_linked(issue),
        "project_path_domain": DOMAIN_PATHS.get(domain, domain),
        "category": domain,
        "priority": derive_priority(issue),
        "next_action": derive_next_action(issue),
    }


def is_stale(record: dict, now: datetime) -> bool:
    updated = record.get("updated_at")
    if not updated:
        return False
    try:
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return False
    return record.get("state") == "open" and (now - dt).days > 30


def reconcile(records: list[dict], now: datetime) -> dict:
    domains_seen = {r["category"] for r in records}
    missing_domains = [d for d in DOMAINS if d not in domains_seen]
    seen_keys: dict[tuple, int] = {}
    duplicates: list[dict] = []
    for r in records:
        key = (r["repo"], (r["title"] or "").strip().lower())
        seen_keys[key] = seen_keys.get(key, 0) + 1
    for r in records:
        key = (r["repo"], (r["title"] or "").strip().lower())
        if seen_keys[key] > 1:
            duplicates.append({"repo": r["repo"], "number": r["number"], "title": r["title"]})
    stale = [r for r in records if is_stale(r, now)]
    completed = [r for r in records if (r.get("state") or "").lower() == "closed"]
    return {
        "missing_domains_without_issue": missing_domains,
        "duplicate_candidates": duplicates,
        "stale_open_issues": [
            {"repo": r["repo"], "number": r["number"], "title": r["title"]} for r in stale
        ],
        "completed_recently_closed": [
            {"repo": r["repo"], "number": r["number"], "title": r["title"]} for r in completed
        ],
    }


def render_markdown(payload: dict) -> str:
    meta = payload["metadata"]
    lines = [
        f"# GitHub Portfolio Backlog — {REPORT_DATE}",
        "",
        "Read-only audit of cloudrec GitHub issues reconciled against portfolio domains.",
        "No issues or Owner OS tasks were modified.",
        "",
        "## Counts",
        f"- Repositories scanned: {meta['repo_count']}",
        f"- Total issues inventoried: {meta['issue_count']}",
        f"- Open: {meta['open_count']}  |  Closed (recent): {meta['closed_count']}",
        "",
        "## Issues",
        "",
        "| Repo | # | Title | State | Category | Priority | Next action |",
        "|------|---|-------|-------|----------|----------|-------------|",
    ]
    for r in payload["issues"]:
        title = (r["title"] or "").replace("|", "\\|")
        lines.append(
            f"| {r['repo']} | {r['number']} | {title} | {r['state']} | "
            f"{r['category']} | {r['priority']} | {r['next_action']} |"
        )
    rec = payload["reconciliation"]
    lines += [
        "",
        "## Reconciliation vs Owner OS domains",
        "",
        f"- Domains with no GitHub issue: {', '.join(rec['missing_domains_without_issue']) or 'none'}",
        f"- Duplicate candidates: {len(rec['duplicate_candidates'])}",
        f"- Stale open issues (>30d): {len(rec['stale_open_issues'])}",
        f"- Completed / recently closed: {len(rec['completed_recently_closed'])}",
        "",
    ]
    return "\n".join(lines) + "\n"


def generate() -> dict:
    now = datetime.now(timezone.utc)
    repos = list_repos()
    raw_issues: list[dict] = []
    for repo in repos:
        raw_issues.extend(list_issues(repo))
    records = [build_record(i) for i in raw_issues]
    open_count = sum(1 for r in records if (r.get("state") or "").lower() == "open")
    closed_count = sum(1 for r in records if (r.get("state") or "").lower() == "closed")
    payload = {
        "metadata": {
            "report_date": REPORT_DATE,
            "generated_at": now.isoformat(),
            "org": ORG,
            "priority_repos": PRIORITY_REPOS,
            "repo_count": len(repos),
            "issue_count": len(records),
            "open_count": open_count,
            "closed_count": closed_count,
            "read_only": True,
        },
        "issues": records,
        "reconciliation": reconcile(records, now),
    }
    return payload


def write_reports(payload: dict) -> tuple[Path, Path]:
    json_path = REPORTS_DIR / f"GITHUB_PORTFOLIO_BACKLOG_{REPORT_DATE}.json"
    md_path = REPORTS_DIR / f"GITHUB_PORTFOLIO_BACKLOG_{REPORT_DATE}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return md_path, json_path


def main() -> int:
    payload = generate()
    md_path, json_path = write_reports(payload)
    meta = payload["metadata"]
    print(f"repos={meta['repo_count']} issues={meta['issue_count']} "
          f"open={meta['open_count']} closed={meta['closed_count']}")
    print(f"md={md_path}")
    print(f"json={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
