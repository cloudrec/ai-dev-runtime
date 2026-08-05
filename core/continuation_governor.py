"""Phase 3 continuation governor — end the stop-and-wait stall.

The 2026-08-05 soak caught the exact pattern: `mess-qa-automation:0.0` sat at
`waiting_input` for 37 minutes holding `[Pasted text #3 +99 lines]`. The watchdog refused
to submit it — correctly, since the structural allowlist cannot classify an opaque paste —
and the project simply stopped.

This module governs that situation without inventing work:

  * QUEUED-BUT-UNSUBMITTED — text the OWNER already put in the input line. Submitting it
    restores the owner's own instruction; it does not author anything. Opaque pastes are
    submitted only where the project config opts in, and every submission is audited.
  * COMPLETED STAGE — advance only to an item written down in the project's durable queue.
    No queue, no advancement.
  * MISSING DESIGN PAYLOAD — a declared source that does not exist is a genuine owner
    blocker naming the exact missing file. Never fabricated, never spun on.

Everything is decision-only: the caller performs delivery through the existing lease-gated,
verified path, so one-copy semantics, dedupe and the safety classifier all still apply.
"""
from __future__ import annotations

import os
import re
from typing import Optional

CONFIG_PATH = os.getenv("PROJECT_QUEUES_CONFIG",
                        "/root/ai-dev-runtime/config/project_queues.yaml")

# Markers Claude Code renders for input that is typed/pasted but NOT submitted.
PASTED_RE = re.compile(r"\[pasted text[^\]]*\]", re.I)
QUEUED_HINT_RE = re.compile(r"press up to edit queued message|queued message", re.I)


def load_config(path: Optional[str] = None) -> dict:
    """Fail-closed: an unreadable config governs nothing."""
    try:
        import yaml
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("projects") or {}
    except Exception:  # noqa: BLE001
        return {}


def detect_queued_input(*, pending: str = "", tail: str = "") -> dict:
    """What, if anything, is sitting unsubmitted in the input line?

    Returns {"queued": bool, "kind": "text"|"paste"|"", "text": str, "evidence": str}.
    `pending` comes from the ghost-aware styled reader, so the dim recall suggestion is
    already excluded by the caller.
    """
    p = (pending or "").strip()
    t = tail or ""
    if p:
        if PASTED_RE.search(p):
            return {"queued": True, "kind": "paste", "text": p, "evidence": "pending_paste"}
        return {"queued": True, "kind": "text", "text": p, "evidence": "pending_text"}
    m = PASTED_RE.search(t)
    if m:
        return {"queued": True, "kind": "paste", "text": m.group(0),
                "evidence": "pasted_marker_in_pane"}
    if QUEUED_HINT_RE.search(t):
        return {"queued": True, "kind": "paste", "text": "",
                "evidence": "queued_message_hint"}
    return {"queued": False, "kind": "", "text": "", "evidence": ""}


def queue_sources(target: str, config: Optional[dict] = None) -> dict:
    """Which declared sources exist, and which are missing."""
    cfg = (config if config is not None else load_config()).get(target) or {}
    required = list(cfg.get("required_sources") or [])
    missing = [p for p in required if not os.path.isfile(p)]
    return {"configured": bool(cfg), "enabled": bool(cfg.get("enabled")),
            "required": required, "missing": missing,
            "pointer": cfg.get("authoritative_pointer") or "",
            "pointer_section": cfg.get("pointer_section") or "",
            "submit_owner_queued_paste": bool(cfg.get("submit_owner_queued_paste")),
            "cwd": cfg.get("cwd") or ""}


def read_pointer(target: str, config: Optional[dict] = None) -> dict:
    """The current queue item from the authoritative pointer file.

    Deliberately conservative: it returns the section verbatim for the caller to ground a
    decision on. It never synthesises an instruction.
    """
    src = queue_sources(target, config)
    if not src["configured"] or not src["enabled"]:
        return {"ok": False, "reason": "project_not_configured"}
    if src["missing"]:
        return {"ok": False, "reason": "missing_required_sources",
                "missing": src["missing"],
                "blocker_fields": [f"file:{p}" for p in src["missing"]]}
    path = src["pointer"]
    if not path:
        return {"ok": False, "reason": "no_authoritative_pointer"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"pointer_unreadable:{e}"}
    section = src["pointer_section"]
    if section:
        m = re.search(rf"^#{{1,6}}.*{re.escape(section)}.*$", text, re.M | re.I)
        if not m:
            return {"ok": False, "reason": "pointer_section_absent",
                    "blocker_fields": [f"section:{section} in {path}"]}
        rest = text[m.end():]
        nxt = re.search(r"^#{1,3}\s", rest, re.M)
        body = rest[:nxt.start()] if nxt else rest
        return {"ok": True, "pointer_path": path, "section": section,
                "body": body.strip()[:4000], "mtime": os.path.getmtime(path)}
    return {"ok": True, "pointer_path": path, "section": "", "body": text[:4000],
            "mtime": os.path.getmtime(path)}


def govern(target: str, *, state: str, pending: str = "", tail: str = "",
           stage_complete: bool = False, config: Optional[dict] = None) -> dict:
    """The decision. Never returns work that is not already queued or written down."""
    cfg_all = config if config is not None else load_config()
    src = queue_sources(target, cfg_all)
    if not src["configured"]:
        return {"action": "skip", "reason": "project_not_governed"}
    if not src["enabled"]:
        return {"action": "skip", "reason": "governor_disabled_for_project"}

    q = detect_queued_input(pending=pending, tail=tail)

    # 1) Owner-queued input that was never submitted — the stall this exists to end.
    if q["queued"] and state in ("idle", "waiting_input", "waiting_owner"):
        if q["kind"] == "paste" and not src["submit_owner_queued_paste"]:
            return {"action": "blocker", "reason": "owner_paste_not_auto_submittable",
                    "detail": q, "owner_blocker": True,
                    "note": "an opaque owner paste is queued; this project does not opt in "
                            "to submitting pastes automatically"}
        # SUBMIT THE EXISTING LINE — never re-send its text. For a paste the pane shows
        # only a placeholder like "[Pasted text #3 +99 lines]"; sending that string would
        # type the placeholder instead of submitting the owner's real content. The caller
        # presses Enter on what is already there.
        return {"action": "submit_queued", "mode": "enter",
                "reason": f"queued_{q['kind']}_unsubmitted",
                "expected_pending": q["text"], "detail": q,
                "note": "presses Enter on the OWNER'S already-queued input exactly once; "
                        "never re-sends or authors text"}

    # 2) Nothing queued and the stage is done — advance only on written-down work.
    if stage_complete and state in ("idle", "waiting_owner"):
        ptr = read_pointer(target, cfg_all)
        if not ptr.get("ok"):
            return {"action": "blocker", "reason": ptr.get("reason"),
                    "missing": ptr.get("missing"),
                    "blocker_fields": ptr.get("blocker_fields"),
                    "owner_blocker": True,
                    "note": "next work needs an owner-authored source that is absent; "
                            "no design is fabricated"}
        return {"action": "advance_queue", "reason": "stage_complete_with_grounded_next",
                "pointer_path": ptr["pointer_path"], "queue_excerpt": ptr["body"][:600],
                "note": "the next item is quoted from the durable queue, not invented"}

    return {"action": "skip", "reason": "nothing_queued_and_stage_incomplete"}
