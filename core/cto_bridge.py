"""GPT CTO bridge: high-level reasoning events -> provider -> decision ledger -> outbox.

Only these event types ever reach the provider: architecture_conflict, repeated_failure,
shared_core_change, milestone_review, owner_needed. Routine continuation never does (that
stays inside Claude via `/goal`, see core/native_goals.py).

Flow, exactly once per event key:
  1. latch a `cto_decision` row (PK event_key) BEFORE the provider call — a replayed event
     finds the latch and does nothing (no second model call, no second message);
  2. call the provider with a compact structured context (project, agent, goal, state,
     question, git diff summary, recent decisions, affected agents — each bounded);
  3. record the parsed output in `cto_decision` and in the control-plane `decision` ledger;
  4. a message for the agent is ENQUEUED in `native_outbox` (PK msg_key) — delivered by the
     controller Claude session through native cross-session messaging (SendMessage), never
     through ChatGPT browser automation and never by typing into a pane;
  5. `owner_needed: true` (or an owner_needed event) emits OWNER_NEEDED and is never
     auto-approved: the provider's opinion is advice, not owner sign-off.

Provider config (no defaults for model or key; nothing hardcoded):
  OWNER_OS_CTO_PROVIDER = "" (disabled, default) | "openai"
  OWNER_OS_CTO_MODEL    = required when a provider is set
  OPENAI_API_KEY        = required for "openai" (read from the environment only; the value
                          is never logged, stored or returned)
  OWNER_OS_CTO_BASE_URL = optional, default https://api.openai.com/v1
Denied agents (payorch-ha-fresh, + OWNER_OS_CTO_DENY_AGENTS) never get an outbox message.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Optional

from core.control_plane.api import _c
from core.control_plane.store import now_iso, now_ts

REASONING_EVENTS = frozenset({"architecture_conflict", "repeated_failure",
                              "shared_core_change", "milestone_review", "owner_needed"})
HARD_DENY_AGENTS = frozenset({"payorch-ha-fresh"})
SOURCE = "cto_bridge"
_LIMITS = {"question": 2000, "goal": 600, "state": 200, "diff_summary": 4000,
           "decision": 300, "max_decisions": 5, "max_affected": 20}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cto_decision (
    event_key TEXT PRIMARY KEY, event_type TEXT, project TEXT, agent TEXT, provider TEXT,
    model TEXT, context_hash TEXT, status TEXT, decision TEXT, rationale TEXT,
    owner_needed INTEGER DEFAULT 0, message_to_agent TEXT, error TEXT,
    latched_ts REAL, latched_at TEXT, finished_at TEXT);
CREATE TABLE IF NOT EXISTS native_outbox (
    msg_key TEXT PRIMARY KEY, event_key TEXT, to_agent TEXT, message TEXT,
    state TEXT DEFAULT 'pending', created_at TEXT, sent_at TEXT, receipt TEXT);
"""


class ProviderError(Exception):
    pass


class Provider:
    name = "none"
    model = ""

    def decide(self, context: dict) -> dict:  # pragma: no cover — interface
        raise NotImplementedError


class OpenAIResponsesProvider(Provider):
    """OpenAI Responses API (POST {base}/responses). Asks for one JSON object back."""
    name = "openai"
    INSTRUCTIONS = (
        "You are the CTO reviewer for an autonomous engineering fleet. You receive one "
        "structured event. Reply with ONLY a JSON object: {\"decision\": short imperative, "
        "\"rationale\": <=3 sentences, \"owner_needed\": true|false, \"message_to_agent\": "
        "text for the agent or \"\"}. Set owner_needed=true for anything involving "
        "production release, money, credentials, CAPTCHA/anti-bot, paid proxy/VPN, "
        "irreversible actions or product decisions; never approve those yourself.")

    def __init__(self, *, model: str, api_key: str, base_url: str = "", timeout: int = 60,
                 opener=None):
        if not model or not api_key:
            raise ProviderError("model and api key are required")
        self.model, self._key = model, api_key
        self.base = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout, self._open = timeout, opener or urllib.request.urlopen

    def __repr__(self):                       # never expose the key
        return f"OpenAIResponsesProvider(model={self.model!r})"

    def decide(self, context: dict) -> dict:
        body = json.dumps({"model": self.model, "instructions": self.INSTRUCTIONS,
                           "input": json.dumps(context, sort_keys=True)}).encode()
        req = urllib.request.Request(f"{self.base}/responses", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {self._key}",
                                              "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 — never echo the request (it holds the key)
            raise ProviderError(f"request_failed:{type(e).__name__}") from None
        return parse_output(_output_text(data))


def _output_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts = []
    for item in data.get("output") or []:
        for c in (item or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                parts.append(c.get("text") or "")
    return "".join(parts)


def parse_output(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[-1]
    try:
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1])
    except ValueError:
        raise ProviderError("unparseable_provider_output") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("decision"), str):
        raise ProviderError("provider_output_missing_decision")
    return {"decision": obj["decision"][:500], "rationale": str(obj.get("rationale") or "")[:2000],
            "owner_needed": obj.get("owner_needed") is True,
            "message_to_agent": str(obj.get("message_to_agent") or "")[:4000]}


def provider_from_env() -> tuple:
    """(provider | None, gate_reason). Disabled is the default and is not an error."""
    kind = os.getenv("OWNER_OS_CTO_PROVIDER", "").strip().lower()
    if not kind:
        return None, "cto_provider_disabled"
    if kind != "openai":
        return None, f"cto_provider_unknown:{kind}"
    model = os.getenv("OWNER_OS_CTO_MODEL", "").strip()
    if not model:
        return None, "missing_config:OWNER_OS_CTO_MODEL"
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None, "missing_secret:OPENAI_API_KEY"
    return OpenAIResponsesProvider(model=model, api_key=os.environ["OPENAI_API_KEY"].strip(),
                                   base_url=os.getenv("OWNER_OS_CTO_BASE_URL", "")), ""


def _cut(v, n):
    return (str(v) if v is not None else "")[:n]


def build_context(ev: dict) -> dict:
    """Compact, bounded, structured — never a raw pane tail or transcript."""
    return {"event_type": ev.get("type"), "project": _cut(ev.get("project"), 120),
            "agent": _cut(ev.get("agent"), 120), "goal": _cut(ev.get("goal"), _LIMITS["goal"]),
            "state": _cut(ev.get("state"), _LIMITS["state"]),
            "question": _cut(ev.get("question"), _LIMITS["question"]),
            "git_diff_summary": _cut(ev.get("diff_summary"), _LIMITS["diff_summary"]),
            "recent_decisions": [_cut(d, _LIMITS["decision"]) for d in
                                 (ev.get("recent_decisions") or [])[:_LIMITS["max_decisions"]]],
            "affected_agents": [_cut(a, 120) for a in
                                (ev.get("affected_agents") or [])[:_LIMITS["max_affected"]]]}


def agent_denied(agent: str) -> bool:
    extra = {s.strip() for s in os.getenv("OWNER_OS_CTO_DENY_AGENTS", "").split(",")
             if s.strip()}
    base = (agent or "").split(":")[0]
    return base in HARD_DENY_AGENTS or base in extra


def handle_event(ev: dict, *, provider: Optional[Provider] = None, gate: str = "",
                 emit_owner=None, conn=None) -> dict:
    """ev: {key, type, project, agent, goal, state, question, diff_summary, ...}."""
    if ev.get("type") not in REASONING_EVENTS:
        return {"handled": False, "reason": "not_a_reasoning_event"}
    if not ev.get("key"):
        return {"handled": False, "reason": "event_key_required"}
    if provider is None and not gate:
        provider, gate = provider_from_env()
    ctx = build_context(ev)
    ctx_hash = hashlib.sha256(json.dumps(ctx, sort_keys=True).encode()).hexdigest()[:16]
    conn, own = _c(conn)
    try:
        conn.executescript(_SCHEMA)
        cur = conn.execute(
            "INSERT OR IGNORE INTO cto_decision (event_key,event_type,project,agent,provider,"
            "model,context_hash,status,latched_ts,latched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ev["key"], ev["type"], ctx["project"], ctx["agent"],
             getattr(provider, "name", "none"), getattr(provider, "model", ""), ctx_hash,
             "latched", now_ts(), now_iso()))
        conn.commit()
        if cur.rowcount != 1:
            return {"handled": False, "reason": "duplicate_event_key"}
        owner_needed = ev["type"] == "owner_needed"
        if provider is None:
            out, status, err = None, "skipped", gate or "cto_provider_disabled"
        else:
            try:
                out, status, err = provider.decide(ctx), "decided", None
            except ProviderError as e:
                out, status, err = None, "provider_error", str(e)[:200]
        if out:
            owner_needed = owner_needed or out["owner_needed"]
        msg = (out or {}).get("message_to_agent") or ""
        conn.execute("UPDATE cto_decision SET status=?, decision=?, rationale=?, owner_needed=?,"
                     " message_to_agent=?, error=?, finished_at=? WHERE event_key=?",
                     (status, (out or {}).get("decision"), (out or {}).get("rationale"),
                      1 if owner_needed else 0, msg, err, now_iso(), ev["key"]))
        if out:
            from core.control_plane import api as cp
            cp.record_decision("agent", ctx["agent"], "cto_advice", out["decision"][:300],
                               out["rationale"][:1000], model_tier=f"cto:{provider.model}",
                               conn=conn)
        queued = False
        if msg and ctx["agent"] and not agent_denied(ctx["agent"]) and not owner_needed:
            # owner-needed advice is NOT forwarded as an instruction: the owner decides first
            queued = conn.execute(
                "INSERT OR IGNORE INTO native_outbox (msg_key,event_key,to_agent,message,"
                "created_at) VALUES (?,?,?,?,?)",
                (f"cto:{ev['key']}", ev["key"], ctx["agent"], msg, now_iso())).rowcount == 1
        conn.commit()
        if owner_needed and emit_owner is not None:
            emit_owner(ev, (out or {}).get("decision") or ev["type"])
        return {"handled": True, "status": status, "owner_needed": owner_needed,
                "message_queued": queued, "error": err}
    finally:
        if own:
            conn.close()


def pending_messages(conn=None) -> list:
    conn, own = _c(conn)
    try:
        conn.executescript(_SCHEMA)
        return [dict(zip(("msg_key", "to_agent", "message", "created_at"), r)) for r in
                conn.execute("SELECT msg_key,to_agent,message,created_at FROM native_outbox "
                             "WHERE state='pending' ORDER BY created_at")]
    finally:
        if own:
            conn.close()


def mark_sent(msg_key: str, receipt: str, conn=None) -> bool:
    """Called by the controller session after SendMessage. Only pending -> sent, once."""
    conn, own = _c(conn)
    try:
        conn.executescript(_SCHEMA)
        n = conn.execute("UPDATE native_outbox SET state='sent', sent_at=?, receipt=? WHERE "
                         "msg_key=? AND state='pending'",
                         (now_iso(), (receipt or "")[:500], msg_key)).rowcount
        conn.commit()
        return n == 1
    finally:
        if own:
            conn.close()
