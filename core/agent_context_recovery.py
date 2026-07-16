"""Read-only recovery and verification of durable agent context.

This module answers a single question for each registered project: does the
durable agent context (PROJECT_STATE.md, HANDOFF.md, conversation references,
tmux/session visibility) still exist, and if not, what exactly is missing?

Hard scope guard: nothing in this module writes inside a project `root`. Any
reconstructed context is emitted as a *draft* under the runtime repository's
reports/ tree so a human can review it before it is copied anywhere. There is no
code path here that deploys, sends messages, mutates production data, or edits
configuration of acap, mess, email or JobHunter.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

CONTEXT_FILES: Sequence[str] = ("PROJECT_STATE.md", "HANDOFF.md")

CONVERSATION_GLOBS: Sequence[str] = (
    "CONVERSATION*.md",
    "HANDOFF*.md",
    "conversations/*",
    ".claude/**/*.jsonl",
    ".claude/**/*.json",
    "*.session.json",
)

MAX_CONVERSATION_HITS = 25

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_MISSING = "missing"
STATUS_ABSENT = "absent"

DEFAULT_REGISTRY: Dict[str, Any] = {
    "version": 1,
    "projects": [
        {"name": "acap", "root": "/root/acap", "protected": True, "tmux_session": "acap"},
        {"name": "mess", "root": "/root/mess", "protected": True, "tmux_session": "mess"},
        {"name": "email", "root": "/root/email", "protected": False, "tmux_session": "email"},
        {
            "name": "JobHunter",
            "root": "/root/JobHunter",
            "protected": False,
            "tmux_session": "jobhunter",
        },
    ],
}

DEFAULT_REGISTRY_PATH = "config/agent_context_registry.yaml"
DEFAULT_OUTPUT_DIR = "reports/runtime/context_recovery"


@dataclass
class ProjectEntry:
    name: str
    root: str
    protected: bool = False
    tmux_session: Optional[str] = None


@dataclass
class ProjectStatus:
    name: str
    root: str
    protected: bool
    root_exists: bool
    status: str
    context_files: Dict[str, Any] = field(default_factory=dict)
    conversation_refs: List[str] = field(default_factory=list)
    tmux_session: Optional[str] = None
    tmux_alive: Optional[bool] = None
    git_head: Optional[str] = None
    blockers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root,
            "protected": self.protected,
            "root_exists": self.root_exists,
            "status": self.status,
            "context_files": self.context_files,
            "conversation_refs": self.conversation_refs,
            "tmux_session": self.tmux_session,
            "tmux_alive": self.tmux_alive,
            "git_head": self.git_head,
            "blockers": self.blockers,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_registry(path: Optional[str] = None) -> List[ProjectEntry]:
    """Load the project registry, falling back to the embedded default.

    The YAML dependency is optional: a missing or unparsable file degrades to the
    built-in list rather than failing the whole recovery run.
    """
    data: Optional[Dict[str, Any]] = None
    if path:
        candidate = Path(path)
        if candidate.is_file():
            try:
                import yaml  # type: ignore

                loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = None
    if data is None:
        data = DEFAULT_REGISTRY
    entries: List[ProjectEntry] = []
    for raw in data.get("projects", []) or []:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("root"):
            continue
        entries.append(
            ProjectEntry(
                name=str(raw["name"]),
                root=str(raw["root"]),
                protected=bool(raw.get("protected", False)),
                tmux_session=raw.get("tmux_session") or None,
            )
        )
    return entries


def tmux_sessions() -> List[str]:
    """Return live tmux session names. Read-only; empty list if tmux is unavailable."""
    binary = shutil.which("tmux")
    if not binary:
        return []
    try:
        proc = subprocess.run(
            [binary, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _git_head(root: Path) -> Optional[str]:
    if not (root / ".git").exists():
        return None
    binary = shutil.which("git")
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, "-C", str(root), "log", "-1", "--pretty=%h %s"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _describe_file(path: Path) -> Dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"present": False}
    return {
        "present": True,
        "bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "empty": stat.st_size == 0,
    }


def find_conversation_refs(root: Path) -> List[str]:
    hits: List[str] = []
    for pattern in CONVERSATION_GLOBS:
        try:
            matches = sorted(root.glob(pattern))
        except Exception:
            continue
        for match in matches:
            if not match.is_file():
                continue
            rel = str(match.relative_to(root))
            if rel not in hits:
                hits.append(rel)
            if len(hits) >= MAX_CONVERSATION_HITS:
                return hits
    return hits


def scan_project(entry: ProjectEntry, live_sessions: Sequence[str]) -> ProjectStatus:
    """Inspect one project without modifying anything under its root."""
    root = Path(entry.root)
    status = ProjectStatus(
        name=entry.name,
        root=str(root),
        protected=entry.protected,
        root_exists=root.is_dir(),
        status=STATUS_ABSENT,
        tmux_session=entry.tmux_session,
    )

    if entry.tmux_session is not None:
        status.tmux_alive = entry.tmux_session in live_sessions

    if not status.root_exists:
        status.blockers.append(f"project root {root} does not exist or is not readable")
        return status

    missing: List[str] = []
    for name in CONTEXT_FILES:
        info = _describe_file(root / name)
        status.context_files[name] = info
        if not info.get("present"):
            missing.append(name)
        elif info.get("empty"):
            missing.append(f"{name} (empty)")

    status.conversation_refs = find_conversation_refs(root)
    status.git_head = _git_head(root)

    if not missing and status.conversation_refs:
        status.status = STATUS_OK
    elif len(missing) >= len(CONTEXT_FILES):
        status.status = STATUS_MISSING
    else:
        status.status = STATUS_DEGRADED

    for item in missing:
        status.blockers.append(f"missing durable context: {item}")
    if not status.conversation_refs:
        status.blockers.append("no conversation references found")
    if status.tmux_alive is False:
        status.blockers.append(f"tmux session '{entry.tmux_session}' not visible")
    return status


def draft_reconstruction(status: ProjectStatus, generated_at: datetime) -> str:
    """Render a review-only PROJECT_STATE/HANDOFF draft for a project.

    The draft is intentionally full of explicit UNKNOWN markers: it records what
    the scanner could observe and refuses to invent project history.
    """
    refs = status.conversation_refs or []
    lines = [
        f"# {status.name} — reconstructed agent context (DRAFT, review before use)",
        "",
        f"Generated: {generated_at.isoformat()}",
        f"Source: core/agent_context_recovery.py (read-only scan of {status.root})",
        "",
        "> This draft was reconstructed from filesystem evidence only. It has NOT been",
        "> copied into the project. Nothing here is authoritative until a human confirms it.",
        "",
        "## Observed state",
        "",
        f"- Root exists: {status.root_exists}",
        f"- Context status: {status.status}",
        f"- Git HEAD: {status.git_head or 'UNKNOWN (no git metadata readable)'}",
        f"- tmux session: {status.tmux_session or 'n/a'} "
        f"(alive: {status.tmux_alive if status.tmux_alive is not None else 'unknown'})",
        "",
        "## Durable context files",
        "",
    ]
    for name in CONTEXT_FILES:
        info = status.context_files.get(name, {"present": False})
        if info.get("present"):
            lines.append(
                f"- `{name}`: present, {info.get('bytes')} bytes, modified {info.get('modified_utc')}"
            )
        else:
            lines.append(f"- `{name}`: MISSING — needs reconstruction")
    lines += ["", "## Conversation references", ""]
    if refs:
        lines += [f"- `{ref}`" for ref in refs]
    else:
        lines.append("- none found — conversation history is UNKNOWN")
    lines += [
        "",
        "## Current work (UNKNOWN — fill in from conversation history)",
        "",
        "- Goal: UNKNOWN",
        "- In progress: UNKNOWN",
        "- Next step: UNKNOWN",
        "",
        "## Blockers observed by the scanner",
        "",
    ]
    lines += [f"- {b}" for b in status.blockers] or ["- none"]
    lines.append("")
    return "\n".join(lines)


def build_report(
    entries: Sequence[ProjectEntry],
    live_sessions: Optional[Sequence[str]] = None,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    sessions = list(live_sessions if live_sessions is not None else tmux_sessions())
    now = generated_at or _utcnow()
    statuses = [scan_project(entry, sessions) for entry in entries]
    protected_blocked = [
        s.name for s in statuses if s.protected and s.status in (STATUS_MISSING, STATUS_ABSENT)
    ]
    return {
        "generated_at": now.isoformat(),
        "tool": "core/agent_context_recovery.py",
        "mode": "read-only",
        "live_tmux_sessions": sessions,
        "projects": [s.to_dict() for s in statuses],
        "summary": {
            "total": len(statuses),
            "ok": sum(1 for s in statuses if s.status == STATUS_OK),
            "degraded": sum(1 for s in statuses if s.status == STATUS_DEGRADED),
            "missing": sum(1 for s in statuses if s.status == STATUS_MISSING),
            "absent": sum(1 for s in statuses if s.status == STATUS_ABSENT),
            "protected_needing_reconstruction": protected_blocked,
        },
    }


def render_markdown(report: Dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Agent context recovery report",
        "",
        f"Generated: {report['generated_at']}",
        f"Mode: {report['mode']} (no product changes, no deploys, no messages)",
        "",
        "## Summary",
        "",
        f"- Projects scanned: {summary['total']}",
        f"- ok: {summary['ok']} | degraded: {summary['degraded']} "
        f"| missing: {summary['missing']} | absent: {summary['absent']}",
        f"- Live tmux sessions: {', '.join(report['live_tmux_sessions']) or 'none visible'}",
    ]
    blocked = summary["protected_needing_reconstruction"]
    lines.append(
        "- Protected projects needing reconstruction: " + (", ".join(blocked) if blocked else "none")
    )
    lines += ["", "## Per-project status", "", "| Project | Protected | Status | tmux | PROJECT_STATE.md | HANDOFF.md | Conversation refs |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for project in report["projects"]:
        files = project["context_files"]

        def mark(name: str) -> str:
            info = files.get(name) or {}
            if not info.get("present"):
                return "missing"
            return "empty" if info.get("empty") else "present"

        tmux = project["tmux_alive"]
        tmux_text = "n/a" if tmux is None else ("alive" if tmux else "not visible")
        lines.append(
            "| {name} | {prot} | {status} | {tmux} | {ps} | {ho} | {refs} |".format(
                name=project["name"],
                prot="yes" if project["protected"] else "no",
                status=project["status"],
                tmux=tmux_text,
                ps=mark("PROJECT_STATE.md"),
                ho=mark("HANDOFF.md"),
                refs=len(project["conversation_refs"]),
            )
        )
    lines += ["", "## Blockers", ""]
    any_blockers = False
    for project in report["projects"]:
        if not project["blockers"]:
            continue
        any_blockers = True
        lines.append(f"### {project['name']}")
        lines += [f"- {b}" for b in project["blockers"]]
        lines.append("")
    if not any_blockers:
        lines += ["- none", ""]
    return "\n".join(lines)


def write_outputs(
    report: Dict[str, Any],
    statuses_drafts: bool = True,
    output_dir: str = DEFAULT_OUTPUT_DIR,
) -> List[str]:
    """Write report + drafts into the runtime repo only. Returns written paths."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"][:10]
    written: List[str] = []

    json_path = out / f"AGENT_CONTEXT_RECOVERY_{stamp}.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(str(json_path))

    md_path = out / f"AGENT_CONTEXT_RECOVERY_{stamp}.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    written.append(str(md_path))

    if statuses_drafts:
        generated_at = datetime.fromisoformat(report["generated_at"])
        drafts = out / "drafts"
        for raw in report["projects"]:
            if not raw["protected"] or raw["status"] == STATUS_OK:
                continue
            status = ProjectStatus(
                name=raw["name"],
                root=raw["root"],
                protected=raw["protected"],
                root_exists=raw["root_exists"],
                status=raw["status"],
                context_files=raw["context_files"],
                conversation_refs=raw["conversation_refs"],
                tmux_session=raw["tmux_session"],
                tmux_alive=raw["tmux_alive"],
                git_head=raw["git_head"],
                blockers=raw["blockers"],
            )
            drafts.mkdir(parents=True, exist_ok=True)
            draft_path = drafts / f"{status.name}_PROJECT_STATE.draft.md"
            draft_path.write_text(draft_reconstruction(status, generated_at), encoding="utf-8")
            written.append(str(draft_path))
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only agent context recovery scan.")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--json", action="store_true", help="print the report as JSON to stdout")
    parser.add_argument("--no-write", action="store_true", help="do not write report files")
    args = parser.parse_args(list(argv) if argv is not None else None)

    entries = load_registry(args.registry)
    report = build_report(entries)

    if not args.no_write:
        for path in write_outputs(report, output_dir=args.output_dir):
            print(f"wrote {path}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))

    # Exit code signals attention needed for protected projects, not tool failure.
    return 1 if report["summary"]["protected_needing_reconstruction"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
