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


# Words that betray work belonging to ANOTHER project. Cross-project instructions are the
# failure this isolation exists to stop: payment work landing on a product agent, product
# work landing on payment, trading verbs reaching a paper-only session.
_SCOPE_MARKERS = {
    "payment": re.compile(r"\b(payment|payout|invoice|charge|refund|billing|wayforpay|"
                          r"merchant|standby|replication)\b", re.I),
    "jobhunter": re.compile(r"\b(jobhunter|job\.clients\.help|vacanc|resume|applicant|"
                            r"microtask)\b", re.I),
    "mess_ui": re.compile(r"\b(mess|messenger|redesign|screen[_ ]spec|copy_ru|apk)\b", re.I),
    "live_trading": re.compile(r"\b(live trade|real order|place order|mainnet|venue|"
                               r"exchange key|withdraw)\b", re.I),
    "publication": re.compile(r"\b(publish|release|deploy|push to prod)\b", re.I),
}


def project_policy(target: str, config: Optional[dict] = None) -> dict:
    cfg = (config if config is not None else load_config()).get(target) or {}
    return {"role": cfg.get("role") or "", "project": cfg.get("project") or "",
            "allowed_scopes": list(cfg.get("allowed_scopes") or []),
            "forbidden_scopes": list(cfg.get("forbidden_scopes") or [])}


def check_project_isolation(target: str, text: str,
                            config: Optional[dict] = None) -> dict:
    """Would this instruction take the project outside its own role?

    Returns {"allowed": bool, "reason": str, "scope": str}. Deny-by-default on a match
    against a forbidden scope — the governor never continues another project's work.
    """
    pol = project_policy(target, config)
    if not pol["project"]:
        return {"allowed": False, "reason": "project_not_governed", "scope": ""}
    body = text or ""
    for scope, rx in _SCOPE_MARKERS.items():
        if scope in pol["forbidden_scopes"] and rx.search(body):
            return {"allowed": False, "reason": "cross_project_work_refused",
                    "scope": scope}
    return {"allowed": True, "reason": "within_project_role", "scope": pol["role"]}


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


# ── the real MESS queue format (owner-authored 2026-08-05) ──────────────────
# ## POINTER
# ```
# CURRENT_STAGE: 2  — Invites
# LAST_COMPLETED: V7 Group Flows (…)
# BRANCH: fable-0.1.91-realdevice-ux
# ```
# ## STAGES
# ### 2. Invites — CURRENT
_POINTER_BLOCK_RE = re.compile(r"^##+\s*POINTER\s*$(.*?)^##+\s", re.M | re.S)
_CURRENT_STAGE_RE = re.compile(r"^\s*CURRENT_STAGE:\s*(\d+)\s*(?:[—\-–]\s*(.+?))?\s*$", re.M)
_LAST_COMPLETED_RE = re.compile(r"^\s*LAST_COMPLETED:\s*(.+?)\s*$", re.M)
_BRANCH_RE = re.compile(r"^\s*BRANCH:\s*(\S+)", re.M)
_STAGE_HEAD_RE = re.compile(r"^###\s*(\d+)\.\s*(.+?)\s*$", re.M)
# A RECORDED blocker starts its own line (optionally bold/backticked), per the queue's own
# convention. The token also appears inside instructions ("if absent, record
# NEEDS_OWNER_PAYLOAD with the exact missing fields") — matching those would raise a
# blocker for a stage the agent is actively working. Anchor to line start.
_NEEDS_PAYLOAD_RE = re.compile(r"^\s*[*_`]{0,3}NEEDS_OWNER_PAYLOAD\b", re.I | re.M)
_RESOLVED_RE = re.compile(r"^\s*[*_`]{0,3}NEEDS_OWNER_PAYLOAD[^\n]{0,60}RESOLVED",
                          re.I | re.M)


_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*(.*?)```", re.S | re.I)
_RESUME_SECTION_RE = re.compile(
    r"^##+[^\n]*RESUME AFTER[^\n]*$\n(.*?)(?=^##+\s|\Z)", re.M | re.S | re.I)


def resume_instruction(path: str) -> str:
    """The owner's verbatim post-`/clear` instruction, quoted from the queue itself.

    Requirement 4 says the resume text must be the durable one, not something composed
    here, so this returns the blockquote lines exactly as written.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:  # noqa: BLE001
        return ""
    m = _RESUME_SECTION_RE.search(text)
    if not m:
        return ""
    lines = [re.sub(r"^>\s?", "", ln).strip() for ln in m.group(1).splitlines()
             if ln.strip().startswith(">")]
    return " ".join(x for x in lines if x)[:1200]


def parse_queue_yaml(path: str) -> dict:
    """Parse the MACHINE-READABLE STATE block. This is the authoritative form once the
    owner's agent has written it; the markdown parser below stays as a fallback."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"queue_unreadable:{e}"}
    m = _YAML_BLOCK_RE.search(text)
    if not m:
        return {"ok": False, "reason": "no_machine_readable_block"}
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except Exception as e:  # noqa: BLE001
        # mid-write files are expected; the caller polls rather than acting
        return {"ok": False, "reason": f"yaml_invalid:{str(e)[:80]}"}
    if not isinstance(data, dict):
        return {"ok": False, "reason": "yaml_not_a_mapping"}

    pointer = str(data.get("pointer") or "")
    stages = data.get("stages") or []
    if not pointer:
        return {"ok": False, "reason": "pointer_missing",
                "blocker_fields": ["field:pointer"]}
    if not isinstance(stages, list) or not stages:
        return {"ok": False, "reason": "no_stages",
                "blocker_fields": ["field:stages"]}
    ids = [str(s.get("id")) for s in stages if isinstance(s, dict)]
    if pointer not in ids:
        return {"ok": False, "reason": "pointer_stage_not_in_stages",
                "blocker_fields": [f"pointer:{pointer} not among {ids[:6]}"]}
    cur = next(s for s in stages if isinstance(s, dict) and str(s.get("id")) == pointer)
    status = str(cur.get("status") or "").upper()
    # The real queue records a missing payload in the `payload` field (and/or `blockers`)
    # while `status` stays CURRENT — checking status alone missed it live on stage 3 and
    # would have idled silently instead of raising the blocker.
    payload_field = cur.get("payload")
    payload_txt = (payload_field if isinstance(payload_field, str)
                   else " ".join(str(v) for v in (payload_field or {}).values())
                   if isinstance(payload_field, dict) else "")
    blockers_txt = " ".join(str(b) for b in (cur.get("blockers") or [])
                            if b is not None)
    # A stage can name a payload file AND still record `missing_fields` — stage 5 does
    # exactly that (payload: CALLS_AND_STATES_V3.json, three fields still missing). The
    # token-only check read that as "fully specified", so an idle agent parked on it was
    # never surfaced: the stop-and-wait stall, reintroduced. Recorded missing fields ARE
    # the gap, whatever the payload field says.
    recorded_missing = [str(x) for x in (cur.get("missing_fields") or []) if str(x).strip()]
    needs = bool(recorded_missing) or ("NEEDS_OWNER_PAYLOAD" in
                                       f"{status} {payload_txt} {blockers_txt}".upper())
    return {"ok": True, "format": "yaml", "path": path, "pointer": pointer,
            "current": cur, "current_status": status,
            "needs_owner_payload": needs,
            "missing_fields": list(cur.get("missing_fields") or []),
            "branch": str(data.get("branch") or ""), "cwd": str(data.get("cwd") or ""),
            "deploy_allowed": bool(data.get("deploy_allowed")),
            "completed": data.get("completed") or [],
            "stages": stages, "resume_instruction": resume_instruction(path),
            "mtime": os.path.getmtime(path)}


def parse_queue(path: str) -> dict:
    """Parse the durable execution queue and VALIDATE it against its own pointer.

    Returns the stages and pointer verbatim. It never rewrites or infers work: an
    unparsable or self-inconsistent queue is reported as invalid so the caller blocks.
    """
    y = parse_queue_yaml(path)
    if y.get("ok"):
        return y
    # Fall back to the markdown form ONLY when there is no machine-readable block at all.
    # If a YAML block exists but is mid-write or self-inconsistent, that is the real
    # answer — masking it with the markdown parser's error would make "wait, the owner's
    # agent is writing" indistinguishable from "this is a legacy file".
    if y.get("reason") != "no_machine_readable_block":
        return y
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"queue_unreadable:{e}"}

    pm = _POINTER_BLOCK_RE.search(text)
    block = pm.group(1) if pm else ""
    cm = _CURRENT_STAGE_RE.search(block)
    if not cm:
        return {"ok": False, "reason": "pointer_missing_current_stage",
                "blocker_fields": ["field:CURRENT_STAGE in POINTER"]}
    current = int(cm.group(1))
    current_name = (cm.group(2) or "").strip()
    lc = _LAST_COMPLETED_RE.search(block)
    br = _BRANCH_RE.search(block)

    stages = []
    heads = list(_STAGE_HEAD_RE.finditer(text))
    for i, h in enumerate(heads):
        body = text[h.end():heads[i + 1].start()] if i + 1 < len(heads) else text[h.end():]
        title = h.group(2)
        status = ""
        if "—" in title or "-" in title:
            parts = re.split(r"\s[—\-–]\s", title, maxsplit=1)
            if len(parts) == 2:
                title, status = parts[0].strip(), parts[1].strip()
        needs = bool(_NEEDS_PAYLOAD_RE.search(body)) and not _RESOLVED_RE.search(body)
        stages.append({"n": int(h.group(1)), "name": title, "status": status,
                       "needs_owner_payload": needs, "body": body.strip()[:2000]})

    if not stages:
        return {"ok": False, "reason": "no_stages_parsed",
                "blocker_fields": ["section:STAGES"]}
    if not any(s["n"] == current for s in stages):
        return {"ok": False, "reason": "pointer_stage_not_in_stages",
                "blocker_fields": [f"CURRENT_STAGE:{current} has no matching '### {current}.' stage"]}

    return {"ok": True, "path": path, "current_stage": current,
            "current_stage_name": current_name,
            "last_completed": (lc.group(1).strip() if lc else ""),
            "branch": (br.group(1) if br else ""),
            "stages": stages, "mtime": os.path.getmtime(path)}


def current_stage_entry(parsed: dict) -> Optional[dict]:
    if not parsed.get("ok"):
        return None
    return next((s for s in parsed["stages"] if s["n"] == parsed["current_stage"]), None)


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
        # PROJECT-ROLE ISOLATION: even the owner's own queued line is refused when it
        # would drag this project into another project's work.
        iso = check_project_isolation(target, q["text"], cfg_all)
        if not iso["allowed"]:
            return {"action": "blocker", "reason": "cross_project_work_refused",
                    "owner_blocker": True, "scope": iso["scope"], "detail": q,
                    "note": f"queued text looks like {iso['scope']} work; this project's "
                            f"role forbids it"}
        # SUBMIT THE EXISTING LINE — never re-send its text. For a paste the pane shows
        # only a placeholder like "[Pasted text #3 +99 lines]"; sending that string would
        # type the placeholder instead of submitting the owner's real content. The caller
        # presses Enter on what is already there.
        return {"action": "submit_queued", "mode": "enter",
                "reason": f"queued_{q['kind']}_unsubmitted",
                "expected_pending": q["text"], "detail": q,
                "note": "presses Enter on the OWNER'S already-queued input exactly once; "
                        "never re-sends or authors text"}

    # 2) Nothing queued — consult the durable queue. Advancement is grounded in the
    #    queue's own pointer and stage status; nothing here authors work.
    if state in ("idle", "waiting_owner"):
        qpath = src["pointer"]
        if qpath and qpath.endswith(".md") and os.path.isfile(qpath):
            q = parse_queue(qpath)
            if not q.get("ok"):
                # No machine-readable block AND a configured section ⇒ this project still
                # uses the legacy markdown pointer; fall through to that path. Anything
                # else (mid-write, invalid YAML, pointer/stage mismatch) is a WAIT — never
                # an invitation to improvise.
                legacy = (q.get("reason") in ("no_machine_readable_block",
                                              "pointer_missing_current_stage",
                                              "no_stages_parsed")
                          and bool(src.get("pointer_section")))
                if not legacy:
                    return {"action": "skip",
                            "reason": f"queue_not_valid:{q.get('reason')}",
                            "blocker_fields": q.get("blocker_fields")}
                q = {}
            if q and q.get("format") == "yaml":
                cur = q.get("current") or {}
                status = q.get("current_status") or ""
                if q.get("needs_owner_payload"):
                    missing = (q.get("missing_fields") or cur.get("missing_fields")
                               or cur.get("missing") or [])
                    return {"action": "blocker", "reason": "NEEDS_OWNER_PAYLOAD",
                            "owner_blocker": True, "stage": q.get("pointer"),
                            "blocker_fields": missing or ["see stage entry in the queue"],
                            "queue_path": qpath,
                            "note": "the queue itself records the payload as missing; "
                                    "no design is fabricated"}
                if status in ("IN_PROGRESS", "CURRENT"):
                    # A queue may define completion by ARTEFACT (the canary harness does:
                    # "a stage advances only when its own artefact exists on disk"). That
                    # is the queue's own rule, so honouring it is grounded, not invented.
                    art = cur.get("artefact")
                    art_path = (os.path.join(src.get("cwd") or "", str(art))
                                if art and not str(art).startswith("/") else str(art or ""))
                    if art and os.path.isfile(art_path):
                        nxt_id = cur.get("next_stage")
                        if not nxt_id:
                            return {"action": "skip", "reason": "queue_exhausted",
                                    "stage": q.get("pointer")}
                        nxt = next((x for x in (q.get("stages") or [])
                                    if str(x.get("id")) == str(nxt_id)), None)
                        if nxt is None:
                            return {"action": "blocker",
                                    "reason": "next_stage_not_in_queue",
                                    "owner_blocker": True,
                                    "blocker_fields": [f"next_stage:{nxt_id}"]}
                        nxt_missing = [str(x) for x in (nxt.get("missing_fields") or [])
                                       if str(x).strip()]
                        if nxt_missing or "NEEDS_OWNER_PAYLOAD" in str(nxt.get("payload") or "").upper():
                            return {"action": "blocker", "reason": "NEEDS_OWNER_PAYLOAD",
                                    "owner_blocker": True, "stage": str(nxt_id),
                                    "blocker_fields": nxt_missing,
                                    "queue_path": qpath}
                        instr = str(nxt.get("instruction") or "").strip()
                        if not instr:
                            return {"action": "blocker", "reason": "NEEDS_OWNER_PAYLOAD",
                                    "owner_blocker": True, "stage": str(nxt_id),
                                    "blocker_fields": [f"instruction for {nxt_id}"]}
                        iso2 = check_project_isolation(target, instr, cfg_all)
                        if not iso2["allowed"]:
                            return {"action": "blocker",
                                    "reason": "cross_project_work_refused",
                                    "owner_blocker": True, "scope": iso2["scope"]}
                        # The completed stage is STILL the queue's own `pointer`. The agent
                        # reads that file, so it will redo the finished stage no matter what
                        # the control plane concludes from the artefact (observed live: the
                        # canary appended a second "repeat run" line to ACCEPTANCE_A.md while
                        # the governor was advancing to stage B). Only whoever owns the queue
                        # may rewrite it — the governor editing a project's durable queue
                        # would be the control plane authoring project state — so this is
                        # surfaced, not silently corrected.
                        return {"action": "advance_queue",
                                "reason": "artefact_present_stage_complete",
                                "stage": q.get("pointer"), "next_stage": str(nxt_id),
                                "step_text": instr, "queue_path": qpath,
                                "pointer_stale": True,
                                "resume_instruction": q.get("resume_instruction", ""),
                                "note": "instruction quoted verbatim from the durable queue"}
                    # Artefact absent. If the CURRENT stage declares its own instruction
                    # and the pane is idle, the stage has not been started — deliver that
                    # instruction verbatim, exactly once (the actuator's idempotency and
                    # the progress fingerprint stop repeats). Stages with no `instruction`
                    # (e.g. the MESS queue, which carries payload/implementation blocks)
                    # are untouched by this path.
                    instr_cur = str(cur.get("instruction") or "").strip()
                    if instr_cur and state in ("idle", "waiting_owner"):
                        iso3 = check_project_isolation(target, instr_cur, cfg_all)
                        if not iso3["allowed"]:
                            return {"action": "blocker",
                                    "reason": "cross_project_work_refused",
                                    "owner_blocker": True, "scope": iso3["scope"]}
                        return {"action": "advance_queue",
                                "reason": "stage_not_started",
                                "stage": q.get("pointer"),
                                "next_stage": q.get("pointer"),
                                "step_text": instr_cur, "queue_path": qpath,
                                "resume_instruction": q.get("resume_instruction", ""),
                                "note": "current stage instruction quoted verbatim from "
                                        "the durable queue"}
                    return {"action": "skip", "reason": "stage_in_progress",
                            "stage": q.get("pointer")}
                if status in ("DONE", "PASS", "COMPLETE", "COMPLETED"):
                    nxt = None
                    ids = [str(x.get("id")) for x in q.get("stages") or []]
                    if q.get("pointer") in ids:
                        i = ids.index(q["pointer"])
                        nxt = (q["stages"][i + 1] if i + 1 < len(q["stages"]) else None)
                    if not nxt:
                        return {"action": "skip", "reason": "queue_exhausted",
                                "stage": q.get("pointer")}
                    return {"action": "advance_queue", "reason": "stage_complete_in_queue",
                            "stage": q.get("pointer"), "next_stage": str(nxt.get("id")),
                            "queue_path": qpath,
                            "resume_instruction": q.get("resume_instruction", ""),
                            "note": "next stage taken verbatim from the durable queue"}
                return {"action": "skip", "reason": f"stage_status_{status.lower() or 'unknown'}",
                        "stage": q.get("pointer")}

    # 2b) legacy markdown pointer path (kept for projects without a YAML queue)
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
